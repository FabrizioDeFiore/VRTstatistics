# 10 Command reference

Every command, option and event of the sensor package, and what a new sensor needs. For how to
install, see [02](02-installing.md); for a guided first recording, see [03](03-connecting-the-sensors.md).

> Part 2: Building on it. [Back to contents](README.md)

| Command | Does |
|---|---|
| `VRTsensors-bitalino` | records one BITalino |
| `VRTsensors-emotibit` | records EmotiBit signals passed on over LSL by EmotiBit Oscilloscope |
| `VRTsensors-validate` | checks recordings for loss, ordering, invalid records and constant channels |
| `VRTstatistics-ingest --module VRTsensors.ingest -a sensors` | merges recordings into `combined.json` |

## Options every recording command has

| Option | |
|---|---|
| `--output FILE` | write records to FILE, one JSON object per line (default: the screen). The folder is created if needed. A non-empty FILE is refused: a recording is never overwritten |
| `--udp HOST:PORT` | also send every record as one UDP datagram, for a live consumer such as Unity. Nothing is retried; a consumer that is absent or slow can't affect the recording |
| `--duration SECONDS` | stop after this long (default: until Ctrl+C). For the BITalino this counts recording time only |

Every record carries `sensor` and `timestamp`; events also carry `event`. Records are saved one by
one, so a recording that is killed loses at most the line being written.

**Stopping.** Ctrl+C, the end of `--duration`, or, when started by `VRTrunserver` or the Unity
editor, the closing of its standard input: the launchers set `VRTSENSORS_STOP_ON_STDIN_EOF=1` and
close the input to ask a recording to stop, and kill it only if it is still running five seconds
later. However it stops, the last line is a `stopped` record with `reason` (`finished` or
`interrupted`) and its totals. A recording without one was killed or crashed.

## `VRTsensors-bitalino`

```
VRTsensors-bitalino --address <PORT> --channel A1=acc --channel A3=eda --channel I1=btn --output bitalino.jsonl
```

| Option | |
|---|---|
| `--address ADDR` | the serial port (`COM5`, `/dev/tty.…`), a MAC address (needs PyBluez, which doesn't install on Windows with current Python: use the port there), or `host:port` |
| `--channel PORT=NAME` | what's plugged into a port: `A1`…`A6` for analog modules, `I1`/`I2` for the button. Repeat per port |
| `--rate HZ` | 1, 10, 100 or 1000 (default 100). One rate for every port |
| `--batch N` | samples fetched per read (default 10). Sets the delay for a live consumer: at 100 Hz, a batch of 10 arrives every 100 ms. Use 2 for live use |
| `--connect-timeout SECONDS` | give up on a board that doesn't answer for this long (default 60; `0` never gives up) |
| `--setup CONFIGDIR` | connect, show what every port reads, ask what's on each, write the answer into the folder's sensors file, and exit |

**Ports.** Only named analog ports are recorded; the digital inputs always are, as `i1`/`i2` when
unnamed. The button must go on `I1` or `I2`; on an analog port it's refused, because a released
button there floats and reads its neighbour's signal. A press is the line low for 20 ms (two
samples at 100 Hz), timed from its first low sample. The first four analog ports are 10-bit
(0 to 1023), a fifth and sixth 6-bit (0 to 63).

**Timing.** `--duration` counts seconds of **recording**; connecting and reconnecting don't count.
`--connect-timeout` counts seconds of **not being connected**, and restarts whenever the board
answers. Samples are dated by count, below, so their timestamps always increase.

**`--setup`** watches every port for three seconds, then asks about the six analog ports and two
digital inputs in turn. Enter means nothing is there. It updates the `VRTsensors-bitalino` line
of the folder's sensors file: the `--channel` names become your answers, and `--address` becomes
the one it just connected through. Other options and other sensors' lines are left alone. It
writes `sensors.local.json` when the folder already has a `sensors.json` or a
`sensors.local.json`, and `sensors.json` otherwise.

| Record | Fields |
|---|---|
| sample | `seq` (0 to 15, then starts again), one field per named analog port, both digital inputs |
| `event: capture` | the first line of every recording, described [below](#what-a-recording-looks-like); plus `device` and `clock: sample-count` |
| `event: connected` | `version`, `rate`, `channels` (name to port), `bits` (name to resolution), and both clocks at that moment, `wall` and `monotonic` |
| `event: module` | one per named port at connection: `port`, `channel`, `kind`, `inferred: false` |
| `event: press` / `release` | `port`, `channel`, `value` |
| `event: gap` | lost samples. `detected: sequence` for a gap the counter shows, with `expected`, `seq`, `lost`. `detected: timing` for whole cycles of 16 the counter can't show, with `lost` and `affected_since`, the time from which the samples already written are dated `lost` periods too early |
| `event: clock` | every 10 seconds: `wall`, `monotonic`, and the sample clock's state: `samples` counted, `period`, `anchor` |
| `event: stopped` | the last line: `reason` (`finished`, `interrupted` or `unreachable`), `samples` written, `lost` |
| `event: disconnected` | `reason`, after a dropout and after every failed connection attempt (each counts as a finding). The recording reconnects by itself |
| `event: unreachable` | it gave up: `seconds` spent trying, `acquired` seconds of samples. It then ends with `stopped` (`reason: unreachable`) and exits 1 |

The command exits non-zero if it gave up on the board or any samples were detected as lost.

**How a sample is dated.** By its number since the connection opened: `anchor + n × period`. The
period is measured from the board itself over the connection, and the anchor follows the batches
that Bluetooth delayed least, so the link's jitter stays out of the timestamps. Timestamps always
increase. What stays in them is the least delay the link ever showed, the same for every sample.
After a reconnection, the count and the clock start again.

### What a recording looks like

Every recording is JSON lines and starts with a `capture` record. Its `format` says what the fields
mean:

| `format` | Since | BITalino `timestamp` |
|---|---|---|
| none (no `capture` record) | the first recordings | batch arrival, back-dated at the nominal rate: 10 to 34% of samples step back by up to a period |
| `2` | 2026-10-07 | the sample count, as above. Plus `clock` records, and a `stopped` record last |

`VRTsensors-validate` and ingest read the format: ingest records it in each recording's summary
and refuses a format newer than its own, and the checker judges steps back in time by it.
`capture`, `clock` and `stopped` records go to the file only, never to `--udp`.

The `capture` record says what made the recording and whose it is:

| Field | |
|---|---|
| `format`, `version` | the format, and the VRTsensors version as installed |
| `revision`, `dirty` | the git commit the code ran from, and whether `VRTsensors/src` had uncommitted changes (the rest of the repository, such as a local `runconfig.json`, is not looked at); both `null` outside a git checkout, `dirty` alone `null` when git status failed |
| `python`, `dependencies` | the Python version, and the versions of `bitalino`, `pylsl`, `numpy` and `pyserial` |
| `command`, `host` | the command line, and the computer's name |
| `participant` | from `participant.json` in the working folder (`{"participant": "<pseudonym>"}`), when present. A file there without a `participant` stops the recording before it starts |
| `session` | the whole of `session.json` in the working folder, when present |
| `sources` | EmotiBit only: the `--source` device IDs the recording was bound to; empty when it was not |

## `VRTsensors-emotibit`

```
VRTsensors-emotibit --udp 127.0.0.1:9010 --output emotibit.jsonl
```

| Option | |
|---|---|
| `--list` | list the LSL streams on the network, with names, types, rates, `source` (the EmotiBit's device ID, such as `MD-V5-0001107`) and host, and exit |
| `--scan` | listen for EmotiBits announcing themselves on the network for 15 seconds (or `--duration`), and exit. Tells you whether the device reached the network at all |
| `--stream NAME` | record this stream; repeat per stream. Default: every LSL stream found, including unrelated ones, so name them for a real recording |
| `--source DEVICE_ID` | record only this EmotiBit's streams; repeat for more devices. The Oscilloscope gives every stream of a device its device ID and keeps it across restarts. **Needed for study data whenever another participant's EmotiBit is on the network**: LSL shows every computer every outlet, so without it each computer records both devices, and ingest refuses the session |
| `--stale-after SECONDS` | report a stream as stale after this long without samples (default 5) |

There's one record per sample **per stream**. Records carry `stream`, `stream_uid`, `source_id`
and `value`, or `value_0`, `value_1`, … for a stream with several channels. Timestamps are LSL's
sender times, corrected by LSL's clock offset and moved onto the computer's clock. A new stream's
samples are held (LSL buffers them) until its clock check answers, usually under a second, so they
are dated correctly; the check never holds up the other streams. If a sender doesn't answer within
2 seconds, or is lost while being asked, it prints a warning, records `time_correction` as 0 with
`time_correction_valid: false`, carries on, and asks again at every search of the network (about
every 5 s) without waiting; the answer, when it comes, is a `time_correction` event, and the
samples before it stay uncorrected. `--study` counts them. A stream still waiting for its clock
check when the capture stops keeps its samples: they are recorded uncorrected, and say so.

| Event | When |
|---|---|
| `connected` | per stream, once its clock check has answered or timed out, dated when it was opened, with `stream_type`, `rate`, `channels`, `time_correction` and `time_correction_valid` (false: the sender did not answer) |
| `time_correction` | a sender that had not answered the clock check has: the new `time_correction`, applied to samples from here on |
| `capture` | the first line of every recording, described [below](#what-a-recording-looks-like); plus `clock: lsl` |
| `clock` | every 10 seconds: `wall`, `monotonic`, `lsl` (LSL's clock) and `epoch_offset`, the offset applied to LSL's clock to give the wall clock |
| `stopped` | the last line: `reason` (`finished` or `interrupted`), `samples` written, `loss_windows` |
| `stale`, `resumed` | a regular stream goes quiet (`silent_for`, seconds), and comes back |
| `disconnected` | a stream's sender has gone (the Oscilloscope was closed or restarted), with `reason`. The stream is closed and the network searched again: the first search can come up to 5 s after the loss, then about every 2 s (each search listens for 1 s) until it is back. Its successor, under a new `stream_uid`, is opened once with a `connected` of its own |
| `loss` | a regular stream delivers noticeably fewer samples than its rate over a 10-second window: `missing` samples over `window` seconds |

Streams with no regular rate (heart rate, the skin-response streams) are exempt from both checks.
The command exits non-zero if no stream was found, no sample arrived, or any window reported loss.

## `VRTsensors-validate`

```
VRTsensors-validate <file or folder> [--verbose] [--quiet] [--study]
```

Exit status 0 if nothing was found, 1 if any recording has findings, 2 if there were no
recordings. `--study` adds what study data needs: format 2, a participant ID, and a known,
committed code revision (`dirty: false`; unknown is a finding, not clean). What it reports: [06 Checking your data](06-checking-your-data.md).

## The `sensors` ingest step

```
VRTstatistics-ingest --norun <run> --config <config> --module VRTsensors.ingest -a sensors
```

Loading `VRTsensors.ingest` registers the step. It finds recordings at `<run>/<role>/*.jsonl` and
puts every record on the session clock using the offset ingest already measured for that role.
Without `--norun`, the same command records a new session first. The step fails, with a message,
if there are no recordings or a complete line isn't a valid record; a final line cut off by an
interrupted recording is skipped with a warning. It also fails if two recordings of one role name
different participants. The fields it adds are in
[07](07-working-with-the-data.md#what-combinedjson-holds).

## Sensors files

A JSON list of recording commands, each a string or a list of arguments (a list keeps a path with
spaces in one piece):

```json
[
    "VRTsensors-bitalino --address <PORT> --batch 2 --udp 127.0.0.1:9010 --output bitalino.jsonl --channel A1=acc --channel I1=btn",
    "VRTsensors-emotibit --udp 127.0.0.1:9010 --output emotibit.jsonl"
]
```

`VRTrunserver` starts each command in the run's working folder before the player and stops it when
the player exits, sending each command's messages to `<command>-<n>.log`. It starts no player, and
the run fails with "sensors did not start", if a command's `--output` already holds a recording or a
command exits within its first two seconds. A command is looked up on
PATH, then next to the Python running the server. `sensors.local.json` wins over `sensors.json`.

## Adding a sensor

A new sensor touches no existing code. It needs:

1. A module in `VRTsensors/src/VRTsensors/` whose records have at least `sensor`, a device identity
   and `timestamp`, plus named numeric values, and events when the connection changes.
2. A `main()` that takes `--output` and `--udp` and writes through `open_capture` and
   `record_sink`, registered in `VRTsensors/setup.cfg` under `console_scripts`. To be a format-2
   recording it also calls `stop_on_stdin_eof()`, builds `capture_header(...)` before opening the
   output and writes it first, writes a `clock` record (`clock_fields()`) every `CLOCK_INTERVAL`,
   and writes a `stopped` record last, however it ends. `bitalino.py` and `emotibit.py` show all
   four.
3. A test script at the repository root that fakes the sensor's input.

Nothing else. The `sensors` step picks up any `*.jsonl` in a role folder, Unity turns any numeric
field into a channel, and a sensors file takes any command. A scene that wants to *use* the new
signal needs to be told which channel names mean what.

---

That's the end of Part 2. **Next**, to understand the design behind it: [11 How it works](11-how-it-works.md)
