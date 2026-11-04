from __future__ import annotations
import argparse
import json
import sys
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import bitalino

from . import (CLOCK_INTERVAL, SensorRecord, capture_header, clock_fields, open_capture, record_sink,
               sensors_path, sensors_write_path, stop_on_stdin_eof, udp_sink)

__all__ = ["BitalinoSource"]

# The device firmware accepts only these acquisition rates.
SAMPLING_RATES = [1, 10, 100, 1000]

# Column layout of bitalino.BITalino.read(): sequence number, the digital lines I1, I2,
# O1, O2, then one column per acquired analog port in ascending port order.
SEQ_COLUMN = 0
DIGITAL_COLUMNS = {"I1": 1, "I2": 2}
FIRST_ANALOG_COLUMN = 5

# The first four acquired analog ports arrive as 10-bit counts, a fifth and a sixth as
# 6-bit: the frame has no room for more. So the resolution of a port depends on how many
# are acquired, not on which, and the capture states it per port in the connected event.
BITS = [10, 10, 10, 10, 6, 6]

# The sequence number is four bits wide and wraps, so a detected gap is a lower
# bound on the number of samples actually lost, never an exact count.
SEQ_MODULO = 16

# The ports, and what may be named on them. The board carries no module identification -
# a port is a number and nothing else - so the name given on the command line is the only
# source of what a port is, and only named analog ports are acquired. The two digital
# lines cost nothing - the frame carries them whether or not anything is plugged in -
# so they are always recorded, under their names when named and as i1/i2 when not.
#
# The button is the one module with a fixed place: a digital input. PLUX's BTN datasheet
# gives its output as 0 V and shows its response on a digital input, and the digital lines
# read 1 with nothing attached in every capture so far, so a press is the line going low.
# On an analog port it has no pull-up, floats when released and reads a few percent of
# whatever the port before it carries: on the development board a released button on A2
# read 0.030 x A1 - 1.65 while A1 carried an accelerometer (R2 0.96 over 245k samples), and
# a light sensor on A2 read the same line. No threshold can tell such a press
# from an accelerometer swing, so analog presses are not detected at all.
ANALOG_PORTS = [f"A{n}" for n in range(1, 7)]
DIGITAL_PORTS = ["I1", "I2"]
ANALOG_MODULES = ["acc", "eda", "emg", "eeg", "lux"]
BUTTON = "btn"

# A press is the digital line held low for at least this long, to ride out contact
# bounce: two samples at 100 Hz. The bounce of this button has not been measured.
PRESS_HOLD = 0.02   # seconds

# How long run() keeps trying a device that will not answer before it gives up. Opening
# over Bluetooth takes about five seconds to fail and retry_interval waits two more, so
# this is roughly eight attempts. It has to exist separately from --duration, which counts
# acquisition and therefore cannot expire while there is none.
CONNECT_TIMEOUT = 60.0   # seconds

# Field names a port cannot take, because every record already carries them.
RESERVED = {"sensor", "device", "timestamp", "seq", "event", "i1", "i2"}


class _Button:
    """
    Presses on one digital line, detected here on every sample so that they are in the
    capture as events rather than left to a consumer that may miss an edge.

    Low for PRESS_HOLD is a press, dated from its first low sample; the first high sample
    after that is the release. A line already low when acquisition starts is not a press.
    """

    def __init__(self, port: str, channel: str, rate: int) -> None:
        self.port = port
        self.channel = channel
        self.rate = rate
        self.pressed: Optional[bool] = None     # unknown until the first sample
        self._hold = max(1, int(PRESS_HOLD * rate))
        self._low = 0                           # consecutive low samples

    def feed(self, value: int, timestamp: float) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Take one sample; return the (event, fields) it causes, if any, with its own timestamp."""
        if self.pressed is None:
            self.pressed = value == 0
        self._low = self._low + 1 if value == 0 else 0
        if self._low == self._hold and not self.pressed:
            self.pressed = True
            return "press", {"port": self.port, "channel": self.channel, "value": value, "timestamp": timestamp - (self._hold - 1) / self.rate}
        if value != 0 and self.pressed:
            self.pressed = False
            return "release", {"port": self.port, "channel": self.channel, "value": value, "timestamp": timestamp}
        return None


def wall_epoch(edges: int = 3) -> float:
    """
    The wall clock minus the performance counter, at this moment.

    The performance counter is fine-grained and never adjusted, so the sample clock runs on
    it. On Windows the wall clock moves in steps of up to 15.6 ms, so one reading of it can
    be that much behind, and every sample of the connection with it. The step edges are
    exact: wait for the wall clock to move, read the counter, and keep the median of a few.
    """
    found = []
    for _ in range(edges):
        start = time.time()
        give_up = time.perf_counter() + 0.05
        while True:
            now, counter = time.time(), time.perf_counter()
            if now != start or counter > give_up:
                break
        found.append(now - counter)
    return sorted(found)[len(found) // 2]


class _SampleClock:
    """
    Dates samples by their number since the connection opened, not by when their batch arrived.

    The board samples on its own crystal, so sample n was taken at t0 + n * period. Neither is
    known, and batches reach us late by however long Bluetooth held them: on real captures
    up to 175 ms, different for every batch. Dating each sample from its batch's
    arrival put all of that jitter into every timestamp, and made 10-34% of them step backwards.

    Here each sample is dated from a line fitted under the arrivals instead:

    - The period is measured from the board itself, over the whole connection, between the
      least-delayed batch near the start and the least-delayed batch of the latest second.
      It follows the crystal, which ran between 17 ppm fast and 147 ppm slow against the
      host clock on the six captures we have replayed through this clock.
    - t0 is the lower envelope of the arrivals over the last `long` seconds: the batches that
      were held least. A batch can be late, never early, so the earliest is the best estimate.
    - The applied t0 moves towards that estimate by at most half a sample period per sample,
      so consecutive timestamps always increase and never stretch by more than half a period.

    A latency common to all batches - the least the link ever takes - is not visible from
    here and stays in every timestamp, the same for every sample.

    A whole cycle of the four-bit sequence number - 16 samples - can go missing without the
    sequence noticing. Timing does: every later batch then arrives a cycle later than its
    count says. When the envelope of the last `recent` seconds sits at least three quarters
    of a cycle above the envelope before it, the missing cycles are counted, and `observe`
    returns them so the caller can number later samples on and report the loss. The samples
    of those `recent` seconds were already dated, a cycle too early: the loss is reported
    with the time from which that is so.
    """

    def __init__(self, rate: int) -> None:
        self.nominal = 1.0 / rate
        self.period = self.nominal
        # The windows scale with the rate, so a cycle of 16 samples always fits twice into `recent`.
        self.recent = max(2.0, 2 * SEQ_MODULO * self.nominal)
        self.long = max(30.0, 4 * self.recent)
        # One point per wall-clock second: the least-delayed batch of that second, as
        # [second, arrival, number of the batch's last sample].
        self._points: Deque[List[float]] = deque()
        self._first: Optional[Tuple[float, int]] = None
        self.anchor: Optional[float] = None
        self._target: Optional[float] = None
        self._last: Optional[Tuple[int, float]] = None

    def _offset(self, point: List[float]) -> float:
        return point[1] - point[2] * self.period

    def observe(self, number: int, arrived: float) -> int:
        """
        Take a batch whose last sample is `number`, which arrived at `arrived`.

        Returns the number of samples found missing by timing - a multiple of SEQ_MODULO, or 0.
        The caller numbers this batch and every later one on by that many.
        """
        second = int(arrived)
        point = [second, arrived, number]
        if self._points and self._points[-1][0] == second:
            if arrived - number * self.nominal < self._points[-1][1] - self._points[-1][2] * self.nominal:
                self._points[-1] = point
        else:
            self._points.append(point)
        while self._points[0][0] < second - self.long:
            self._points.popleft()
        if self._first is None or (arrived - self._first[0] < self.recent and
                                   arrived - number * self.nominal < self._first[0] - self._first[1] * self.nominal):
            self._first = (arrived, number)

        # The least-delayed batch of the last `recent` seconds, not simply the latest one: a
        # second can hold a single batch, held back a tenth of a second by the link.
        latest = min((p for p in self._points if p[0] > second - self.recent), key=lambda p: p[1] - p[2] * self.nominal)
        if latest[1] - self._first[0] >= 10.0 and latest[2] > self._first[1]:
            period = (latest[1] - self._first[0]) / (latest[2] - self._first[1])
            # Beyond a tenth of a percent it is not the crystal but something this cannot see.
            self.period = min(max(period, self.nominal * 0.999), self.nominal * 1.001)

        missing = 0
        recent = [p for p in self._points if p[0] > second - self.recent]
        older = [p for p in self._points if p[0] <= second - self.recent]
        if older and second - self._points[0][0] >= 2 * self.recent:
            shift = min(map(self._offset, recent)) - min(map(self._offset, older))
            cycle = SEQ_MODULO * self.period
            if shift > 0.75 * cycle:
                missing = SEQ_MODULO * max(1, round(shift / cycle))
                for p in recent:
                    p[2] += missing
        self._target = min(map(self._offset, self._points))
        if self.anchor is None:
            self.anchor = self._target
        return missing

    def timestamp(self, number: int) -> float:
        """When sample `number` was taken, never earlier than the sample dated before it."""
        target = self._target + number * self.period
        if self._last is None:
            stamp = target
        else:
            n, t = self._last
            # Half a period either way per sample: fast enough to follow, slow enough never
            # to reverse, so a better estimate arriving is a slight stretch, not a step back.
            expected = t + (number - n) * self.period
            stamp = min(max(target, expected - self.period / 2), expected + self.period / 2)
        self.anchor = stamp - number * self.period
        self._last = (number, stamp)
        return stamp


class BitalinoSource:
    """
    Acquisition of a single BITalino (r)evolution device.

    Produces one record per sample - the named analog ports under their names, and the two
    digital lines always, under their names when named and as i1/i2 when not - and a record
    whenever the connection state changes, so a dropout appears in the output as an event
    rather than as an unexplained gap between two timestamps.
    """
    verbose: bool = True

    address: str
    labels: Dict[str, str]
    rate: int
    batch: int
    timeout: float
    retry_interval: float
    device: Optional[bitalino.BITalino]
    lost: int

    def __init__(
        self,
        address: str,
        labels: Dict[str, str],
        rate: int = 100,
        batch: int = 10,
        timeout: float = 5.0,
        retry_interval: float = 2.0,
    ) -> None:
        """
        :param address: COM port, MAC address or host:port of the device.
        :param labels: port (A1..A6, I1, I2) to signal name, as channel_labels() makes it.
        :param rate: sampling rate in Hz, one of SAMPLING_RATES.
        :param batch: samples fetched per read; reading one at a time is expensive.
        :param timeout: seconds to wait for the device before declaring it gone.
        :param retry_interval: seconds between reconnection attempts.
        """
        if rate not in SAMPLING_RATES:
            raise RuntimeError(f"BITalino sampling rate must be one of {SAMPLING_RATES}, not {rate}")
        self.analog = [port for port in ANALOG_PORTS if port in labels]
        if not self.analog:
            raise RuntimeError("No analog port named: the device acquires at least one, e.g. --channel A1=acc")
        self.address = address
        self.labels = labels
        self.rate = rate
        self.batch = batch
        self.timeout = timeout
        self.retry_interval = retry_interval
        self.device = None
        self.lost = 0
        self.gave_up = False                    # run() stopped because the device never answered
        # The vendor API numbers ports from 0, and packs the frame in ascending port order.
        self.channels = [ANALOG_PORTS.index(port) for port in self.analog]
        self.bits = {labels[port]: BITS[column] for column, port in enumerate(self.analog)}
        self.buttons: Dict[str, _Button] = {}
        self._last_seq: Optional[int] = None
        self._clock = _SampleClock(rate)
        self._next = 0                          # number of the next sample since the connection opened
        self._next_clock = 0.0
        self._epoch = 0.0                       # wall - perf_counter, fixed per connection (see open)

    def _connect(self) -> bitalino.BITalino:
        """Open the device. Overridden by the test to run without hardware."""
        # A timeout must be passed: without one the vendor API blocks forever on a
        # device that has stopped responding, and the dropout is never noticed.
        return bitalino.BITalino(self.address, timeout=self.timeout)

    def open(self) -> List[SensorRecord]:
        """Connect, start acquisition, and return the records describing the connection: `connected`, then one `module` per named port."""
        self.device = self._connect()
        version = self.device.version()
        self.device.start(self.rate, self.channels)
        self._last_seq = None
        # A new connection is a new count: the board restarts its acquisition, and the
        # time spent away is not known in samples.
        self._clock = _SampleClock(self.rate)
        self._next = 0
        # The sample clock runs on the performance counter, which nothing adjusts; samples are
        # put on the wall clock with the mapping of this moment, fixed for the whole connection.
        # A wall clock stepped mid-connection (a time synchronisation) therefore neither moves
        # the samples nor looks like a delivery delay - which the clock read as lost cycles of
        # 16 samples. The `clock` records show such a step, and the checker reports it.
        self._epoch = wall_epoch()
        self._next_clock = time.perf_counter() + CLOCK_INTERVAL
        self.buttons = {port: _Button(port, self.labels[port], self.rate) for port in DIGITAL_PORTS if port in self.labels}
        if self.verbose:
            print(f"bitalino: {self.address}: connected, {version}, {self.rate}Hz, {', '.join(f'{port}={self.labels[port]}' for port in self.labels)}")
        records = [self._event("connected", version=version, rate=self.rate, channels={name: port for port, name in self.labels.items()}, bits=self.bits, **clock_fields())]
        # What a port is comes from its name, never from its signal; `inferred` stays for
        # consumers that were written when the acquisition still guessed.
        for port, name in self.labels.items():
            records.append(self._event("module", port=port, channel=name, kind=name, inferred=False))
        return records

    def close(self) -> None:
        """Stop acquisition and release the port, if we still hold it."""
        if self.device is None:
            return
        device, self.device = self.device, None
        # Stopping and releasing are attempted separately. The vendor API raises a plain
        # Exception once the device has gone away - the normal case on a dropout - and it
        # raises it from stop(), which is the first call: sharing one try block with close()
        # meant the transport was never released on precisely the path that needs it most,
        # and the next reconnection found the port still held.
        failure = None
        try:
            device.stop()
        except Exception as e:
            failure = e
        try:
            device.close()
        except Exception as e:
            # The stop failure is the one that explains what happened, so it is kept.
            failure = e if failure is None else failure
        if failure is not None and self.verbose:
            print(f"bitalino: {self.address}: while closing: {failure}")

    def read(self) -> List[SensorRecord]:
        """Read one batch and return its records, gap and press events included."""
        assert self.device is not None
        frame = self.device.read(self.batch)
        arrived = time.perf_counter()
        records: List[SensorRecord] = []
        # Number every sample first. A gap the sequence number shows is samples that were
        # taken and never arrived, so the count moves on over them.
        numbered = []
        for sample in frame:
            seq = int(sample[SEQ_COLUMN])
            gap = self._check_seq(seq)
            if gap is not None:
                self._next += gap[1]
            numbered.append((self._next, seq, gap, sample))
            self._next += 1
        # Then let the clock see the batch: it may find whole sequence cycles missing.
        hidden = self._clock.observe(self._next - 1, arrived)
        if hidden:
            self._next += hidden
            self.lost += hidden
            numbered = [(number + hidden, seq, gap, sample) for number, seq, gap, sample in numbered]
            print(f"bitalino: {self.address}: {hidden} sample(s) lost that the sequence number cannot show, found by timing", file=sys.stderr)
            records.append(self._event("gap", timestamp=self._clock.timestamp(numbered[0][0]) + self._epoch, lost=hidden, detected="timing",
                                       affected_since=round(arrived - self._clock.recent + self._epoch, 6)))
        for number, seq, gap, sample in numbered:
            timestamp = self._clock.timestamp(number) + self._epoch
            if gap is not None:
                records.append(self._event("gap", timestamp=timestamp, expected=gap[0], seq=seq, lost=gap[1], detected="sequence"))
            record = self._record(timestamp, seq=seq)
            # Each named port takes exactly its own column: the frame is packed in
            # ascending port order and self.analog is kept in that order.
            for column, port in enumerate(self.analog):
                record[self.labels[port]] = int(sample[FIRST_ANALOG_COLUMN + column])
            events: List[SensorRecord] = []
            for port, column in DIGITAL_COLUMNS.items():
                value = int(sample[column])
                record[self.labels.get(port, port.lower())] = value
                if port in self.buttons:
                    event = self.buttons[port].feed(value, timestamp)
                    if event is not None:
                        events.append(self._event(event[0], **event[1]))
            records.append(record)
            # After the sample they were seen in, so a consumer reading in order has the value first.
            records.extend(events)
        if arrived >= self._next_clock:
            self._next_clock = arrived + CLOCK_INTERVAL
            records.append(self._event("clock", **clock_fields(), samples=self._next, period=round(self._clock.period, 9),
                                       anchor=round(self._clock.anchor + self._epoch, 6), epoch=round(self._epoch, 6)))
        return records

    def run(self, sink: Callable[[SensorRecord], None], duration: Optional[float] = None,
            connect_timeout: Optional[float] = CONNECT_TIMEOUT) -> None:
        """
        Acquire for `duration` seconds, or until interrupted, reconnecting as needed.

        :param sink: called with every record produced, in order.
        :param duration: seconds of acquisition, or None to run until interrupted.
        :param connect_timeout: seconds to keep trying a device that will not answer
            before giving up, or None to retry for as long as the process lives.
        """
        # `duration` is a length of acquisition, not of wall clock: only the time the device
        # is actually delivering samples counts towards it. Opening over Bluetooth takes
        # seconds and often fails once before it succeeds, and a deadline set at the start
        # charges all of that to the capture - --duration 30 returned 25 seconds of samples.
        #
        # Because of that, `duration` can never expire while nothing is connected, so it is
        # not a way out of an unreachable device. `connect_timeout` is: it is the separate
        # budget for *not* being connected, and it is what stops a finite request becoming
        # an endless retry loop. It restarts whenever the device answers, so a long capture
        # that drops out repeatedly is not punished for the earlier dropouts.
        acquired = 0.0
        unreachable_since: Optional[float] = None
        try:
            while duration is None or acquired < duration:
                started = None
                try:
                    for record in self.open():
                        sink(record)
                    unreachable_since = None
                    started = time.time()
                    while duration is None or acquired + (time.time() - started) < duration:
                        for record in self.read():
                            sink(record)
                except Exception as e:
                    # bitalino signals both "cannot connect" and "lost the device" with a
                    # plain Exception, so this cannot be narrowed. A dropout is expected
                    # operation here, not a failure: record it and try again.
                    print(f"bitalino: {self.address}: {e}", file=sys.stderr)
                    sink(self._event("disconnected", reason=str(e)))
                    self.close()
                    if unreachable_since is None:
                        unreachable_since = time.time()
                    waited = time.time() - unreachable_since
                    if connect_timeout is not None and waited >= connect_timeout:
                        print(f"bitalino: {self.address}: no answer for {waited:.1f}s, giving up", file=sys.stderr)
                        sink(self._event("unreachable", seconds=round(waited, 3), acquired=round(acquired, 3)))
                        self.gave_up = True
                        return
                    time.sleep(self.retry_interval)
                finally:
                    # Whatever ended the inner loop - the duration, a dropout, Ctrl-C - the
                    # samples that did arrive are acquisition and are counted as such.
                    if started is not None:
                        acquired += time.time() - started
        finally:
            self.close()

    def _check_seq(self, seq: int) -> Optional[Tuple[int, int]]:
        """(expected sequence number, samples lost) if `seq` does not follow the previous one."""
        expected = None if self._last_seq is None else (self._last_seq + 1) % SEQ_MODULO
        self._last_seq = seq
        if expected is None or seq == expected:
            return None
        lost = (seq - expected) % SEQ_MODULO
        self.lost += lost
        print(
            f"bitalino: {self.address}: at least {lost} sample(s) lost, expected seq {expected}, got {seq}",
            file=sys.stderr,
        )
        return expected, lost

    def _record(self, timestamp: float, **fields: Any) -> SensorRecord:
        record: SensorRecord = {
            "sensor": "bitalino",
            "device": self.address,
            "timestamp": round(timestamp, 6),
        }
        record.update(fields)
        return record

    def _event(self, event: str, timestamp: Optional[float] = None, **fields: Any) -> SensorRecord:
        return self._record(time.time() if timestamp is None else timestamp, event=event, **fields)


def parse_channel(spec: str) -> Tuple[str, str]:
    """Parse an "A1=acc" or "I1=btn" channel specification into a port and a signal name."""
    port, _, name = spec.partition("=")
    port, name = port.strip().upper(), name.strip()
    if port not in ANALOG_PORTS and port not in DIGITAL_PORTS:
        raise RuntimeError(f"Port '{port}' is not one of A1..A6, I1, I2")
    if not name or name in RESERVED:
        raise RuntimeError(f"'{spec}': a port needs a name that is not a record field, e.g. {port}={BUTTON if port in DIGITAL_PORTS else ANALOG_MODULES[0]}")
    if name == BUTTON and port not in DIGITAL_PORTS:
        raise RuntimeError(f"{port}={name}: the button goes on a digital input, I1 or I2. On an analog port it floats when released and reads whatever the port before it carries, so its presses cannot be told from that port's swings")
    if port in DIGITAL_PORTS and name != BUTTON:
        raise RuntimeError(f"{port}={name}: {port} is a digital input, which only the button ({BUTTON}) goes on")
    return port, name


def channel_labels(specs: List[str]) -> Dict[str, str]:
    """Port to name for the --channel specifications given, checked for duplicates."""
    if not specs:
        raise RuntimeError("No ports named: say what is plugged in with --channel A1=acc and the like, or run --setup")
    labels = dict(parse_channel(spec) for spec in specs)
    if len(labels) != len(specs):
        raise RuntimeError(f"A port is named twice in {specs}")
    if len(set(labels.values())) != len(labels):
        raise RuntimeError(f"Two ports carry the same name in {specs}")
    if not any(port in labels for port in ANALOG_PORTS):
        raise RuntimeError("No analog port named: the device acquires at least one, e.g. --channel A1=acc")
    return labels


WATCH = 3.0     # seconds the setup watches the ports before asking about them


def setup(address: str, configdir: str) -> None:
    """
    Ask what is plugged into each port, and write the answer into the role's sensors.json.

    The board cannot say what is attached, so this connects, shows what every port reads
    for a few seconds - enough to see that a module is alive and a port is empty - and asks.
    The answers land as --channel arguments on the VRTsensors-bitalino line of the
    configuration directory's sensors file, which is what VRTrunserver starts, so nothing is
    edited by hand. A directory that already holds a sensors.json gets a sensors.local.json
    instead: what is plugged into this machine today is not an edit to a shared example.
    """
    source = BitalinoSource(address, {port: port for port in ANALOG_PORTS}, batch=10)
    source.verbose = False
    for attempt in range(3):
        try:
            source.open()
            break
        except Exception as e:
            # Bluetooth SPP to the board is slow and often fails on the first try.
            print(f"bitalino: {address}: {e}" + (", retrying" if attempt < 2 else ""))
            if attempt == 2:
                raise
            time.sleep(source.retry_interval)
    print(f"bitalino: {address}: connected, watching the ports for a few seconds...")
    fields = ANALOG_PORTS + [port.lower() for port in DIGITAL_PORTS]
    seen: Dict[str, List[int]] = {field: [] for field in fields}
    deadline = time.time() + WATCH
    try:
        while time.time() < deadline:
            for record in source.read():
                if "event" not in record and "seq" in record:
                    for field in fields:
                        seen[field].append(record[field])
    finally:
        source.close()
    print("An empty port reads 0, or a few percent of the port before it when that one is busy.")
    labels: Dict[str, str] = {}
    for port in ANALOG_PORTS + DIGITAL_PORTS:
        values = seen[port if port in ANALOG_PORTS else port.lower()]
        reading = f"{min(values)}-{max(values)}" if min(values) != max(values) else str(min(values))
        choices = f"{'/'.join(ANALOG_MODULES)}, or Enter for nothing" if port in ANALOG_PORTS else f"{BUTTON} if the button is here, or Enter"
        answer = input(f"  {port} reads {reading}. What is plugged in? [{choices}] ").strip().lower()
        if answer:
            labels[port] = answer
    specs = [f"{port}={name}" for port, name in labels.items()]
    channel_labels(specs)
    current = sensors_path(configdir)
    filename = sensors_write_path(configdir)
    # Read whatever applies, write where a machine's own answer belongs: the other commands
    # in the file - an EmotiBit - are kept either way. utf-8-sig, because
    # Windows PowerShell writes a byte-order mark.
    commands: List[Any] = []
    if current:
        with open(current, encoding="utf-8-sig") as fp:
            commands = json.load(fp)
    default = f"VRTsensors-bitalino --address {address} --batch 2 --udp 127.0.0.1:9010 --output bitalino.jsonl"
    index = next((i for i, c in enumerate(commands) if (c.split() if isinstance(c, str) else c)[:1] == ["VRTsensors-bitalino"]), None)
    args = default.split() if index is None else commands[index].split() if isinstance(commands[index], str) else list(commands[index])
    # Replace the previous naming and address, keep everything else on the line as it was. The
    # address is the one just connected through: a port number belongs to the machine the board
    # is paired with, so an example's port is someone else's.
    kept: List[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg in ("--channel", "--address"):
            skip = True
        else:
            kept.append(arg)
    kept[1:1] = ["--address", address]
    for spec in specs:
        kept += ["--channel", spec]
    line = " ".join(kept)
    if index is None:
        commands.append(line)
    else:
        commands[index] = line
    with open(filename, "w", encoding="utf-8") as fp:
        json.dump(commands, fp, indent=4)
        fp.write("\n")
    print(f"\n{filename}:\n  {line}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture one BITalino device to a JSON-lines file")
    parser.add_argument("--address", required=True, metavar="ADDR", help="COM port (e.g. COM7), MAC address, or host:port. On Windows use the outgoing COM port of the paired device: the MAC address form needs PyBluez, which does not build on Python 3.12.")
    parser.add_argument("--channel", action="append", dest="channels", default=[], metavar="PORT=NAME", help="What is plugged into a port, e.g. A1=acc, A3=eda, I1=btn. Repeat per port; only named analog ports are acquired, while the two digital lines are always recorded (as i1/i2 when unnamed). The button goes on a digital input, I1 or I2.")
    parser.add_argument("--setup", metavar="CONFIGDIR", help="Connect, show what every port reads, ask what is plugged into each, write the answer into CONFIGDIR, and exit. The answer goes into sensors.local.json when CONFIGDIR already holds a sensors.json to preserve, and into sensors.json otherwise.")
    parser.add_argument("--rate", type=int, default=100, choices=SAMPLING_RATES, help="Sampling rate in Hz (default: 100)")
    parser.add_argument("--batch", type=int, default=10, help="Samples fetched per read (default: 10)")
    parser.add_argument("--duration", type=float, metavar="SECONDS", help="Stop after this many seconds of acquisition - time spent connecting or reconnecting does not count (default: run until interrupted)")
    parser.add_argument("--connect-timeout", type=float, default=CONNECT_TIMEOUT, metavar="SECONDS", help=f"Give up on a device that will not answer after this long (default: {CONNECT_TIMEOUT:g}). 0 retries for as long as the process lives. --duration counts acquisition, so it cannot end a capture that never starts; this can.")
    parser.add_argument("--output", metavar="FILE", help="Write records here as JSON lines (default: stdout)")
    parser.add_argument("--udp", metavar="HOST:PORT", help="Also send every record as a UDP datagram, one JSON line each, for a realtime consumer such as Unity. Fire-and-forget: the file remains the record.")
    args = parser.parse_args()

    try:
        if args.setup:
            setup(args.address, args.setup)
            return
        source = BitalinoSource(args.address, channel_labels(args.channels), rate=args.rate, batch=args.batch)
        # The UDP target is checked before the output is opened, so a mistyped --udp does
        # not leave an empty capture file behind.
        send = udp_sink(args.udp) if args.udp else None
        # Before the output exists: a malformed participant.json stops the capture here.
        header = capture_header("bitalino", device=args.address, clock="sample-count")
        output = open_capture(args.output)
    except (RuntimeError, OSError, EOFError) as e:
        print(f"{parser.prog}: Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(1)

    sink = record_sink(output, send)
    written = [0]

    def counting(record: SensorRecord) -> None:
        if "event" not in record:
            written[0] += 1
        sink(record)

    reason = "finished"
    try:
        stop_on_stdin_eof()
        sink(header)
        source.run(counting, duration=args.duration, connect_timeout=args.connect_timeout or None)
        if source.gave_up:
            reason = "unreachable"
    except KeyboardInterrupt:
        reason = "interrupted"
        print("bitalino: interrupted", file=sys.stderr)
    finally:
        # Last, so that a file which does not end with it was cut short: killed, or crashed.
        sink({"sensor": "bitalino", "device": args.address, "timestamp": round(time.time(), 6), "event": "stopped",
              "reason": reason, "samples": written[0], "lost": source.lost})
        if output is not sys.stdout:
            output.close()

    if source.gave_up:
        # Not a finished capture: whatever was asked for was not recorded, and a launcher reads this.
        print("bitalino: gave up on a device that did not answer", file=sys.stderr)
        sys.exit(1)
    if source.lost:
        print(f"bitalino: {source.lost} sample(s) lost during capture", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
