"""
Physiological sensor acquisition for VRTstatistics.

One module per device - `bitalino`, `emotibit` - each producing the same shape of
record, plus `ingest`, which merges captures into a session, and `validate`, which checks them.
This module holds what they share: the record type, where a capture writes, and which
sensors file applies to a configuration directory.
"""
from __future__ import annotations
import json
import os
import socket
import sys
import time
from typing import Any, Callable, Dict, Optional, TextIO

__all__ = ["SensorRecord", "udp_sink", "open_capture", "record_sink", "capture_header", "clock_fields", "stop_on_stdin_eof",
           "read_study_file", "source_revision", "PARTICIPANT", "SESSION",
           "sensors_path", "sensors_write_path", "SENSORS", "SENSORS_LOCAL", "FORMAT"]

type SensorRecord = dict[str, Any]
"""
A single record produced by a sensor source: either one sample or one event.

Records are free-form dicts, the shape VRTstatistics.datastore already handles. Every record
carries `sensor` and `timestamp` (seconds since the epoch, on the acquiring machine's clock);
an event also carries `event`. Other field names are per device and are documented, not fixed
by a schema.
"""

# The version of what a capture file means, written in its first record. Bump it whenever the
# meaning of a field changes, not only its presence: readers refuse a format newer than theirs.
#   1  no `capture` record (captures before 2026-10-07). A BITalino sample's timestamp is its
#      batch's arrival, back-dated at the nominal rate.
#   2  a `capture` record first, `clock` records every CLOCK_INTERVAL, and a `stopped` record
#      last unless the capture was killed or crashed. A BITalino sample's timestamp comes from
#      its sample count (see bitalino._SampleClock).
FORMAT = 2

# Seconds between `clock` records.
CLOCK_INTERVAL = 10.0

# Records that describe the capture rather than the signal. They belong in the file, which is
# the record, and are not sent to a realtime consumer, which has no use for them.
FILE_ONLY_EVENTS = {"capture", "clock", "stopped"}


# Who and what a capture belongs to, read from the working directory the capture runs in.
# VRTrun uploads a configuration's top-level files and then its role's files into that
# directory, so session.json beside runconfig.json describes the session for every role, and
# participant.json in a role's folder says which participant that role's machine recorded.
# Both are optional; `VRTsensors-validate --study` requires the participant.
PARTICIPANT = "participant.json"
SESSION = "session.json"

# Packages whose version changes what a capture contains, recorded in every capture header.
RECORDED_DEPENDENCIES = ("bitalino", "pylsl", "numpy", "pyserial")


def read_study_file(name: str, directory: str = ".") -> Optional[Dict[str, Any]]:
    """
    The JSON object in `directory`/`name`, or None when there is no such file.

    A file that exists but is not a JSON object is an error, raised before anything is
    recorded: a participant ID that silently goes missing is worse than a capture that
    refuses to start.
    """
    path = os.path.join(directory, name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as fp:
            content = json.load(fp)
    except (OSError, ValueError) as e:
        raise RuntimeError(f"{path}: {e}")
    if not isinstance(content, dict):
        raise RuntimeError(f"{path}: expected a JSON object")
    if name == PARTICIPANT and not (isinstance(content.get("participant"), str) and content["participant"]):
        raise RuntimeError(f"{path}: expected {{\"participant\": \"<pseudonymous ID>\"}}")
    return content


def source_revision() -> Dict[str, Any]:
    """
    The git commit the running VRTsensors comes from, and whether its checkout had changes.

    The package version is fixed when the package is installed, so an editable install that
    has been pulled since reports an old version. The commit is read now, from the checkout
    the code is running from; outside a git checkout both fields are None.
    """
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        revision = subprocess.run(["git", "-C", here, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        if revision.returncode != 0:
            return {"revision": None, "dirty": None}
        status = subprocess.run(["git", "-C", here, "status", "--porcelain", "--untracked-files=no", "--", ".."],
                                capture_output=True, text=True, timeout=5)
        return {"revision": revision.stdout.strip(), "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None}
    except (OSError, subprocess.SubprocessError):
        return {"revision": None, "dirty": None}


def capture_header(sensor: str, **fields: Any) -> SensorRecord:
    """
    The first record of every capture: which format it is in, what made it, and whose it is.

    The version, commit, dependencies, command line and host are what a reader needs to
    reproduce or question a capture months later, and none of them can be recovered from the
    samples. `participant` and `session` come from participant.json and session.json in the
    working directory, when they are there.
    """
    from importlib.metadata import PackageNotFoundError, version
    try:
        package = version("VRTsensors")
    except PackageNotFoundError:
        package = "unknown"
    dependencies = {}
    for name in RECORDED_DEPENDENCIES:
        try:
            dependencies[name] = version(name)
        except PackageNotFoundError:
            dependencies[name] = None
    header: SensorRecord = {
        "sensor": sensor, "timestamp": round(time.time(), 6), "event": "capture", "format": FORMAT,
        "version": package, **source_revision(), "python": sys.version.split()[0], "dependencies": dependencies,
        "command": [os.path.basename(sys.argv[0])] + sys.argv[1:], "host": socket.gethostname(),
    }
    participant = read_study_file(PARTICIPANT)
    session = read_study_file(SESSION)
    if participant is not None:
        header["participant"] = participant["participant"]
    if session is not None:
        header["session"] = session
    header.update(fields)
    return header


# Set by a launcher (VRTrunserver, the Unity editor) that holds the capture's standard input
# and closes it to ask the capture to stop.
STOP_ON_STDIN_EOF = "VRTSENSORS_STOP_ON_STDIN_EOF"


def stop_on_stdin_eof() -> None:
    """
    When a launcher asks for it, treat the end of standard input as Ctrl-C.

    A launcher that kills a capture leaves a file that cannot be told from one whose process
    crashed, and a BITalino that was never told to stop. Closing the capture's standard input
    instead lets it end the way Ctrl-C ends it - the device stopped, the file closed, a
    `stopped` record last - and a launcher still kills it if it has not gone within seconds.
    Run by hand, without the variable, standard input is left alone.
    """
    if os.environ.get(STOP_ON_STDIN_EOF) != "1" or sys.stdin is None:
        return
    import threading
    import _thread

    def watch() -> None:
        try:
            while sys.stdin.read(4096):
                pass
        except (OSError, ValueError):
            pass
        _thread.interrupt_main()

    threading.Thread(target=watch, name="stop-on-stdin-eof", daemon=True).start()


def clock_fields() -> Dict[str, float]:
    """
    Both clocks of this machine, read together, for a `clock` record.

    The wall clock dates every record; the monotonic clock never jumps. A difference between
    them that changes from one clock record to the next is the wall clock being stepped -
    a time synchronisation during the session - and everything dated after it moved with it,
    except BITalino samples, which keep their connection's mapping (see bitalino.wall_epoch).
    """
    return {"wall": round(time.time(), 6), "monotonic": round(time.monotonic(), 6)}


# What a machine acquires is declared in sensors.json. A sensors.local.json beside it wins.
#
# The two exist because one file would otherwise answer two different questions. In a shared
# configuration tree, sensors.json is documentation: what a role runs, checked in, and meant to
# stay put. On a machine, the same name would also have to say what is physically plugged in
# today, which git cannot know and which changes between sessions. Editing the first to answer
# the second commits one session's wiring over the example, where it looks exactly like the
# example. So the local answer has its own file, which is gitignored, and everything that reads
# either one says which it used - a stale local file is otherwise invisible until a run comes
# back empty.
SENSORS = "sensors.json"
SENSORS_LOCAL = "sensors.local.json"


def sensors_path(configdir: str) -> Optional[str]:
    """The sensors file that applies in `configdir` - the local one if there is one - or None."""
    local = os.path.join(configdir, SENSORS_LOCAL)
    if os.path.exists(local):
        return local
    shared = os.path.join(configdir, SENSORS)
    return shared if os.path.exists(shared) else None


def sensors_write_path(configdir: str) -> str:
    """
    Where to write a machine's wiring in `configdir`.

    The local file whenever there is already a sensors.json to preserve, so --setup on a
    checked-out example never overwrites the example; sensors.json itself in a directory
    that has none yet, which is the case for someone setting up their own config.
    """
    if os.path.exists(os.path.join(configdir, SENSORS_LOCAL)):
        return os.path.join(configdir, SENSORS_LOCAL)
    if os.path.exists(os.path.join(configdir, SENSORS)):
        return os.path.join(configdir, SENSORS_LOCAL)
    return os.path.join(configdir, SENSORS)


def udp_sink(target: str) -> Callable[[str], None]:
    """
    Return a function that sends one JSON line per call as a UDP datagram to host:port.

    For the realtime path into Unity. UDP is deliberate: a listener that is not running,
    has stopped listening, or cannot keep up must not slow down or break the capture,
    which is the file's job. Nothing is retried and delivery is not guaranteed, so the
    receiver has to treat every datagram as optional and the file as the record.
    """
    host, _, port = target.rpartition(":")
    if not host or not port.isdigit():
        raise RuntimeError(f"UDP target '{target}' should look like host:port, e.g. 127.0.0.1:9010")
    address = (host, int(port))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(line: str) -> None:
        try:
            sock.sendto(line.encode(), address)
        except OSError as e:
            # One unreachable datagram must not end a capture that is otherwise fine.
            print(f"udp: {target}: {e}", file=sys.stderr)

    return send


def open_capture(path: Optional[str]) -> TextIO:
    """
    The stream a capture writes to: the file at `path`, or stdout when `path` is None.

    A non-empty file is refused, because a capture is a record and is never overwritten. The
    file's directory is created, so a capture aimed at a fresh run directory does not fail
    before it has even connected.
    """
    if path is None:
        return sys.stdout
    if os.path.exists(path) and os.path.getsize(path) > 0:
        raise RuntimeError(f"{path} already exists and is not empty; a capture is a record, so it is not overwritten")
    if os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    return open(path, "w", encoding="utf-8")


def record_sink(output: TextIO, send: Optional[Callable[[str], None]] = None) -> Callable[[SensorRecord], None]:
    """
    Return the function every source calls with each record: one JSON line to `output`, and
    the same line to `send` (a udp_sink) when there is one.

    Flushed per record, so a device that drops out or a capture that is killed loses at most
    the record being written, never the tail of the file.
    """
    def sink(record: SensorRecord) -> None:
        line = json.dumps(record)
        output.write(line + "\n")
        output.flush()
        if send is not None and record.get("event") not in FILE_ONLY_EVENTS:
            send(line)

    return sink
