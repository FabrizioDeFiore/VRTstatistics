#!/usr/bin/env python3
"""
Test for VRTsensors.emotibit against LSL streams published from this process.

This exercises stream selection, clock correction, timestamps, loss and stale detection
with deterministic in-memory LSL test doubles. Hardware and network discovery remain
separate integration checks because multicast behaviour depends on the host firewall.

  python test_emotibit.py

What it does NOT cover, and only the hardware can: that EmotiBit Oscilloscope is
relaying anything at all, that its lslOutputSettings.json patchcords name the streams
you expect, and that the device stays on the WiFi network.
"""
import json
import os
import sys
import tempfile
import time

import pylsl
from pylsl.util import LostError, TimeoutError as LSLTimeoutError

import VRTsensors.emotibit as emotibit
from VRTsensors.emotibit import EmotibitSource
from VRTsensors.validate import Capture

RATE = 15.0                      # EmotiBit publishes EDA at 15Hz
RESOLVE_WAIT = 1.0

failures = 0
stream_infos = []
inlets_made = []


class TestInfo:
    def __init__(self, name, channels, rate, source_id, uid=None):
        self._name = name
        self._channels = channels
        self._rate = rate
        self._source_id = source_id
        self._uid = uid or source_id
        self.queue = []
        self.lost = False                   # the outlet has gone away
        self.correction_error = None        # what time_correction raises, if anything
        self.correction_cost = None         # called with the timeout of every time_correction call

    def name(self): return self._name
    def type(self): return "EmotiBit"
    def channel_count(self): return self._channels
    def nominal_srate(self): return self._rate
    def source_id(self): return self._source_id
    def uid(self): return self._uid


class TestOutlet:
    def __init__(self, info):
        self.info = info

    def push_sample(self, values):
        self.info.queue.append((values, pylsl.local_clock()))


class TestInlet:
    def __init__(self, info, recover=True):
        self.info = info
        self.recover = recover
        inlets_made.append(self)

    def open_stream(self, timeout): pass
    def close_stream(self): pass

    def time_correction(self, timeout):
        if self.info.correction_cost is not None:
            self.info.correction_cost(timeout)
        if self.info.correction_error is not None:
            raise self.info.correction_error
        return 0.0

    def pull_chunk(self, timeout):
        if self.info.lost:
            raise LostError("The stream has been lost.")
        queued, self.info.queue = self.info.queue, []
        return [item[0] for item in queued], [item[1] for item in queued]


emotibit.pylsl.StreamInlet = TestInlet


def check(label: str, condition: bool, detail: str = "") -> None:
    global failures
    if condition:
        print(f"  ok    {label}")
    else:
        failures += 1
        print(f"  FAIL  {label}{': ' + detail if detail else ''}")


def outlet(name: str, channels: int = 1, rate: float = RATE, source_id: str = "", uid: str = "") -> TestOutlet:
    info = TestInfo(name, channels, rate, source_id or name, uid or None)
    stream_infos.append(info)
    return TestOutlet(info)


def opened(source: EmotibitSource):
    """Open local test streams without depending on multicast discovery."""
    source._resolve = lambda: stream_infos
    return source.open()


def test_records():
    print("records:")
    out = outlet("VRTsensorsTestEA")
    source = EmotibitSource(["VRTsensorsTestEA"], resolve_wait=RESOLVE_WAIT)
    source.verbose = False
    connected = opened(source)

    check("stream resolved and opened", len(source.inlets) == 1, f"{list(source.inlets)}")
    if not connected:
        print("  (no stream resolved; skipping the rest of this test)")
        return
    check("connect event", connected[0].get("event") == "connected", repr(connected[0]))
    check("connect event carries the nominal rate", connected[0].get("rate") == RATE, repr(connected[0]))

    sent = 5
    for value in range(sent):
        out.push_sample([float(value)])
        time.sleep(1 / RATE)
    time.sleep(0.2)
    records = source.read()
    source.close()

    samples = [r for r in records if "value" in r]
    check("one record per sample", len(samples) == sent, f"{len(samples)} of {sent}")
    check("values arrive intact", [r["value"] for r in samples] == [float(v) for v in range(sent)], repr([r.get("value") for r in samples]))
    check("stream named on every record", all(r.get("stream") == "VRTsensorsTestEA" for r in samples))
    check("sensor identified", all(r.get("sensor") == "emotibit" for r in samples))
    check("timestamps increase", all(b["timestamp"] > a["timestamp"] for a, b in zip(samples, samples[1:])))
    if samples:
        drift = abs(samples[-1]["timestamp"] - time.time())
        check("timestamps are wall-clock, not LSL's arbitrary epoch", drift < 5.0, f"{drift:.1f}s from now")
    check("no loss reported", source.losses == 0, f"losses={source.losses}")


def test_stream_selection():
    print("stream selection:")
    wanted = outlet("VRTsensorsTestWanted")
    ignored = outlet("VRTsensorsTestIgnored")
    source = EmotibitSource(["VRTsensorsTestWanted"], resolve_wait=RESOLVE_WAIT)
    source.verbose = False
    opened(source)
    source.close()

    opened_names = [source.stream_names[key] for key in source.samples]
    check("the named stream was found", "VRTsensorsTestWanted" in opened_names, repr(opened_names))
    check("the unnamed stream was left alone", "VRTsensorsTestIgnored" not in opened_names, repr(opened_names))
    del wanted, ignored


def test_multichannel():
    print("multichannel:")
    out = outlet("VRTsensorsTestIMU", channels=3, rate=25.0)
    source = EmotibitSource(["VRTsensorsTestIMU"], resolve_wait=RESOLVE_WAIT)
    source.verbose = False
    if not opened(source):
        print("  (no stream resolved; skipping)")
        return
    out.push_sample([1.0, 2.0, 3.0])
    time.sleep(0.2)
    records = source.read()
    source.close()

    samples = [r for r in records if "value_0" in r]
    check("one record for the sample", len(samples) == 1, f"{len(samples)} records")
    if samples:
        check("every channel emitted by index", (samples[0].get("value_0"), samples[0].get("value_1"), samples[0].get("value_2")) == (1.0, 2.0, 3.0), repr(samples[0]))


def test_duplicate_names():
    print("duplicate stream names:")
    first = outlet("VRTsensorsTestShared", source_id="participant-a")
    second = outlet("VRTsensorsTestShared", source_id="participant-b")
    source = EmotibitSource(["VRTsensorsTestShared"], resolve_wait=RESOLVE_WAIT)
    source.verbose = False
    connected = opened(source)
    check("both participants opened", len(connected) == 2, repr(connected))
    first.push_sample([1.0])
    second.push_sample([2.0])
    time.sleep(0.2)
    samples = [record for record in source.read() if "value" in record]
    source.close()
    check("both participants retained", {record.get("source_id") for record in samples} == {"participant-a", "participant-b"}, repr(samples))


def test_bound_to_its_device():
    print("two participants' EmotiBits, both visible to both computers:")
    # LSL shows every outlet on the network to every computer, under the same stream names.
    # Each computer's capture is bound to its own device; the other is never opened, before
    # or after both Oscilloscopes restart (new uids, same device IDs).
    a = outlet("VRTsensorsTestEDA", source_id="MD-A", uid="a-1")
    b = outlet("VRTsensorsTestEDA", source_id="MD-B", uid="b-1")
    roles = {"MD-A": EmotibitSource(["VRTsensorsTestEDA"], sources=["MD-A"], resolve_wait=RESOLVE_WAIT),
             "MD-B": EmotibitSource(["VRTsensorsTestEDA"], sources=["MD-B"], resolve_wait=RESOLVE_WAIT)}
    for device, source in roles.items():
        source.verbose = False
        connected = opened(source)
        check(f"{device}'s capture opens only {device}", [r.get("source_id") for r in connected] == [device], repr(connected))
    a.push_sample([1.0])
    b.push_sample([2.0])
    for device, source in roles.items():
        samples = [r for r in source.read() if "value" in r]
        check(f"{device}'s capture records only {device}", samples and {r["source_id"] for r in samples} == {device}, repr(samples))
    for old in (a, b):
        old.info.lost = True
        stream_infos.remove(old.info)
    for source in roles.values():
        source.read()
    a = outlet("VRTsensorsTestEDA", source_id="MD-A", uid="a-2")
    b = outlet("VRTsensorsTestEDA", source_id="MD-B", uid="b-2")
    for device, source in roles.items():
        reconnected = source.open()
        check(f"after the restart {device}'s capture reopens only {device}", [r.get("source_id") for r in reconnected] == [device], repr(reconnected))
    a.push_sample([3.0])
    b.push_sample([4.0])
    for device, source in roles.items():
        samples = [r for r in source.read() if "value" in r]
        check(f"after the restart {device}'s capture records only {device}", samples and {r["source_id"] for r in samples} == {device}, repr(samples))
        check(f"{device}'s capture has seen one device", source.devices == {device}, repr(source.devices))
        source.close()
    stream_infos.remove(a.info)
    stream_infos.remove(b.info)


def test_loss():
    print("loss detection:")
    out = outlet("VRTsensorsTestLoss")
    source = EmotibitSource(["VRTsensorsTestLoss"], resolve_wait=RESOLVE_WAIT, loss_window=1.0)
    source.verbose = False
    if not opened(source):
        print("  (no stream resolved; skipping)")
        return
    # A third of the declared rate for rather more than one window: two thirds of the
    # samples the rate promises never arrive.
    deadline = time.time() + 1.4
    while time.time() < deadline:
        out.push_sample([0.0])
        time.sleep(3 / RATE)
    records = source.read()
    source.close()

    losses = [r for r in records if r.get("event") == "loss"]
    check("loss reported", len(losses) == 1, f"{len(losses)} loss events")
    check("loss counted on the source", source.losses == 1, f"losses={source.losses}")
    if losses:
        check("loss estimates the missing samples", losses[0].get("missing", 0) >= 1, repr(losses[0]))


def test_burst_delivery_is_not_loss():
    print("burst delivery is not loss:")
    out = outlet("VRTsensorsTestBurst")
    source = EmotibitSource(["VRTsensorsTestBurst"], resolve_wait=RESOLVE_WAIT, loss_window=1.0)
    source.verbose = False
    if not opened(source):
        print("  (no stream resolved; skipping)")
        return
    # The shape the hardware actually produces: a packet every ~100ms whose samples all
    # carry one arrival time. Above the declared rate overall, evenly spaced nowhere.
    # This is the regression that spacing-based detection failed - see LOSS_WINDOW.
    deadline = time.time() + 1.5
    while time.time() < deadline:
        out.push_sample([0.0])
        out.push_sample([0.0])
        time.sleep(0.1)
    source.read()
    source.close()

    check("full-rate bursts are not reported as loss", source.losses == 0, f"losses={source.losses}")


def test_event_driven_stream_is_never_stale():
    print("event-driven streams are not judged stale:")
    out = outlet("VRTsensorsTestEvent", rate=pylsl.IRREGULAR_RATE)
    source = EmotibitSource(["VRTsensorsTestEvent"], stale_after=0.3, resolve_wait=RESOLVE_WAIT)
    source.verbose = False
    if not opened(source):
        print("  (no stream resolved; skipping)")
        return
    time.sleep(0.5)
    stale = [r for r in source.read() if r.get("event") == "stale"]
    source.close()
    check("silence on a rateless stream is not a fault", not stale, f"{len(stale)} stale events")
    del out


def test_stale():
    print("stale detection:")
    out = outlet("VRTsensorsTestStale")
    source = EmotibitSource(["VRTsensorsTestStale"], stale_after=0.3, resolve_wait=RESOLVE_WAIT)
    source.verbose = False
    if not opened(source):
        print("  (no stream resolved; skipping)")
        return
    time.sleep(0.5)
    stale = [r for r in source.read() if r.get("event") == "stale"]
    check("silence reported once", len(stale) == 1, f"{len(stale)} stale events")
    check("nothing further while still silent", not [r for r in source.read() if r.get("event") == "stale"])

    out.push_sample([1.0])
    time.sleep(0.2)
    resumed = [r for r in source.read() if r.get("event") == "resumed"]
    source.close()
    check("recovery reported", len(resumed) == 1, f"{len(resumed)} resumed events")


def test_clock_correction_timeout():
    print("a sender that does not answer the clock probe:")
    out = outlet("VRTsensorsTestSlowClock")
    # pylsl's own TimeoutError, which is not the built-in one: catching only the built-in
    # let this end the capture.
    out.info.correction_error = LSLTimeoutError("The operation failed due to a timeout.")
    source = EmotibitSource(["VRTsensorsTestSlowClock"], resolve_wait=RESOLVE_WAIT)
    source.verbose = False
    source.correction_wait = 0.0        # give up on the first answer at once
    try:
        connected = opened(source)
        check("the stream still opens", len(connected) == 1, repr(connected))
        check("its correction is recorded as zero", connected and connected[0].get("time_correction") == 0.0, repr(connected))
        check("and recorded as not valid", connected and connected[0].get("time_correction_valid") is False, repr(connected))
        # The review's case: written to a file, these samples passed --study as corrected.
        out.push_sample([1.0])
        records = [{"sensor": "emotibit", "stream": "", "timestamp": time.time() - 1, "event": "capture", "format": 2,
                    "participant": "P01", "revision": "a" * 40, "dirty": False, "sources": ["VRTsensorsTestSlowClock"]}]
        records += connected + source.read()
        check("the sender is asked again while it does not answer", source.retry_corrections() == [] and source.uncorrected)
        out.info.correction_error = None
        answered = source.retry_corrections()
        check("its answer is recorded when it comes", [(r.get("event"), r.get("time_correction_valid")) for r in answered] == [("time_correction", True)], repr(answered))
        check("and it is not asked again", not source.uncorrected and source.retry_corrections() == [])
        out.push_sample([2.0])
        records += answered + source.read()
        records.append({"sensor": "emotibit", "stream": "", "timestamp": time.time(), "event": "stopped", "reason": "finished"})
        with tempfile.TemporaryDirectory() as workdir:
            path = os.path.join(workdir, "emotibit.jsonl")
            with open(path, "w") as fp:
                fp.writelines(json.dumps(r) + "\n" for r in records)
            findings = Capture(path).study_findings()
        check("--study counts the one sample dated without correction", any("1 samples dated without LSL clock correction" in f for f in findings), repr(findings))
    except Exception as e:
        check("the stream still opens", False, f"{type(e).__name__}: {e}")
    finally:
        source.close()
        stream_infos.remove(out.info)


class FakeTime:
    """Time that moves only when something sleeps or waits: a stall becomes exact and instant."""
    def __init__(self):
        self.now = 1_000_000.0
    def time(self): return self.now
    def sleep(self, seconds): self.now += seconds


def test_unanswered_corrections_do_not_stop_reading():
    # Review follow-up, 2026-10-08: nineteen streams whose sender never answers the
    # clock probe, each probe costing its timeout. Retrying them in turn stopped every read
    # for 9.5 s at a time, so Unity saw nothing for most of a 30 s run.
    print("nineteen senders that never answer the clock probe:")
    clock = FakeTime()
    real_time = emotibit.time
    emotibit.time = clock
    infos = []
    for i in range(19):
        info = TestInfo(f"VRTsensorsTestMute{i}", 1, RATE, "MD-MUTE", f"uid-mute-{i}")
        info.correction_error = LSLTimeoutError("The operation failed due to a timeout.")
        info.correction_cost = lambda timeout: clock.sleep(timeout)
        infos.append(info)
    source = EmotibitSource(resolve_interval=5.0, resolve_wait=RESOLVE_WAIT)
    source.verbose = False
    source.correction_wait = 2.0
    reads = []
    real_read = source.read
    def timed_read():
        reads.append(clock.now)
        return real_read()
    source.read = timed_read
    records = []
    try:
        source._resolve = lambda: infos
        records += source.open()
        source._resolve = lambda: []
        start = clock.now
        source.run(records.append, duration=30.0)
        gaps = [b - a for a, b in zip([start] + reads, reads)]
        connected = [r for r in records if r.get("event") == "connected"]
        check("reads keep their cadence: no gap over 0.1 s", gaps and max(gaps) <= 0.1, f"longest gap {max(gaps or [0]):.2f}s")
        check("every stream is released uncorrected after its wait", len(connected) == 19 and
              all(r.get("time_correction_valid") is False for r in connected), f"{len(connected)} connected")
        check("and released within the wait", connected and all(r["timestamp"] <= start + 2.0 for r in connected) and
              reads and min(reads) - start <= 0.1)
    finally:
        emotibit.time = real_time
        source.close()


def test_held_until_the_correction_answers():
    print("a sender whose clock probe answers late:")
    out = outlet("VRTsensorsTestLateClock")
    out.info.correction_error = LSLTimeoutError("The operation failed due to a timeout.")
    source = EmotibitSource(["VRTsensorsTestLateClock"], resolve_wait=RESOLVE_WAIT)
    source.verbose = False
    source.correction_wait = 60.0
    try:
        check("no connected event while it is waited for", opened(source) == [])
        out.push_sample([1.0])
        check("and nothing is read from it", source.read() == [])
        out.info.correction_error = None
        connected = source.settle_corrections()
        check("its connected event carries a valid correction once it answers",
              [(r.get("event"), r.get("time_correction_valid")) for r in connected] == [("connected", True)], repr(connected))
        check("the held sample arrives, once", len([r for r in source.read() if "value" in r]) == 1)
    finally:
        source.close()
        stream_infos.remove(out.info)


def mute_clock(info: TestInfo, probes: list) -> None:
    """A sender that never answers the clock probe; every probe's timeout goes into probes."""
    info.correction_error = LSLTimeoutError("The operation failed due to a timeout.")
    info.correction_cost = probes.append


def held_at_the_end(label: str, interrupt: bool) -> None:
    # Review, 2026-10-10: a stream still held for its correction when the capture
    # ends. Closing it unread threw its buffered samples away.
    print(label)
    clock = FakeTime()
    real_time = emotibit.time
    emotibit.time = clock
    probes = []
    late = TestInfo("VRTsensorsTestHeldEDA", 1, RATE, "MD-1", "uid-held")
    mute_clock(late, probes)
    source = EmotibitSource(resolve_interval=5.0, resolve_wait=RESOLVE_WAIT)
    source.verbose = False
    source.correction_wait = 2.0
    if interrupt:
        # Ctrl-C, or a launcher closing standard input, arrives during the poll sleep.
        def sleep(seconds):
            raise KeyboardInterrupt
        clock.sleep = sleep
    records = []
    interrupted = False
    try:
        source._resolve = lambda: [late]
        records += source.open()
        source._resolve = lambda: []
        late.queue.append(([1.0], pylsl.local_clock()))
        try:
            source.run(records.append, duration=0.1)
        except KeyboardInterrupt:
            interrupted = True
        samples = [r for r in records if "value" in r]
        connected = [r for r in records if r.get("event") == "connected"]
        check("the interruption still reaches the caller", interrupted == interrupt)
        check("the held sample is recorded, once", len(samples) == 1 and not late.queue, f"{len(samples)} recorded, {len(late.queue)} left")
        check("its stream is connected first, correction marked not valid",
              [r.get("time_correction_valid") for r in connected] == [False] and records.index(connected[0]) < records.index(samples[0]),
              repr(connected))
        check("no clock probe waits while stopping", probes and all(t == 0.0 for t in probes), repr(probes))
    finally:
        emotibit.time = real_time
        source.close()


def test_held_stream_kept_when_a_short_capture_ends():
    held_at_the_end("a stream still held when a short capture ends:", interrupt=False)


def test_held_stream_kept_when_interrupted():
    held_at_the_end("a stream still held when the capture is interrupted:", interrupt=True)


def test_reconnected_stream_kept_beside_an_established_one():
    print("a stream back near the end of a capture, beside one that never left:")
    clock = FakeTime()
    real_time = emotibit.time
    emotibit.time = clock
    probes = []
    steady = TestInfo("VRTsensorsTestSteady", 1, RATE, "MD-1", "uid-steady")
    returned = TestInfo("VRTsensorsTestBack", 1, RATE, "MD-1", "uid-back-2")
    mute_clock(returned, probes)
    source = EmotibitSource(resolve_interval=5.0, resolve_wait=RESOLVE_WAIT)
    source.verbose = False
    source.correction_wait = 2.0
    records = []
    try:
        source._resolve = lambda: [steady]
        records += source.open()
        steady.queue.append(([1.0], pylsl.local_clock()))
        records += source.read()
        # The other stream's successor appears; its sender does not answer the probe yet.
        source._resolve = lambda: [steady, returned]
        records += source.open()
        source._resolve = lambda: []
        steady.queue.append(([2.0], pylsl.local_clock()))
        returned.queue.append(([3.0], pylsl.local_clock()))
        source.run(records.append, duration=0.1)
        values = sorted(r["value"] for r in records if "value" in r)
        back = [r for r in records if r.get("event") == "connected" and r.get("stream_uid") == "uid-back-2"]
        check("every sample of both streams is recorded, once", values == [1.0, 2.0, 3.0], repr(values))
        check("the returned stream says its correction is not valid", [r.get("time_correction_valid") for r in back] == [False], repr(back))
        check("no clock probe waits while stopping", probes and all(t == 0.0 for t in probes), repr(probes))
    finally:
        emotibit.time = real_time
        source.close()


def test_restarted_stream_is_recorded_once():
    print("a stream that is lost and comes back:")
    first = outlet("VRTsensorsTestRestart", source_id="MD-1", uid="uid-before")
    source = EmotibitSource(["VRTsensorsTestRestart"], resolve_wait=RESOLVE_WAIT)
    source.verbose = False
    opened(source)
    check("inlets are opened without liblsl recovery", inlets_made and not inlets_made[-1].recover)
    first.push_sample([1.0])
    before = [r for r in source.read() if "value" in r]

    # The Oscilloscope restarts: the old outlet is gone, a new one with a new uid appears.
    first.info.lost = True
    stream_infos.remove(first.info)
    lost = source.read()
    disconnected = [r for r in lost if r.get("event") == "disconnected"]
    check("the loss is recorded as a disconnection", len(disconnected) == 1 and disconnected[0].get("stream_uid") == "uid-before", repr(lost))
    check("the lost inlet is closed", "uid-before" not in source.inlets, repr(list(source.inlets)))

    second = outlet("VRTsensorsTestRestart", source_id="MD-1", uid="uid-after")
    reconnected = source.open()
    check("its successor is opened once", [r.get("stream_uid") for r in reconnected] == ["uid-after"], repr(reconnected))
    second.push_sample([2.0])
    after = [r for r in source.read() if "value" in r]
    source.close()
    check("each sample is recorded once", len(before) == 1 and len(after) == 1, f"{len(before)} then {len(after)}")
    stream_infos.remove(second.info)


if __name__ == "__main__":
    test_clock_correction_timeout()
    test_unanswered_corrections_do_not_stop_reading()
    test_held_until_the_correction_answers()
    test_held_stream_kept_when_a_short_capture_ends()
    test_held_stream_kept_when_interrupted()
    test_reconnected_stream_kept_beside_an_established_one()
    test_restarted_stream_is_recorded_once()
    test_records()
    test_stream_selection()
    test_multichannel()
    test_duplicate_names()
    test_bound_to_its_device()
    test_loss()
    test_burst_delivery_is_not_loss()
    test_stale()
    test_event_driven_stream_is_never_stale()
    print(f"\n{'FAILED: ' + str(failures) + ' check(s)' if failures else 'all checks passed'}")
    sys.exit(1 if failures else 0)
