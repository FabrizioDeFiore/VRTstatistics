#!/usr/bin/env python3
"""
Test for VRTsensors.bitalino against a simulated device.

Covers what is awkward to test with hardware in hand: that every named port gets exactly
its own column and nothing else, the record shape, presses on the digital line, lost
samples via the sequence number, the reconnect path after a dropout, and the setup that
writes sensors.json. Run it before touching the real device, and after any change to the
module.

  python test_bitalino.py

This does NOT replace the physical test: unplug the device mid-capture and confirm the
output shows the dropout and the recovery; press the button and confirm a press.
"""
import sys
import time

import numpy

import VRTsensors.bitalino as module
from VRTsensors.bitalino import BitalinoSource, SEQ_MODULO, ANALOG_PORTS, parse_channel, channel_labels, setup, _Button

RATE = 100
BATCH = 10
LABELS = {"A1": "eda", "A2": "acc"}

failures = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global failures
    if condition:
        print(f"  ok    {label}")
    else:
        failures += 1
        print(f"  FAIL  {label}{': ' + detail if detail else ''}")


def pattern(channel: int, index: int) -> int:
    """A value no two ports share at any sample index: the port's number in the hundreds, the sample in the units."""
    return (channel + 1) * 100 + index % 50


class _FakeDevice:
    """
    Stand-in for bitalino.BITalino: counts out samples, then stops responding.

    Packs the frame the way the vendor decoder does: the acquired analog ports in
    ascending order after the five leading columns, with a fifth and sixth acquired
    port truncated to 6 bits.
    """

    def __init__(self, channels, dropout_after, skip_seq, levels=None, digital=None):
        self.channels = channels
        self.dropout_after = dropout_after   # batches to serve before the device "goes away"
        self.skip_seq = skip_seq             # sequence numbers to skip, i.e. samples lost in transit
        self.levels = levels                 # channel -> value or function of the sample index; default the pattern above
        self.digital = digital or {}         # column (1 for I1, 2 for I2) -> function of the sample index; default 1, the line at rest
        self.served = 0
        self.batches = 0
        self.seq = 0
        self.closed = False

    def version(self):
        return "BITalino_v5.2"

    def start(self, rate, channels):
        self.rate = rate

    def read(self, nSamples):
        if self.batches >= self.dropout_after:
            # The vendor API reports a lost device with a plain Exception; so do we.
            raise Exception("The computer lost communication with the device.")
        self.batches += 1
        time.sleep(nSamples / self.rate)
        frame = numpy.zeros((nSamples, 5 + len(self.channels)), dtype=int)
        for sample in range(nSamples):
            while self.seq in self.skip_seq:
                self.seq = (self.seq + 1) % SEQ_MODULO
            frame[sample, 0] = self.seq
            for column in (1, 2):
                frame[sample, column] = self.digital[column](self.served) if column in self.digital else 1
            for index, channel in enumerate(sorted(self.channels)):
                level = pattern(channel, self.served) if self.levels is None else self.levels.get(channel, 2)
                value = level(self.served) if callable(level) else level
                frame[sample, 5 + index] = value & 0x3F if index >= 4 else value
            self.served += 1
            self.seq = (self.seq + 1) % SEQ_MODULO
        return frame

    def stop(self):
        pass

    def close(self):
        self.closed = True


class _FakeSource(BitalinoSource):
    """A BitalinoSource whose devices are simulated. Later connections never drop out."""

    def __init__(self, *args, dropout_after=99, skip_seq=(), levels=None, digital=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.dropout_after = dropout_after
        self.skip_seq = skip_seq
        self.levels = levels
        self.digital = digital
        self.connections = 0
        self.devices = []

    def _connect(self):
        self.connections += 1
        device = _FakeDevice(
            self.channels,
            self.dropout_after if self.connections == 1 else 99,
            self.skip_seq,
            self.levels,
            self.digital,
        )
        self.devices.append(device)
        return device


def test_records():
    print("records:")
    source = _FakeSource("FAKE", LABELS, rate=RATE, batch=BATCH)
    source.verbose = False
    opened = source.open()
    records = source.read()
    source.close()

    check("connected first, then one module event per named port", [r.get("event") for r in opened] == ["connected", "module", "module"], repr(opened))
    connected = opened[0]
    check("connect event names the channels", connected.get("channels") == {"eda": "A1", "acc": "A2"}, repr(connected.get("channels")))
    check("connect event states the resolution", connected.get("bits") == {"eda": 10, "acc": 10}, repr(connected.get("bits")))
    check("a module event is the name given, not a guess", opened[1] == {**opened[1], "port": "A1", "channel": "eda", "kind": "eda", "inferred": False}, repr(opened[1]))
    check("one record per sample", len(records) == BATCH, f"{len(records)} records")

    first = records[0]
    check("sensor and device identified", first.get("sensor") == "bitalino" and first.get("device") == "FAKE", repr(first))
    check("channels present under their names", first.get("eda") == 100 and first.get("acc") == 200, repr(first))
    check("unnamed digital lines present as i1 and i2", first.get("i1") == 1 and first.get("i2") == 1, repr(first))
    check("sequence number present", first.get("seq") == 0, repr(first))
    check("nothing else in a sample", set(first) == {"sensor", "device", "timestamp", "seq", "eda", "acc", "i1", "i2"}, repr(sorted(first)))

    stamps = [r["timestamp"] for r in records]
    check("timestamps increase", all(b > a for a, b in zip(stamps, stamps[1:])))
    spacing = [round(b - a, 6) for a, b in zip(stamps, stamps[1:])]
    check("timestamps are one sample interval apart", all(abs(s - 1 / RATE) <= 1.1e-6 for s in spacing), repr(spacing))
    check("no samples reported lost", source.lost == 0, f"lost={source.lost}")


def test_independence():
    print("port independence:")
    # Every port carries a value no other port ever carries. Whatever subset is named,
    # each name must read its own port's pattern at every sample and never a neighbour's.
    subsets = [["A1"], ["A6"], ["A2", "A5"], ["A1", "A3", "A6"], ["A4", "A5", "A6"], ANALOG_PORTS]
    for ports in subsets:
        labels = {port: f"n{port[1]}" for port in ports}
        source = _FakeSource("FAKE", labels, rate=RATE, batch=BATCH)
        source.verbose = False
        opened = source.open()
        records = [r for _ in range(3) for r in source.read() if "seq" in r]
        source.close()
        expected = {}
        clean = True
        for index, record in enumerate(records):
            for column, port in enumerate(ports):
                value = pattern(ANALOG_PORTS.index(port), index)
                expected[port] = value & 0x3F if column >= 4 else value
                clean = clean and record[labels[port]] == expected[port]
            clean = clean and not any(name in record for name in ANALOG_PORTS)
        check(f"{'+'.join(ports)}: each name reads exactly its own port, unnamed ports are absent", clean, repr(records[-1]))
        bits = opened[0]["bits"]
        check(f"{'+'.join(ports)}: resolution is 10 bits for the first four acquired, 6 after", bits == {labels[p]: (10 if i < 4 else 6) for i, p in enumerate(ports)}, repr(bits))


def test_button():
    print("button on a digital line:")
    clock = [0]

    def feed(button, values):
        events = []
        for v in values:
            clock[0] += 1
            event = button.feed(v, clock[0] / RATE)
            if event:
                events.append(event)
        return events

    button = _Button("I1", "btn", RATE)
    check("the line at rest is not a press", feed(button, [1] * 10) == [] and button.pressed is False)
    check("one low sample is bounce, not a press", feed(button, [0, 1, 1]) == [])
    first = (clock[0] + 1) / RATE
    events = feed(button, [0, 0, 0, 1])
    check("two low samples are a press, the next high sample its release", [e[0] for e in events] == ["press", "release"], repr(events))
    fields = dict(events[0][1]) if events else {}
    check("the press is dated from the first low sample and names the port", abs(fields.pop("timestamp", 0) - first) < 1e-9 and fields == {"port": "I1", "channel": "btn", "value": 0}, repr(events[0] if events else events))
    check("a long hold is one press", [e[0] for e in feed(button, [0] * 50 + [1])] == ["press", "release"])

    held = _Button("I2", "btn", RATE)
    events = feed(held, [0] * 20 + [1] * 5)
    check("a button already down when acquisition starts is a release, not a press", [e[0] for e in events] == ["release"], repr(events))
    slow = _Button("I1", "btn", 1)
    slow.feed(1, 0.0)
    check("at 1 Hz the hold is one sample", slow.feed(0, 1.0) is not None)

    # In the capture: the press rides on the digital column, whatever the analog ports do.
    digital = {1: lambda i: 0 if 12 <= i < 20 else 1}
    swinging = {0: lambda i: 100 + 700 * (i % 2), 1: lambda i: 0 if 10 <= i < 25 else 900}
    source = _FakeSource("FAKE", {"A1": "acc", "A2": "lux", "I1": "btn"}, rate=RATE, batch=BATCH, levels=swinging, digital=digital)
    source.verbose = False
    source.open()
    records = [r for _ in range(3) for r in source.read()]
    source.close()
    events = [(r["event"], r["channel"]) for r in records if "event" in r]
    check("one press and one release on btn, none on the analog ports however they swing", events == [("press", "btn"), ("release", "btn")], repr(events))
    check("the named line is in every sample under its name", all("btn" in r and "i1" not in r and "i2" in r for r in records if "seq" in r))
    press = next(r for r in records if r.get("event") == "press")
    check("the press follows the sample that made it", records[records.index(press) - 1].get("btn") == 0 and records[records.index(press) - 1]["seq"] == 13 % SEQ_MODULO, repr(records[records.index(press) - 1]))


def test_lost_samples():
    print("lost samples:")
    # Sequence numbers 3 and 4 never arrive: two samples lost in one gap.
    source = _FakeSource("FAKE", LABELS, rate=RATE, batch=BATCH, skip_seq=(3, 4))
    source.verbose = False
    source.open()
    records = source.read()
    source.close()

    gaps = [r for r in records if r.get("event") == "gap"]
    check("one gap reported", len(gaps) == 1, f"{len(gaps)} gaps")
    if gaps:
        check("gap counts both lost samples", gaps[0].get("lost") == 2, repr(gaps[0]))
        check("gap reports expected and actual seq", gaps[0].get("expected") == 3 and gaps[0].get("seq") == 5, repr(gaps[0]))
    check("loss accumulated on the source", source.lost == 2, f"lost={source.lost}")


def test_reconnect():
    print("reconnect:")
    source = _FakeSource("FAKE", LABELS, rate=RATE, batch=BATCH, dropout_after=2, retry_interval=0.05)
    source.verbose = False
    records = []
    source.run(records.append, duration=1.0)

    events = [r["event"] for r in records if "event" in r]
    check("device was reopened", source.connections >= 2, f"{source.connections} connections")
    check("dropout recorded", "disconnected" in events, repr(events))
    check("reconnection recorded", events.count("connected") >= 2, repr(events))
    dropped_at = next((i for i, r in enumerate(records) if r.get("event") == "disconnected"), len(records))
    check("samples resume after the dropout", any("seq" in r for r in records[dropped_at:]))
    check("first device was closed", source.devices[0].closed, "close() not called on the lost device")


def test_duration_is_acquisition_time():
    print("duration:")

    class _SlowToOpen(_FakeSource):
        """A device that takes a second to answer, as the real one does over Bluetooth."""

        def _connect(self):
            time.sleep(1.0)
            return super()._connect()

    source = _SlowToOpen("FAKE", LABELS, rate=RATE, batch=BATCH)
    source.verbose = False
    records = []
    started = time.time()
    source.run(records.append, duration=1.0)
    elapsed = time.time() - started

    samples = [r for r in records if "seq" in r]
    span = samples[-1]["timestamp"] - samples[0]["timestamp"] if len(samples) > 1 else 0.0
    # The second spent opening is given back, so the wall clock is about two seconds and
    # the samples still cover the second that was asked for.
    check("opening does not come out of the capture", elapsed >= 1.9, f"{elapsed:.2f}s elapsed")
    check("a full duration of samples is acquired", span >= 0.8, f"{span:.2f}s of samples")


def test_duration_survives_a_failed_attempt():
    print("duration across a failed connection:")

    class _FailsFirst(_FakeSource):
        """Answers only on the second attempt, as a board often does over Bluetooth."""

        def _connect(self):
            self.attempts = getattr(self, "attempts", 0) + 1
            if self.attempts == 1:
                time.sleep(0.4)
                raise Exception("The computer lost communication with the device.")
            time.sleep(0.4)
            return super()._connect()

    source = _FailsFirst("FAKE", LABELS, rate=RATE, batch=BATCH, retry_interval=0.2)
    source.verbose = False
    records = []
    started = time.time()
    source.run(records.append, duration=1.0)
    elapsed = time.time() - started

    samples = [r for r in records if "seq" in r]
    span = samples[-1]["timestamp"] - samples[0]["timestamp"] if len(samples) > 1 else 0.0
    # 0.4s failing, 0.2s waiting and 0.4s opening are all outside the capture. Charging
    # them left 0.4s of a second's request: four reads instead of ten.
    check("a failed attempt does not come out of the capture", span >= 0.8, f"{span:.2f}s of samples")
    check("the failed attempt still costs wall clock", elapsed >= 1.9, f"{elapsed:.2f}s elapsed")
    check("the failure is recorded", any(r.get("event") == "disconnected" for r in records))


def test_a_device_that_never_answers_gives_up():
    print("a device that never answers:")

    class _Silent(_FakeSource):
        def _connect(self):
            self.attempts = getattr(self, "attempts", 0) + 1
            time.sleep(0.05)
            raise Exception("The computer lost communication with the device.")

    source = _Silent("FAKE", LABELS, rate=RATE, batch=BATCH, retry_interval=0.05)
    source.verbose = False
    records = []
    started = time.time()
    # duration counts acquisition, of which there is none, so only connect_timeout can end this.
    source.run(records.append, duration=1.0, connect_timeout=0.5)
    elapsed = time.time() - started

    check("it gives up rather than retrying forever", elapsed < 5, f"{elapsed:.2f}s elapsed")
    check("it tried more than once", getattr(source, "attempts", 0) >= 2, f"{getattr(source, 'attempts', 0)} attempts")
    check("giving up is in the record", any(r.get("event") == "unreachable" for r in records))
    check("the source says it gave up", source.gave_up)

    # Through the command: the last record says why it ended, and the exit status says it failed,
    # so a launcher can tell a capture that gave up from one that finished.
    import json
    import os
    import tempfile
    real_connect = module.BitalinoSource._connect
    module.BitalinoSource._connect = lambda self: (time.sleep(0.05), (_ for _ in ()).throw(Exception("no answer")))[1]
    real_argv = sys.argv
    with tempfile.TemporaryDirectory() as directory:
        output = os.path.join(directory, "bitalino.jsonl")
        sys.argv = ["VRTsensors-bitalino", "--address", "FAKE", "--channel", "A1=acc", "--connect-timeout", "0.3", "--output", output]
        status = None
        try:
            module.main()
        except SystemExit as e:
            status = e.code
        finally:
            sys.argv = real_argv
            module.BitalinoSource._connect = real_connect
        last = json.loads(open(output).read().splitlines()[-1])
    check("a capture that gave up ends with stopped, reason unreachable", last.get("event") == "stopped" and last.get("reason") == "unreachable", repr(last))
    check("and exits non-zero", status == 1, repr(status))
    check("no samples were invented", not any("seq" in r for r in records))


def test_close_releases_the_port_when_stop_fails():
    print("closing after a dropout:")

    class _StopFails(_FakeDevice):
        """Fails the way a vanished device does: stop() raises, close() still has to run."""

        def stop(self):
            raise Exception("The computer lost communication with the device.")

    class _Vanishing(_FakeSource):
        def _connect(self):
            self.connections += 1
            device = _StopFails(self.channels, 99, ())
            self.devices.append(device)
            return device

    source = _Vanishing("FAKE", LABELS, rate=RATE, batch=BATCH)
    source.verbose = False
    source.open()
    source.close()

    # stop() and close() shared one try block, and stop() is both the first call and the
    # one that raises on a dropout, so the serial port was never released on exactly the
    # path that loses the device - and the next reconnection found it still held.
    check("close() runs although stop() raised", source.devices[0].closed, "close() not called")
    check("the device reference is released", source.device is None)

    # A dropout mid-run takes the same path, so prove it there too rather than only in isolation.
    dropping = _Vanishing("FAKE", LABELS, rate=RATE, batch=BATCH, dropout_after=1, retry_interval=0.05)
    dropping.verbose = False
    dropping.run([].append, duration=0.3)
    check("every device opened during a run is closed", all(d.closed for d in dropping.devices),
          f"{sum(1 for d in dropping.devices if not d.closed)} of {len(dropping.devices)} left open")


def _arrivals(seconds, batch=2, period=1 / RATE, hidden_at=None, seed=7):
    """
    Batches as Bluetooth delivers them: (number of the last sample, arrival, true time of that
    sample), each late by its own latency - mostly a few milliseconds, sometimes a hundred.
    `hidden_at` drops one whole sequence cycle at that many seconds, which the sequence
    number cannot show, so the numbers given stop counting it.
    """
    import random
    rng = random.Random(seed)
    batches, true_index, dropped = [], 0, 0
    t0 = 1_800_000_000.0
    while true_index * period < seconds:
        if hidden_at is not None and not dropped and true_index * period >= hidden_at:
            true_index += SEQ_MODULO
            dropped = SEQ_MODULO
        true_index += batch
        latency = 0.002 + rng.expovariate(1 / 0.015) + (0.12 if rng.random() < 0.01 else 0.0)
        truth = t0 + (true_index - 1) * period
        batches.append((true_index - 1 - dropped, truth + latency, truth))
    # Bluetooth delivers in order: a batch is never ahead of the one before it.
    for i in range(1, len(batches)):
        n, arrival, truth = batches[i]
        batches[i] = (n, max(arrival, batches[i - 1][1]), truth)
    return batches


def test_sample_clock():
    print("sample clock:")
    clock = module._SampleClock(RATE)
    errors, stamps = [], []
    for n, arrival, truth in _arrivals(60):
        check_missing = clock.observe(n, arrival)
        if check_missing:
            break
        stamp = clock.timestamp(n)
        stamps.append(stamp)
        errors.append(stamp - truth)
    settled = errors[len(errors) // 6:]       # after the first ten seconds
    least = min(settled)
    check("no loss found where there is none", not check_missing)
    check("timestamps always increase", all(b > a for a, b in zip(stamps, stamps[1:])))
    # What remains is the link's least latency, the same for every sample, plus little else.
    spread = max(settled) - least
    check("transport jitter is gone from the timestamps", spread < 0.008, f"spread {spread * 1000:.1f} ms")
    check("what stays is at most the least latency the link ever showed", 0 <= least < 0.005, f"{least * 1000:.1f} ms")

    slow = module._SampleClock(RATE)
    for n, arrival, truth in _arrivals(120, period=1.0002 / RATE):
        slow.observe(n, arrival)
    ppm = (slow.period * RATE - 1) * 1e6
    # Measured over the whole connection, so it sharpens with time: within 30 ppm at one
    # minute, within 10 at two, within 1 at five, over seeds tried.
    check("the period follows a crystal 200 ppm slow", abs(ppm - 200) < 15, f"{ppm:.0f} ppm")

    hidden = module._SampleClock(RATE)
    found = []
    for n, arrival, truth in _arrivals(60, hidden_at=30.0):
        missing = hidden.observe(n + sum(m for _, m in found), arrival)
        if missing:
            found.append((truth, missing))
    check("a lost cycle the sequence cannot show is found once, by timing", [m for _, m in found] == [SEQ_MODULO], repr(found))
    check("and found within a few seconds", found and 30.0 <= found[0][0] - 1_800_000_000.0 < 33.0, repr(found))


def test_wall_clock_step_is_not_loss():
    for step in (0.5, 1.0):
        _wall_clock_step(step)


def _wall_clock_step(step):
    print(f"a wall clock stepped {step:+.1f} s during a connection:")
    # A time synchronisation steps the wall clock; the monotonic clock is untouched. The sample
    # clock read the step as a delivery delay and reported cycles of 16 samples lost (48 for
    # +0.5 s, in review) where none were.
    real_time = module.time

    class Stepped:
        start = real_time.time()

        @staticmethod
        def time():
            now = real_time.time()
            return now + (step if now - Stepped.start > 3.0 else 0.0)

        monotonic = staticmethod(real_time.monotonic)
        perf_counter = staticmethod(real_time.perf_counter)
        sleep = staticmethod(real_time.sleep)

    module.time = Stepped
    try:
        source = _FakeSource("FAKE", LABELS, rate=RATE, batch=BATCH)
        source.verbose = False
        records = []
        source.run(records.append, duration=9.0)
    finally:
        module.time = real_time
    samples = [r["timestamp"] for r in records if "seq" in r]
    steps = [b - a for a, b in zip(samples, samples[1:])]
    check("the wall clock had been stepped by the end", abs(Stepped.time() - real_time.time() - step) < 0.01)
    check("no loss is reported where no sample was lost", source.lost == 0 and not any(r.get("event") == "gap" for r in records),
          f"lost={source.lost}")
    check("the samples stay evenly spaced across the step", steps and max(steps) < 2.5 / RATE and min(steps) > 0, f"{min(steps):.4f}..{max(steps):.4f}")


def test_capture_records():
    print("capture records:")
    import io
    import json as _json
    from VRTsensors import FORMAT, capture_header, record_sink
    header = capture_header("bitalino", device="COM5", clock="sample-count")
    check("the header names its format, version, command and host",
          header.get("event") == "capture" and header.get("format") == FORMAT and {"version", "command", "host"} <= set(header), repr(header))
    sent, output = [], io.StringIO()
    sink = record_sink(output, sent.append)
    for event in ("capture", "clock", "connected", None):
        sink({"sensor": "bitalino", "timestamp": 1.0, **({"event": event} if event else {})})
    check("everything is written to the file", len(output.getvalue().splitlines()) == 4)
    check("capture and clock records are not sent to a realtime consumer",
          [_json.loads(line).get("event") for line in sent] == ["connected", None], repr(sent))
    check("the header names the code's commit and the dependency versions",
          isinstance(header.get("revision"), str) and len(header["revision"]) == 40 and "pylsl" in header.get("dependencies", {}), repr(header))
    import os
    import tempfile
    here = os.getcwd()
    with tempfile.TemporaryDirectory() as directory:
        os.chdir(directory)
        try:
            check("without participant.json the header names no participant", "participant" not in capture_header("bitalino"))
            with open("participant.json", "w") as fp:
                fp.write('{"participant": "P07"}')
            with open("session.json", "w") as fp:
                fp.write('{"study": "pilot", "session": "S3"}')
            named = capture_header("bitalino")
            check("participant.json and session.json land in the header",
                  named.get("participant") == "P07" and named.get("session") == {"study": "pilot", "session": "S3"}, repr(named))
            with open("participant.json", "w") as fp:
                fp.write('{"name": "P07"}')
            try:
                capture_header("bitalino")
                check("a participant.json without a participant is refused", False)
            except RuntimeError:
                check("a participant.json without a participant is refused", True)
        finally:
            os.chdir(here)


def test_parse_channel():
    print("channel specifications:")
    check("A1=eda names the port", parse_channel("A1=eda") == ("A1", "eda"))
    check("case and spaces are forgiven", parse_channel(" a3 = lux ") == ("A3", "lux"))
    check("the button on a digital input", parse_channel("I1=btn") == ("I1", "btn"))
    check("named ports only, in the order given", channel_labels(["A2=acc", "I1=btn", "A1=eda"]) == {"A2": "acc", "I1": "btn", "A1": "eda"})
    refused = {
        "no port at all": [],
        "a bare port": ["A3"],
        "A7": ["A7=x"],
        "the button on an analog port": ["A2=btn"],
        "something other than the button on a digital input": ["I1=acc", "A1=eda"],
        "a port named twice": ["A3=eda", "A3=lux"],
        "two ports with one name": ["A1=eda", "A2=eda"],
        "a record field as a name": ["A1=timestamp"],
        "only a digital input": ["I1=btn"],
    }
    for label, specs in refused.items():
        try:
            channel_labels(specs)
            check(f"{label} is refused", False, repr(specs))
        except RuntimeError as e:
            check(f"{label} is refused", True, str(e))


def test_setup(tmpdir, skip_seq=()):
    print("setup:")
    import builtins
    import json
    import os
    configdir = os.path.join(tmpdir, "with-gap" if skip_seq else "self")
    os.makedirs(configdir)
    filename = os.path.join(configdir, "sensors.json")
    with open(filename, "w") as fp:
        json.dump(["VRTsensors-emotibit --udp 127.0.0.1:9010 --output emotibit.jsonl",
                   "VRTsensors-bitalino --address COM9 --channel A1=acc --channel A2=btn --batch 2 --udp 127.0.0.1:9010 --output bitalino.jsonl"], fp)
    # A1 an accelerometer being waved, A2 a steady module, the rest empty; the button on I1.
    levels = {0: lambda i: 300 + 400 * (i % 2), 1: 512}
    prompts = []
    answers = ["acc", "lux", "", "", "", "", "btn", ""]
    real_source, real_input, real_watch = module.BitalinoSource, builtins.input, module.WATCH
    module.BitalinoSource = lambda address, labels, **kw: _FakeSource(address, labels, levels=levels, skip_seq=skip_seq, **kw)
    builtins.input = lambda prompt: (prompts.append(prompt), answers.pop(0))[1]
    module.WATCH = 0.3
    try:
        setup("FAKE", configdir)
    finally:
        module.BitalinoSource, builtins.input, module.WATCH = real_source, real_input, real_watch
    # It writes a sensors.local.json rather than the sensors.json it was given: a machine's
    # own wiring does not belong in a checked-in example, which is how one machine's EmotiBit
    # quietly left the two-machine configuration.
    local = os.path.join(configdir, "sensors.local.json")
    check("a machine's wiring goes in its own file", os.path.exists(local))
    check("the example it read is left as it was", json.load(open(filename))[1].endswith("--output bitalino.jsonl"))
    commands = json.load(open(local))
    check("every port is asked about, the six analog ones then the two digital ones", [p.split()[0] for p in prompts] == ANALOG_PORTS + ["I1", "I2"], repr(prompts))
    check("the prompt shows what the port reads", [p.strip()[:len(x)] for p, x in zip(prompts, ("A1 reads 300-700.", "A2 reads 512.", "A3 reads 2."))] == ["A1 reads 300-700.", "A2 reads 512.", "A3 reads 2."] and prompts[6].strip().startswith("I1 reads 1."), repr(prompts))
    check("the other command is untouched", commands[0].startswith("VRTsensors-emotibit"))
    # The address is the one setup just connected through, not the example's: a port number
    # belongs to the machine the board is paired with, so the example's COM9 is someone else's.
    check("the bitalino line takes the address setup used, keeps its other options and names what was answered",
          commands[1] == "VRTsensors-bitalino --address FAKE --batch 2 --udp 127.0.0.1:9010 --output bitalino.jsonl --channel A1=acc --channel A2=lux --channel I1=btn", commands[1])
    specs = [a.strip() for a in commands[1].split("--channel ")[1:]]
    check("the names it wrote are accepted back", channel_labels(specs) == {"A1": "acc", "A2": "lux", "I1": "btn"}, repr(specs))
    # Run again: the local file now exists, so that is the one read and the one rewritten.
    answers[:] = ["eda", "", "", "", "", "", "btn", ""]
    module.BitalinoSource = lambda address, labels, **kw: _FakeSource(address, labels, levels=levels, skip_seq=skip_seq, **kw)
    builtins.input = lambda prompt: (prompts.append(prompt), answers.pop(0))[1]
    module.WATCH = 0.3
    try:
        setup("FAKE", configdir)
    finally:
        module.BitalinoSource, builtins.input, module.WATCH = real_source, real_input, real_watch
    again = json.load(open(local))
    check("a second setup reads and rewrites the local file", again[1].endswith("--channel A1=eda --channel I1=btn"), again[1])
    check("and still leaves the example alone", json.load(open(filename))[1].endswith("--output bitalino.jsonl"))


if __name__ == "__main__":
    import tempfile
    test_parse_channel()
    test_sample_clock()
    test_wall_clock_step_is_not_loss()
    test_capture_records()
    test_records()
    test_independence()
    test_button()
    test_lost_samples()
    test_reconnect()
    test_duration_is_acquisition_time()
    test_duration_survives_a_failed_attempt()
    test_a_device_that_never_answers_gives_up()
    test_close_releases_the_port_when_stop_fails()
    with tempfile.TemporaryDirectory() as tmpdir:
        test_setup(tmpdir)
        test_setup(tmpdir, skip_seq=(3,))
    print(f"\n{'FAILED: ' + str(failures) + ' check(s)' if failures else 'all checks passed'}")
    sys.exit(1 if failures else 0)
