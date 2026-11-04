from __future__ import annotations
import argparse
import socket
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import pylsl
try:
    from pylsl.util import LostError, TimeoutError as LSLTimeoutError
except ImportError:     # pylsl before 1.17 kept them in pylsl.pylsl
    from pylsl.pylsl import LostError, TimeoutError as LSLTimeoutError

from . import CLOCK_INTERVAL, SensorRecord, capture_header, clock_fields, open_capture, record_sink, stop_on_stdin_eof, udp_sink

__all__ = ["EmotibitSource"]

# Nominal rates of the EmotiBit signals, for reference when reading the output.
# EmotiBit Oscilloscope decides which of these it relays, through the patchcords in
# lslOutputSettings.json, and under what stream names; nothing here assumes a name.
#   EA 15Hz  ·  PI/PR/PG 25Hz  ·  T1/TH 7.5Hz  ·  AX/AY/AZ, GX/GY/GZ, MX/MY/MZ 25Hz
#   HR, BI, SA/SR/SF are event-driven and carry no nominal rate.

# The EmotiBit transmits a packet roughly every 100ms and EmotiBit Oscilloscope stamps
# every sample in that packet with the one arrival time, so samples do not arrive a nominal
# interval apart: most are microseconds apart and the rest a whole packet apart. Spacing
# between consecutive samples therefore says nothing about loss. What arrived is counted
# against what the nominal rate promises, over a window. On hardware, spacing-based
# detection called 375 samples missing from a stream that was running above its rate.
LOSS_WINDOW = 10.0      # seconds of samples accounted for per check
LOSS_TOLERANCE = 0.05   # fraction of a window that may go missing before it is reported

# EmotiBit announces itself on this UDP port; the Oscilloscope discovers devices by
# listening for it. Scanning it separates "the device never reached the network" from
# "the device is here and the Oscilloscope is not seeing it", which look identical from
# the Oscilloscope's empty device list.
ADVERTISING_PORT = 3131


class EmotibitSource:
    """
    Acquisition of EmotiBit data relayed over LSL by EmotiBit Oscilloscope.

    EmotiBit is not a single-rate device: it publishes one stream per signal, each at
    its own rate, so this produces one record per sample per stream rather than one
    row per instant. Streams may appear and disappear while running, and are picked up
    as they do.
    """
    verbose: bool = True

    names: List[str]
    stale_after: float
    resolve_interval: float
    resolve_wait: float
    poll_interval: float
    inlets: Dict[str, pylsl.StreamInlet]
    samples: Dict[str, int]
    losses: int
    total_samples: int

    def __init__(
        self,
        names: Optional[List[str]] = None,
        sources: Optional[List[str]] = None,
        stale_after: float = 5.0,
        resolve_interval: float = 5.0,
        resolve_wait: float = 1.0,
        poll_interval: float = 0.02,
        loss_window: float = LOSS_WINDOW,
        loss_tolerance: float = LOSS_TOLERANCE,
        correction_wait: float = 2.0,
    ) -> None:
        """
        :param names: LSL stream names to acquire, or None to take every stream found.
        :param sources: LSL source IDs to acquire - the EmotiBit's device ID, which the
            Oscilloscope gives every stream of the device and keeps across its restarts - or
            None to take streams from any device.
        :param stale_after: seconds of silence before a stream is reported stale.
        :param resolve_interval: seconds between looks for streams that appeared later.
        :param resolve_wait: seconds each look waits for streams to answer.
        :param poll_interval: seconds between reads; must be shorter than a sample interval.
        :param loss_window: seconds of samples counted before loss is judged.
        :param loss_tolerance: fraction of a window that may go missing before it is reported.
        :param correction_wait: seconds a new stream's samples are held for its first clock
            correction before they are recorded uncorrected.
        """
        self.names = names or []
        self.sources = sources or []
        # Devices whose streams this capture has opened. LSL shows every outlet on the
        # network, so in a two-participant session a capture by name alone records the other
        # participant's EmotiBit too, and ingest then files it under this one's participant.
        self.devices: Set[str] = set()
        self.stale_after = stale_after
        self.resolve_interval = resolve_interval
        self.resolve_wait = resolve_wait
        self.poll_interval = poll_interval
        self.loss_window = loss_window
        self.loss_tolerance = loss_tolerance
        self.inlets = {}
        self.samples = {}
        self.losses = 0
        self.total_samples = 0
        self.rates: Dict[str, float] = {}
        self.stream_names: Dict[str, str] = {}
        self.source_ids: Dict[str, str] = {}
        self.corrections: Dict[str, float] = {}
        # Streams whose sender has not answered LSL's clock probe. Their samples are recorded
        # uncorrected and say so, and the probe is retried until it answers.
        self.uncorrected: Set[str] = set()
        self.correction_wait = correction_wait
        # Streams opened and waiting for their first clock correction, with the moment they
        # stop waiting and the fields of their connected event. Nothing is read from them
        # meanwhile: LSL buffers their samples, which are then dated correctly.
        self._held: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._epoch_offset = 0.0
        self._last_seen: Dict[str, float] = {}
        self._stale: Dict[str, bool] = {}
        self._window_start: Dict[str, float] = {}
        self._window_count: Dict[str, int] = {}
        # Streams lost and not yet back, by (name, source id). While any is missing the
        # network is looked at every resolve_wait rather than every resolve_interval: LSL
        # gives a new inlet nothing published before it connected, so every second spent
        # finding a restarted stream is a second of its samples gone.
        self._awaiting: Set[Tuple[str, str]] = set()

    def _resolve(self) -> List[pylsl.StreamInfo]:
        """Find the LSL streams currently on the network. Overridden by the test."""
        return pylsl.resolve_streams(self.resolve_wait)

    def open(self) -> List[SensorRecord]:
        """Open an inlet for every wanted stream not already open, and describe each one."""
        # LSL stamps samples with a monotonic clock whose epoch is arbitrary. This offset
        # puts them on the same wall clock as everything else in the pipeline; both clocks
        # tick at the same rate, so it is measured once rather than per sample.
        if not self.inlets:
            self._epoch_offset = time.time() - pylsl.local_clock()
        # Every inlet is opened first, and no clock correction is waited for here: liblsl
        # measures it on a background thread per inlet (about 0.65 s for a local outlet),
        # so asking with a zero timeout only polls. Waiting in turn cost the last of
        # nineteen streams the first twelve seconds of the capture, and stopped every read
        # meanwhile. The streams are held until it answers (settle_corrections).
        found = []
        found_keys = set()
        try:
            for info in self._resolve():
                name = info.name()
                source_id = info.source_id()
                key = info.uid()
                if (self.names and name not in self.names) or key in self.inlets or key in found_keys:
                    continue
                if self.sources and source_id not in self.sources:
                    continue
                # No recovery inside liblsl. A recovering inlet whose outlet is restarted (an
                # Oscilloscope restart) silently re-attaches to the new outlet, while the
                # resolve below opens that outlet again under its new uid: every sample from
                # then on was written twice. Instead a lost stream is closed and reported
                # (see read), and the next resolve opens its successor once.
                inlet = pylsl.StreamInlet(info, recover=False)
                # Opened here rather than left to the first read: liblsl does not transfer
                # anything until the stream is open, so samples pushed in between would be
                # lost, silently taking the first moments of every capture with them.
                inlet.open_stream(timeout=self.resolve_wait)
                # Stamped here, not after the correction pass: the stream starts carrying
                # samples the moment it is opened, and a connected event timestamped a
                # correction pass later would misdate the start of every capture.
                found.append((key, name, source_id, info, inlet, time.time()))
                self._awaiting.discard((name, source_id))
                found_keys.add(key)
        except Exception:
            for _, _, _, _, inlet, _ in found:
                inlet.close_stream()
            raise
        until = time.time() + self.correction_wait
        for key, name, source_id, info, inlet, opened_at in found:
            if source_id not in self.devices:
                self.devices.add(source_id)
                if len(self.devices) > 1 and not self.sources:
                    print(f"emotibit: streams from {len(self.devices)} devices ({', '.join(sorted(self.devices))}) are being "
                          f"recorded together; with a participant, give each computer's capture --source <device ID>", file=sys.stderr)
            self.inlets[key] = inlet
            self.samples[key] = 0
            self.stream_names[key] = name
            self.source_ids[key] = source_id
            self.rates[key] = info.nominal_srate()
            self.uncorrected.discard(key)
            self._last_seen[key] = opened_at
            self._stale[key] = False
            self._window_start.pop(key, None)
            self._window_count.pop(key, None)
            # Stamped with opened_at, not the moment the correction answers: the stream
            # carries samples from the moment it is opened.
            self._held[key] = (until, dict(
                timestamp=opened_at,
                stream_type=info.type(),
                channels=info.channel_count(),
                rate=info.nominal_srate(),
                source_id=source_id,
                stream_uid=key,
            ))
        return self.settle_corrections()

    def settle_corrections(self) -> List[SensorRecord]:
        """
        Release every held stream whose first clock correction has answered, or whose wait is
        over, with its connected event. Never waits.

        A stream that did not answer in time is recorded uncorrected from then on, and says
        so (time_correction_valid); retry_corrections keeps asking.
        """
        records: List[SensorRecord] = []
        now = time.time()
        for key in sorted(self._held):
            until, fields = self._held[key]
            name = self.stream_names[key]
            correction = self._time_correction(name, self.inlets[key], timeout=0.0, quiet=True)
            if correction is None and now < until:
                continue
            del self._held[key]
            if correction is None:
                self.uncorrected.add(key)
                print(f"emotibit: {name}: no clock correction from the sender; timestamps are uncorrected until it answers", file=sys.stderr)
            self.corrections[key] = correction or 0.0
            if self.verbose:
                print(f"emotibit: {name}: connected, {fields['channels']} channel(s), {fields['rate']}Hz")
            records.append(self._event(name, "connected", time_correction=round(self.corrections[key], 6),
                                       time_correction_valid=correction is not None, **fields))
        return records

    def release_held(self) -> List[SensorRecord]:
        """
        Stop waiting for the clock correction of every held stream, as a capture ends.

        Closing a held inlet threw away every sample LSL had buffered for it: a short capture,
        or a stream that came back near the end of a long one, lost them all (found in
        review, 2026-10-10). Released now, its samples are read before the inlet closes, uncorrected
        and saying so unless the correction has answered meanwhile. Polls once; never waits.
        """
        self._held = {key: (0.0, fields) for key, (_, fields) in self._held.items()}
        return self.settle_corrections()

    def retry_corrections(self) -> List[SensorRecord]:
        """
        Ask again for the clock correction of every stream that has none.

        A stream opened while its sender did not answer would otherwise keep a correction of
        zero for its whole life: for an outlet on another computer, an offset of unknown size
        in every timestamp. The answer is recorded as a `time_correction` event; the samples
        before it remain uncorrected, and the checker counts them.

        Never waits: liblsl keeps probing in the background, so this only polls. Waiting half
        a second per stream here stopped every read for 9.5 s at a time with nineteen
        streams whose sender did not answer (found in review, 2026-10-08).
        """
        records: List[SensorRecord] = []
        for key in sorted(self.uncorrected):
            if key not in self.inlets:
                self.uncorrected.discard(key)
                continue
            name = self.stream_names[key]
            correction = self._time_correction(name, self.inlets[key], timeout=0.0, quiet=True)
            if correction is None:
                continue
            self.corrections[key] = correction
            self.uncorrected.discard(key)
            print(f"emotibit: {name}: clock correction now known; samples from here on are corrected", file=sys.stderr)
            records.append(self._event(name, "time_correction", source_id=self.source_ids[key], stream_uid=key,
                                       time_correction=round(correction, 6), time_correction_valid=True))
        return records

    def close(self) -> None:
        """Close every open inlet."""
        for key, inlet in self.inlets.items():
            inlet.close_stream()
            if self.verbose:
                print(f"emotibit: {self.stream_names[key]}: closed after {self.samples[key]} sample(s)")
        self.inlets = {}
        self._held = {}

    def read(self) -> List[SensorRecord]:
        """Pull whatever has arrived on every inlet not held for its clock correction, and return its records."""
        records: List[SensorRecord] = []
        now = time.time()
        lost: List[str] = []
        for key, inlet in self.inlets.items():
            if key in self._held:
                continue
            name = self.stream_names[key]
            try:
                samples, stamps = inlet.pull_chunk(timeout=0.0)
            except LostError as e:
                # The outlet has gone - the Oscilloscope was closed or restarted. Its
                # successor, if one appears, has a new uid and is opened by the next resolve.
                lost.append(key)
                self._awaiting.add((name, self.source_ids[key]))
                print(f"emotibit: {name}: stream lost", file=sys.stderr)
                records.append(self._event(name, "disconnected", source_id=self.source_ids[key], stream_uid=key, reason=str(e) or "stream lost"))
                continue
            if not stamps:
                stale = self._check_stale(key, now)
                if stale is not None:
                    records.append(stale)
                continue
            if self._stale[key]:
                self._stale[key] = False
                records.append(self._event(name, "resumed", source_id=self.source_ids[key], stream_uid=key))
            self._last_seen[key] = now
            for values, stamp in zip(samples, stamps):
                timestamp = stamp + self.corrections[key] + self._epoch_offset
                loss = self._check_loss(key, timestamp)
                if loss is not None:
                    records.append(loss)
                record = self._record(name, timestamp, source_id=self.source_ids[key], stream_uid=key)
                if len(values) == 1:
                    record["value"] = values[0]
                else:
                    # Multi-channel streams are emitted by index: the channel labels live
                    # in the patchcord configuration, not in anything we can read here.
                    for index, value in enumerate(values):
                        record[f"value_{index}"] = value
                records.append(record)
                self.samples[key] += 1
                self.total_samples += 1
        for key in lost:
            self.inlets.pop(key).close_stream()
        return records

    def run(self, sink: Callable[[SensorRecord], None], duration: Optional[float] = None) -> None:
        """
        Acquire for `duration` seconds, or until interrupted.

        :param sink: called with every record produced, in order.
        :param duration: seconds to acquire, or None to run until interrupted.
        """
        deadline = None if duration is None else time.time() + duration
        next_resolve = 0.0
        next_clock = 0.0
        try:
            while deadline is None or time.time() < deadline:
                if time.time() >= next_resolve:
                    for record in self.open():
                        sink(record)
                    if not self.inlets and self.verbose:
                        print("emotibit: no streams found; is EmotiBit Oscilloscope running with LSL output enabled?")
                    for record in self.retry_corrections():
                        sink(record)
                    next_resolve = time.time() + (self.resolve_wait if self._awaiting else self.resolve_interval)
                for record in self.settle_corrections():
                    sink(record)
                if time.time() >= next_clock:
                    # The LSL clock and the offset applied to it travel with the wall clock: a
                    # wall clock stepped mid-session moves BITalino timestamps, which are wall
                    # clock, but not these, which are LSL's plus an offset measured once.
                    next_clock = time.time() + CLOCK_INTERVAL
                    sink(self._event("", "clock", **clock_fields(), lsl=round(pylsl.local_clock(), 6),
                                     epoch_offset=round(self._epoch_offset, 6)))
                for record in self.read():
                    sink(record)
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            # An interrupted capture is an orderly stop too: keep what is buffered.
            self._drain(sink)
            raise
        else:
            self._drain(sink)
        finally:
            self.close()

    def _drain(self, sink: Callable[[SensorRecord], None]) -> None:
        """Record what is still buffered, held streams included, before the inlets close."""
        for record in self.release_held():
            sink(record)
        for record in self.read():
            sink(record)

    def _time_correction(self, name: str, inlet: pylsl.StreamInlet, timeout: float = 0.0, quiet: bool = False) -> Optional[float]:
        """Offset from the sender's clock to ours, or None if the sender will not answer."""
        try:
            return inlet.time_correction(timeout=timeout)
        except (TimeoutError, LSLTimeoutError, LostError):
            # pylsl raises its own TimeoutError, which is not the built-in one: catching only
            # the built-in let one slow stream end the whole capture.
            # Without it the stream is still usable, but its timestamps cannot be trusted
            # against the other streams, which is exactly the failure that hides in a plot -
            # so the record says so (time_correction_valid), and it is asked again.
            if not quiet:
                print(f"emotibit: {name}: no clock correction from the sender; timestamps are uncorrected until it answers", file=sys.stderr)
            return None

    def _check_stale(self, key: str, now: float) -> Optional[SensorRecord]:
        """
        Report a stream that has gone quiet, once, until it comes back.

        Streams with no nominal rate - HR and the SCR family - are event-driven, and
        silence is their ordinary state rather than a fault. Nothing here can tell a
        quiet one from a dead one, so they are not judged at all rather than judged
        wrongly: on a two-minute capture the fixed timeout raised fourteen alarms on
        those three streams and none anywhere else.
        """
        if self.rates[key] <= 0:
            return None
        silent_for = now - self._last_seen[key]
        if self._stale[key] or silent_for <= self.stale_after:
            return None
        self._stale[key] = True
        name = self.stream_names[key]
        print(f"emotibit: {name}: silent for {silent_for:.1f}s", file=sys.stderr)
        return self._event(name, "stale", source_id=self.source_ids[key], stream_uid=key, silent_for=round(silent_for, 3))

    def _check_loss(self, key: str, timestamp: float) -> Optional[SensorRecord]:
        """
        Report samples missing from a stream that has a nominal rate.

        Counted over a window rather than inferred from the spacing of consecutive
        samples, which the burst delivery described at the top of this file makes
        meaningless. A window closes on the first sample past loss_window seconds.
        """
        rate = self.rates[key]
        if rate <= 0:
            return None
        start = self._window_start.get(key)
        if start is None:
            self._window_start[key] = timestamp
            self._window_count[key] = 1
            return None
        self._window_count[key] += 1
        elapsed = timestamp - start
        if elapsed < self.loss_window:
            return None
        counted = self._window_count[key]
        self._window_start[key] = timestamp
        self._window_count[key] = 0
        expected = rate * elapsed
        missing = round(expected - counted)
        if missing <= expected * self.loss_tolerance:
            return None
        self.losses += 1
        name = self.stream_names[key]
        print(f"emotibit: {name}: {missing} of {round(expected)} expected sample(s) missing over {elapsed:.1f}s", file=sys.stderr)
        return self._event(name, "loss", timestamp=timestamp, source_id=self.source_ids[key], stream_uid=key, missing=missing, window=round(elapsed, 3))

    def _record(self, stream: str, timestamp: float, **fields: Any) -> SensorRecord:
        record: SensorRecord = {
            "sensor": "emotibit",
            "stream": stream,
            "timestamp": round(timestamp, 6),
        }
        record.update(fields)
        return record

    def _event(self, stream: str, event: str, timestamp: Optional[float] = None, **fields: Any) -> SensorRecord:
        return self._record(stream, time.time() if timestamp is None else timestamp, event=event, **fields)


def scan_network(seconds: float = 15.0) -> None:
    """
    Listen for EmotiBit announcing itself, and report which hosts are doing so.

    This answers the question the Oscilloscope's empty device list cannot: whether the
    device ever reached this network at all.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", ADVERTISING_PORT))
    except OSError as e:
        print(f"Cannot listen on UDP {ADVERTISING_PORT}: {e}")
        print("EmotiBit Oscilloscope may already hold the port. Close it and scan again.")
        return
    sock.settimeout(1.0)

    # This machine's own broadcasts come back to us on the same port, and the Oscilloscope
    # is one of the things sending them. Without this the scan reports the host talking to
    # itself as a discovered device.
    try:
        local = {info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except socket.gaierror:
        # A host that cannot resolve its own name still scans; it just cannot label its own echo.
        local = set()

    print(f"Listening on UDP {ADVERTISING_PORT} for {seconds:.0f}s. The EmotiBit's blue LED should be blinking.")
    seen: Dict[str, int] = {}
    deadline = time.time() + seconds
    try:
        while time.time() < deadline:
            try:
                payload, sender = sock.recvfrom(2048)
            except socket.timeout:
                continue
            host = sender[0]
            seen[host] = seen.get(host, 0) + 1
            if seen[host] == 1:
                origin = "this machine" if host in local else "REMOTE"
                print(f"  {host} ({origin}): {payload[:120]!r}")
    finally:
        sock.close()

    remote = {host: count for host, count in seen.items() if host not in local}
    if remote:
        print(f"\n{len(remote)} device(s) on the network: " + ", ".join(f"{h} ({n} packets)" for h, n in remote.items()))
        print("The EmotiBit reached this network. If the Oscilloscope still lists nothing, the problem is the Oscilloscope, not the device.")
    elif seen:
        print(f"\nOnly this machine was heard ({', '.join(seen)}) - that is the Oscilloscope's own discovery traffic.")
        print("No EmotiBit answered. Check the blue LED: blinking means connected, solid means the WiFi credentials are wrong.")
    else:
        print("\nNothing heard at all, not even this machine's own discovery traffic.")
        print("Either nothing is scanning for EmotiBits, or a firewall is dropping the traffic before it reaches us.")


def list_streams(wait: float = 2.0) -> None:
    """Print the LSL streams on the network, so their names can be passed to --stream."""
    streams = pylsl.resolve_streams(wait)
    if not streams:
        print("No LSL streams found. Is EmotiBit Oscilloscope running with LSL in its Output List?")
        return
    for info in streams:
        print(f"{info.name()}\ttype={info.type()}\tchannels={info.channel_count()}\trate={info.nominal_srate()}\tsource={info.source_id()}\thost={info.hostname()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture EmotiBit data relayed over LSL to a JSON-lines file")
    parser.add_argument("--scan", action="store_true", help="Listen for the EmotiBit announcing itself on the network and exit. Use this when the Oscilloscope shows no device: it says whether the device reached the network at all.")
    parser.add_argument("--list", action="store_true", help="List the LSL streams currently on the network and exit. The names come from the patchcords in EmotiBit Oscilloscope's lslOutputSettings.json.")
    parser.add_argument("--stream", action="append", dest="streams", default=[], metavar="NAME", help="LSL stream name to acquire. Repeat per stream. Default: every stream found, which will also pick up any unrelated LSL stream on the network.")
    parser.add_argument("--source", action="append", dest="sources", default=[], metavar="DEVICE_ID", help="Acquire only streams of this EmotiBit, by its device ID (the source shown by --list, such as MD-V5-0001107). Repeat for more devices. Required for study data when another participant's EmotiBit is on the network: LSL shows every computer every outlet.")
    parser.add_argument("--duration", type=float, metavar="SECONDS", help="Stop after this many seconds (default: run until interrupted)")
    parser.add_argument("--stale-after", type=float, default=5.0, metavar="SECONDS", help="Report a stream as stale after this much silence (default: 5)")
    parser.add_argument("--output", metavar="FILE", help="Write records here as JSON lines (default: stdout)")
    parser.add_argument("--udp", metavar="HOST:PORT", help="Also send every record as a UDP datagram, one JSON line each, for a realtime consumer such as Unity. Fire-and-forget: the file remains the record.")
    args = parser.parse_args()

    if args.scan:
        scan_network(args.duration or 15.0)
        return

    if args.list:
        list_streams()
        return

    try:
        source = EmotibitSource(args.streams, sources=args.sources, stale_after=args.stale_after)
        # The UDP target is checked before the output is opened, so a mistyped --udp does
        # not leave an empty capture file behind.
        send = udp_sink(args.udp) if args.udp else None
        # Before the output exists: a malformed participant.json stops the capture here.
        header = capture_header("emotibit", clock="lsl", sources=args.sources)
        output = open_capture(args.output)
    except (RuntimeError, OSError) as e:
        print(f"{parser.prog}: Error: {e}", file=sys.stderr)
        sys.exit(1)

    sink = record_sink(output, send)
    reason = "finished"
    try:
        stop_on_stdin_eof()
        sink(header)
        source.run(sink, duration=args.duration)
    except KeyboardInterrupt:
        reason = "interrupted"
        print("emotibit: interrupted", file=sys.stderr)
    finally:
        # Last, so that a file which does not end with it was cut short: killed, or crashed.
        sink({"sensor": "emotibit", "stream": "", "timestamp": round(time.time(), 6), "event": "stopped",
              "reason": reason, "samples": source.total_samples, "loss_windows": source.losses})
        if output is not sys.stdout:
            output.close()

    if not source.samples:
        print(f"{parser.prog}: Error: no streams were ever found", file=sys.stderr)
        sys.exit(1)
    if not source.total_samples:
        print(f"{parser.prog}: Error: streams connected but no samples arrived", file=sys.stderr)
        sys.exit(1)
    if source.losses:
        print(f"emotibit: {source.losses} window(s) with missing samples", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
