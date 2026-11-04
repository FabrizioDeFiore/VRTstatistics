from __future__ import annotations
import argparse
import json
import math
import os
import sys
from importlib.metadata import version as _pkg_version
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["Capture"]

# A BITalino sequence number is four bits wide and wraps, so a detected gap is a lower
# bound on the samples actually lost, never an exact count.
SEQ_MODULO = 16

# Fields every record carries, which are therefore not channels.
RESERVED = {"sensor", "device", "timestamp", "seq", "event", "stream", "stream_uid",
            "source_id", "component", "component_role"}
StreamKey = Tuple[str, str, str, str]

# The capture format this checker understands; see VRTsensors.FORMAT. A capture with no
# `capture` record is format 1.
KNOWN_FORMAT = 2

# How far the wall clock may move against the monotonic clock between two clock readings
# before it counts as stepped: slewing by time synchronisation stays far below this.
CLOCK_STEP = 0.1    # seconds

# Two streams of one signal may overlap by this much without being reported: a stream
# whose successor is opened while its last packet is still being delivered.
OVERLAP_TOLERANCE = 0.5     # seconds


class _Stream:
    """One stream within a capture: a whole BITalino, or one LSL stream of an EmotiBit."""
    name: str
    samples: int
    backwards: int
    duplicate: int
    worst_back: float
    first: float
    last: float

    def __init__(self, name: str) -> None:
        self.name = name
        self.samples = self.backwards = self.duplicate = 0
        self.worst_back = 0.0
        self.first = self.last = 0.0
        self._previous: Optional[float] = None

    def add(self, timestamp: float) -> None:
        if self.samples == 0:
            self.first = timestamp
        self.samples += 1
        self.last = timestamp
        if self._previous is not None:
            if timestamp < self._previous:
                self.backwards += 1
                self.worst_back = max(self.worst_back, self._previous - timestamp)
            elif timestamp == self._previous:
                self.duplicate += 1
        self._previous = timestamp

    @property
    def span(self) -> float:
        return self.last - self.first

    @property
    def rate(self) -> float:
        return self.samples / self.span if self.span > 0 else 0.0

    @property
    def out_of_order(self) -> int:
        return self.backwards + self.duplicate


class Capture:
    """
    What a JSON-lines capture says about itself: rate, ordering, losses and channel ranges.

    Ordering is judged per stream, because a capture with several streams is written in
    arrival order and is not globally sorted by time - reading an EmotiBit file as one
    series reports enormous backward jumps that are only stream boundaries.
    """
    path: str
    streams: Dict[StreamKey, _Stream]
    events: Dict[str, int]
    channels: Dict[Tuple[StreamKey, str], Tuple[float, float, int]]
    seq_gaps: int
    seq_lost: int
    unparseable: int

    def __init__(self, path: str) -> None:
        self.path = path
        self.streams = {}
        self.events = {}
        self.channels = {}
        self.seq_gaps = self.seq_lost = self.unparseable = 0
        self.timing_lost = 0
        self.format = 1
        self.last_event: Optional[str] = None
        self.header: Dict[str, Any] = {}
        # First and latest (wall - monotonic, wall) per device, for the drift over the capture.
        self._skew_first: Dict[Tuple[str, str], Tuple[float, float]] = {}
        self._last_is_event = False
        self.clock_steps: List[str] = []
        self._skew: Dict[Tuple[str, str], Tuple[float, float]] = {}
        self._sequence: Dict[StreamKey, int] = {}
        # LSL clock correction per EmotiBit stream: True known, False failed, None not recorded
        # (before VRTsensors recorded it); and the samples dated without a known one.
        self._corrected: Dict[StreamKey, Optional[bool]] = {}
        self.uncorrected: Dict[str, int] = {}
        self.correction_unrecorded: Dict[str, int] = {}
        self.correction_missing: Dict[str, int] = {}
        self.errors: List[str] = []
        self._read()

    def _read(self) -> None:
        """Read the whole capture, accumulating what `report` will later judge.

        A bad line is recorded and scanning continues: one malformed record must not hide
        everything after it, and "this file has 3 invalid lines" is a more useful finding
        than a traceback."""
        with open(self.path, encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    self._check_record(record)
                except (json.JSONDecodeError, ValueError) as error:
                    self.unparseable += 1
                    if len(self.errors) < 10:
                        self.errors.append(f"{self.path}:{number}: {error}")
                    continue
                key = (record["sensor"], record.get("device") or record.get("source_id", ""),
                       record.get("stream", ""), record.get("stream_uid", ""))
                if record.get("event"):
                    event = record["event"]
                    self.events[event] = self.events.get(event, 0) + 1
                    if event == "capture" and isinstance(record.get("format"), int):
                        self.format = record["format"]
                        self.header = record
                    self.last_event = event
                    if event == "gap" and record.get("detected") == "timing" and isinstance(record.get("lost"), int):
                        self.timing_lost += record["lost"]
                    self._check_clock(record)
                    self._last_is_event = True
                    if record["sensor"] == "emotibit" and event in ("connected", "time_correction"):
                        valid = record.get("time_correction_valid")
                        self._corrected[key] = valid if isinstance(valid, bool) else None
                    if event in ("connected", "disconnected"):
                        self._sequence.pop(key, None)
                        if key in self.streams:
                            self.streams[key]._previous = None
                    continue
                self._last_is_event = False
                self._sample(record, key)

    def _check_clock(self, record: Dict[str, Any]) -> None:
        """Note a wall clock that moved against the monotonic one between two clock readings."""
        wall, monotonic = record.get("wall"), record.get("monotonic")
        if not isinstance(wall, (int, float)) or not isinstance(monotonic, (int, float)):
            return
        key = (record["sensor"], record.get("device") or record.get("source_id", ""))
        skew = wall - monotonic
        if key in self._skew:
            previous, at = self._skew[key]
            if abs(skew - previous) > CLOCK_STEP:
                self.clock_steps.append(f"{' / '.join(p for p in key if p)}: the wall clock moved {skew - previous:+.3f}s "
                                        f"against the monotonic clock between {at:.3f} and {wall:.3f}")
        self._skew_first.setdefault(key, (skew, wall))
        self._skew[key] = (skew, wall)

    def _check_record(self, record: Any) -> None:
        """Raise ValueError unless this is a record the pipeline can carry.

        Deliberately strict about types rather than truthiness: `bool` is a subclass of
        `int` in Python, so a `seq` of `True` would otherwise pass as 1, and a non-finite
        float survives a JSON round-trip but breaks every consumer downstream."""
        if not isinstance(record, dict) or not isinstance(record.get("sensor"), str) or not record["sensor"]:
            raise ValueError("expected an object with a nonempty sensor name")
        timestamp = record.get("timestamp")
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
            raise ValueError("timestamp must be a finite number")
        for field in ("device", "source_id", "stream", "stream_uid", "event"):
            if field in record and not isinstance(record[field], str):
                raise ValueError(f"{field} must be a string")
        if "event" in record and not record["event"]:
            raise ValueError("event must not be empty")
        if record["sensor"] == "bitalino" and "seq" in record:
            seq = record["seq"]
            if isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq < SEQ_MODULO:
                raise ValueError("BITalino seq must be an integer from 0 to 15")
        for field, value in record.items():
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{field} must be finite")

    def _sample(self, record: Dict[str, Any], key: StreamKey) -> None:
        """Take one sample into its stream's ordering, its sequence check and its channel ranges.

        Everything is kept per stream, never per file: a multi-stream capture is written in
        arrival order and is not globally sorted by time, so judging it as one series
        reports stream boundaries as enormous backward jumps.

        A numeric field is a channel unless it is in RESERVED or is a bool - a digital line
        arrives as 0/1 and is a real channel, while a JSON `true` is metadata."""
        name = " / ".join(part for part in key if part)
        stream = self.streams.setdefault(key, _Stream(name))
        # An EmotiBit sample is corrected only when an event said so. One with no connected or
        # time_correction event before it has no correction state at all, and unknown is not valid.
        if record["sensor"] == "emotibit" and self._corrected.get(key) is not True:
            if key not in self._corrected:
                tally = self.correction_missing
            else:
                tally = self.uncorrected if self._corrected[key] is False else self.correction_unrecorded
            label = " / ".join(part for part in key[:3] if part)
            tally[label] = tally.get(label, 0) + 1
        stream.add(record["timestamp"])
        sequence = record.get("seq") if record["sensor"] == "bitalino" else None
        if sequence is not None:
            if key in self._sequence:
                expected = (self._sequence[key] + 1) % SEQ_MODULO
                if sequence != expected:
                    self.seq_gaps += 1
                    self.seq_lost += (sequence - expected) % SEQ_MODULO
            self._sequence[key] = sequence
        for field, value in record.items():
            if field in RESERVED or isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            channel = (key, field)
            low, high, count = self.channels.get(channel, (value, value, 0))
            self.channels[channel] = (min(low, value), max(high, value), count + 1)

    def overlapping_copies(self) -> List[str]:
        """
        Signals recorded twice at once: two streams of one device and one signal name,
        told apart only by their LSL uid, whose samples overlap in time.

        A stream that is lost and comes back gets a new uid, so two uids per signal are
        normal when they follow one another. Overlapping, they are one signal written twice,
        which no per-stream check can see: each copy on its own is clean.
        """
        groups: Dict[Tuple[str, str, str], List[_Stream]] = {}
        for (sensor, identity, stream, uid), s in self.streams.items():
            if uid:
                groups.setdefault((sensor, identity, stream), []).append(s)
        found = []
        for (sensor, identity, stream), members in sorted(groups.items()):
            members.sort(key=lambda s: s.first)
            for earlier, later in zip(members, members[1:]):
                overlap = earlier.last - later.first
                if overlap > OVERLAP_TOLERANCE:
                    found.append(f"{' / '.join(p for p in (sensor, identity, stream) if p)}: two streams overlap "
                                 f"by {overlap:.1f}s - the same signal recorded twice")
        return found

    @property
    def samples(self) -> int:
        return sum(s.samples for s in self.streams.values())

    @property
    def out_of_order(self) -> int:
        return sum(s.out_of_order for s in self.streams.values())

    def study_findings(self) -> List[str]:
        """
        What a capture needs to be study data, beyond being sound: the current format, a
        participant ID, and code that can be named - a committed revision.

        Not part of `report`, because a demo or a test bench has none of these and is not
        wrong for it; `VRTsensors-validate --study` adds them.
        """
        findings = []
        if self.format < 2:
            findings.append(f"capture format {self.format}: no capture record, so no code version, participant or clean end")
            return findings
        if not self.header.get("participant"):
            findings.append("no participant ID: put participant.json in the role's configuration folder")
        for label, count in sorted(self.uncorrected.items()):
            findings.append(f"{label}: {count} samples dated without LSL clock correction (the sender did not answer the probe)")
        for label, count in sorted(self.correction_unrecorded.items()):
            findings.append(f"{label}: {count} samples, and whether LSL clock correction succeeded was not recorded (VRTsensors before 2026-10-08)")
        for label, count in sorted(self.correction_missing.items()):
            findings.append(f"{label}: {count} samples before any connected or time_correction event, so their clock correction is unknown (an incomplete or edited file)")
        devices = sorted({identity for sensor, identity, stream, uid in self.streams if identity})
        if len(devices) > 1:
            findings.append(f"samples from {len(devices)} devices ({', '.join(devices)}) in one participant's capture")
        if self.header.get("sensor") == "emotibit" and not self.header.get("sources"):
            findings.append("an EmotiBit capture not bound to its device: give it --source <device ID>, or another participant's EmotiBit on the network is recorded too")
        # Clean is a claim, so only an explicit `dirty: false` makes it: a checkout whose status
        # could not be read is unknown, not clean. The check covers VRTsensors/src, the code
        # that wrote the samples; a local runconfig.json or sensors.json change is not in it.
        revision = self.header.get("revision")
        dirty = self.header.get("dirty")
        if not isinstance(revision, str) or not revision.strip():
            findings.append("the code's git revision is unknown: run VRTsensors from a git checkout")
        elif dirty is True:
            findings.append(f"recorded with uncommitted changes to VRTsensors on top of {revision[:9]}")
        elif dirty is not False:
            findings.append(f"whether the code had uncommitted changes on top of {revision[:9]} is unknown: git status failed when the capture started")
        return findings

    def report(self) -> List[str]:
        """Detected problems, independent of verbosity; no findings is not proof of completeness."""
        findings = []
        if self.unparseable:
            findings.append(f"{self.unparseable} invalid records")
            findings.extend(self.errors)
        if not self.samples:
            findings.append("no samples at all" + (f" - only {self.events}" if self.events else ""))
        if self.seq_gaps:
            findings.append(f"{self.seq_gaps} sequence discontinuities, at least {self.seq_lost} samples lost")
        if self.timing_lost:
            findings.append(f"at least {self.timing_lost} samples lost that the sequence number could not show, found by timing")
        findings.extend(self.overlapping_copies())
        findings.extend(f"clock stepped: {step}" for step in self.clock_steps)
        # From format 2 a capture's last record is `stopped`; without it the process was
        # killed or crashed, and the file may end anywhere.
        if self.format >= 2 and not (self._last_is_event and self.last_event == "stopped"):
            findings.append("no 'stopped' record at the end: the capture was killed or crashed, so its end is not known to be complete")
        if self.format > KNOWN_FORMAT:
            findings.append(f"written in capture format {self.format}, newer than this checker's {KNOWN_FORMAT}: update VRTsensors")
        # `unreachable` means the acquisition gave up on a device that never answered, so
        # everything after it is absent by decision rather than by accident. That is a
        # finding, not an observation: a capture that ends this way is incomplete and the
        # verdict has to say so.
        for event in ("loss", "gap", "disconnected", "stale", "unreachable"):
            if self.events.get(event):
                findings.append(f"{self.events[event]} acquisition '{event}' event(s); inspect the capture for details")
        if self.out_of_order and not self._legacy_ordering():
            findings.append(self._ordering())
        return findings

    def _legacy_ordering(self) -> bool:
        """
        Whether the steps back are the known property of a format-1 BITalino capture.

        Format 1 dated each sample from its batch's arrival, so 10-34% of samples stepped
        back by up to a sample period: true of every such capture, and so no verdict on any.
        """
        return self.format < 2 and all(key[0] == "bitalino" for key, s in self.streams.items() if s.out_of_order)

    def _ordering(self) -> str:
        share = 100.0 * self.out_of_order / max(1, self.samples)
        worst = max(s.worst_back for s in self.streams.values())
        return (f"{self.out_of_order} samples ({share:.1f}%) not strictly increasing in time, "
                f"worst step back {worst * 1000:.1f} ms")

    def notes(self) -> List[str]:
        """
        Observations that are not problems and never change the verdict.

        A constant channel is the one worth stating unprompted: an unused digital line, a
        genuinely steady signal and an electrode that was never connected all look identical
        here, and only the last is a fault. Saying so costs a line; leaving someone to find a
        dead electrode after the session costs the session.
        """
        notes = []
        for key, (first, since) in sorted(self._skew_first.items()):
            latest, at = self._skew[key]
            if at - since >= 60:
                notes.append(f"{' / '.join(p for p in key if p)}: the wall clock moved {1000 * (latest - first):+.1f} ms against "
                             f"the monotonic clock over {(at - since) / 60:.0f} min")
        if self.out_of_order and self._legacy_ordering():
            notes.append(self._ordering() + " - format 1, where batch-arrival timestamps always do this")
        constant = [f"{self.streams[key].name} / {field}"
                    for (key, field), (low, high, _) in self.channels.items() if low == high]
        if constant:
            notes.append(f"{len(constant)} channel(s) never changed: {', '.join(sorted(constant))}"
                         " - unused line, steady signal or disconnected sensor; check which")
        return notes


def _print(capture: Capture, verbose: bool, findings: List[str]) -> None:
    """
    Print one capture's summary, its streams, and its observations and findings.

    Observations are prefixed `-` and findings `!`, and only findings decide the exit
    status - which is what lets a constant channel always be reported without an ambiguous
    case failing a run. `verbose` adds channel ranges and event counts and changes nothing
    about the verdict.
    """
    print(f"\n{capture.path}")
    print(f"  {len(capture.streams)} stream(s), {capture.samples} samples, {sum(capture.events.values())} events")
    for name in sorted(capture.streams, key=lambda n: -capture.streams[n].samples):
        s = capture.streams[name]
        note = f"{s.out_of_order} out of order" if s.out_of_order else "in order"
        print(f"    {s.name:<28} {s.samples:>7} samples  {s.span:>7.1f}s  {s.rate:>7.2f} Hz  {note}")
    if verbose:
        for key, field in sorted(capture.channels):
            low, high, count = capture.channels[(key, field)]
            flag = "  never changed" if low == high else ""
            print(f"    channel {capture.streams[key].name} / {field}  n={count:<8} {low:g} .. {high:g}{flag}")
        if capture.events:
            print(f"    events: {capture.events}")
    # "-" is an observation and "!" is a finding: only the second decides the exit status.
    for note in capture.notes():
        print(f"  - {note}")
    for finding in findings:
        print(f"  ! {finding}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check VRTsensors captures for loss, ordering and dead channels")
    parser.add_argument("--version", action="version", version=f"%(prog)s {_pkg_version('VRTsensors')}")
    parser.add_argument("paths", nargs="+", metavar="PATH", help="Capture files, or directories to search for *.jsonl")
    parser.add_argument("--verbose", action="store_true", help="Also show every channel's range and the event counts")
    parser.add_argument("--quiet", action="store_true", help="Report only captures with findings")
    parser.add_argument("--study", action="store_true", help="Also require what study data needs: format 2, a participant ID, and a committed code revision")
    args = parser.parse_args()

    files: List[str] = []
    for path in args.paths:
        if os.path.isdir(path):
            for root, dirs, names in os.walk(path):
                dirs.sort()     # a stable order, so two runs over one directory print alike
                files.extend(os.path.join(root, n) for n in sorted(names) if n.endswith(".jsonl"))
        else:
            files.append(path)
    if not files:
        print("No .jsonl captures found", file=sys.stderr)
        return 2

    unsound = 0
    for path in files:
        try:
            capture = Capture(path)
        except (OSError, UnicodeError) as error:
            print(f"\n{path}\n  ! cannot read capture: {error}")
            unsound += 1
            continue
        findings = capture.report() + (capture.study_findings() if args.study else [])
        if findings:
            unsound += 1
        if findings or not args.quiet:
            _print(capture, args.verbose, findings)

    print(f"\n{len(files)} capture(s), {unsound} with findings")
    return 1 if unsound else 0


if __name__ == "__main__":
    sys.exit(main())
