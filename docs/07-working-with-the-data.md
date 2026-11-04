# 07 Working with the data

What's in a record, what the numbers do and don't mean, and how to load them for analysis without
drawing the wrong conclusion.

> Part 1: Using it. [Back to contents](README.md)

## Read this before using any of these numbers

**These are recordings of what each sensor reported, not calibrated physiology.** A BITalino
sample is dated by its number since the connection opened, on the recording computer's clock; an
EmotiBit sample by when its packet reached the computer. No units or synchronisation accuracy are
guaranteed; those belong to a study's own measurement protocol.

A change in a signal might be the body, or an electrode settling, the person shifting, or a cable
moving. If you need calibrated physiology, build it on top of this; it isn't a substitute.

## What's in a recording

A recording is a `.jsonl` file: one JSON object per line, in the order things happened, saved
after every line. Every record has:

| Field | Meaning |
|---|---|
| `sensor` | `bitalino` or `emotibit` |
| `timestamp` | seconds since 1 January 1970 (UTC), from the recording computer's clock |
| `event` | only on events (a connection, a press, a gap): what happened |

Besides those, every sample says which device it came from, and carries the values:

| Sensor | One sample is | Device identity |
|---|---|---|
| **BITalino** | every named port at one moment, as raw readings; the digital inputs as `0`/`1`; `seq`, the board's counter (0 to 15, then starts again) | `device`, its port |
| **EmotiBit** | one signal's value: `value`, or `value_0`, `value_1`, … for a signal with several channels | `source_id`, `stream_uid`, and `stream` for the signal's name |

A sample and a button press:

```json
{"sensor": "bitalino", "device": "COM5", "timestamp": 1789908469.351, "seq": 0, "acc": 488, "btn": 1, "i2": 1}
{"sensor": "bitalino", "device": "COM5", "timestamp": 1789914024.642482, "event": "press", "port": "I1", "channel": "btn", "value": 0}
```

A press is timed from the first low sample that counted as a press. Every event each sensor can
write is listed in [10 Command reference](10-command-reference.md).

The first line of every recording is a `capture` record: which format the file is in, the code and
package versions that wrote it, the command line, the computer and, for a study, the participant's
pseudonym. Every ten seconds a `clock` record gives the computer's two clocks, and the last line is
a `stopped` record. All three are described in [10](10-command-reference.md#what-a-recording-looks-like).

**EmotiBit rates**, observed on our kit, not guaranteed: motion and PPG near 25 per second, EDA
15, temperature 7.5, with heart rate and the skin-response signals irregular. Keep the device
identity: two EmotiBits can both publish a signal called `EDA`.

## What `combined.json` holds

After ingest ([04](04-running-a-session.md#part-3-record-the-session)), `combined.json` has:

```
fileversion
session       session_id, session_start_time, roles, user_names, desyncs, ...
annotations   what each step did, with a summary
data          every record, from every role, on the session clock
```

The `sensors` step keeps every original field of every record and adds:

| Field | Meaning |
|---|---|
| `role` | the role of the computer that recorded it |
| `participant` | the pseudonymous ID from the role's `participant.json`, when there was one |
| `component` | the sensor, plus the signal where there is one: `bitalino`, `emotibit.EDA` |
| `component_role` | role plus component: `participant1.sensor.emotibit.EDA` |
| `orchtime` | the original `timestamp` plus that role's measured clock offset |
| `sessiontime` | seconds since the session started. Negative before it began |

Its summary for each recording looks like this:

```json
"participant1/emotibit.jsonl": {
  "role": "participant1", "sensor": "emotibit",
  "format": 2, "version": "…", "revision": "…", "dirty": false,
  "participant": "P07", "session": {"study": "…", "session": "…"},
  "records": 77726, "samples": 77707, "events": 19,
  "before_session_start": 8803,
  "first_sessiontime": -26.353, "last_sessiontime": 204.863
}
```

`before_session_start` is normal: recordings start when the application opens, before anyone has
joined the session.

**One participant per computer, one participant per device.** A role identifies a computer. If
one computer recorded two sensors, both belong to its participant; tell them apart by device
identity. Ingest refuses a session where a participant's recording holds samples of two devices
of one kind, or where one device appears under two participants: that is an EmotiBit recorded
without `--source`, which records every EmotiBit on the network.

## Each press is recorded twice; count the right one

The recording's `press` event is the real one. Unity writes a second record of the same press into
`stats.log`, with `presses` (its running count) and `source_timestamp`. Both end up in
`combined.json`. Count every row with `event == "press"` and you count each press twice: in one of
our runs, 33 rows for 13 real presses. Filter on `sensor == "bitalino"`, as below.

## Loading it in Python

With VRTstatistics' `DataStore`, filters are **text expressions**, with `record` standing for each
row:

```python
from VRTstatistics.datastore import DataStore

ds = DataStore("run-YYYYMMDD-HHMM/combined.json")
ds.load()

eda = ds.get_dataframe(
    predicate="record.get('component') == 'emotibit.EDA' and record.get('role') == 'participant1'",
    fields=["sessiontime", "source_id", "stream_uid", "value"])

presses = ds.get_dataframe(
    predicate="record.get('sensor') == 'bitalino' and record.get('event') == 'press'",
    fields=["sessiontime", "role", "device", "timestamp", "port", "channel"])
```

Or export to CSV with `VRTstatistics-filter`, using the same kind of expression.

## Converting to physical units

BITalino values are the board's raw readings, and nothing here converts them. PLUX's datasheets
give the conversions, where `ADC` is the recorded value and `n` the port's resolution in bits: 10
for the first four acquired ports, 6 for a fifth and sixth (the `connected` record's `bits` says
which). The supply voltage `VCC` is 3.3 V.

| Module | Conversion | Source |
|---|---|---|
| `eda` | EDA (µS) = (ADC / 2ⁿ × VCC) / 0.132 | PLUX, *Electrodermal Activity (EDA) Sensor Data Sheet*, EDA 081217, rev. B |
| `eeg` | EEG (µV) = (ADC / 2ⁿ − ½) × VCC / 41782 × 10⁶ | PLUX, *Electroencephalography (EEG) Sensor Data Sheet*, EEG 011020, rev. B |
| `acc` | ACC (g) = (ADC − Cmin) / (Cmax − Cmin) × 2 − 1, with `Cmin`, `Cmax` measured per board by a slow 360° rotation | PLUX, *Accelerometer (ACC) Sensor Data Sheet*, ACC 011020, rev. B |
| `lux` | LUX (%) = ADC / 2ⁿ × 100 | PLUX, *Light (LUX) Sensor Data Sheet*, LUX 090616, rev. A |
| `emg` | not given here: the datasheet wasn't retrieved for these pages | PLUX's EMG sensor datasheet |

The accelerometer needs a calibration per board, which no recording here contains. The EmotiBit's
values arrive in the units EmotiBit Oscilloscope publishes them in, unchanged; see EmotiBit's own
documentation for those. The scene's scaling ("full scale" constants) is a display choice, not a
conversion.

## The scene's signals aren't in the data

`tension`, `brain`, `movement`, `arousal` and the rest are values the demo scene works out for
display. They're not written to `combined.json` and they're not measurements. Start an analysis
from the raw channels. To reproduce the muscle and brain meters, recompute them from the raw
recording, one sample at a time, with the constants in
[09 Unity](09-unity.md#activity-worked-out-for-every-sample), not from `stats.log`'s one-second
summaries.

## Before drawing a conclusion

1. **Check the merge first**, then look at recording faults ([06](06-checking-your-data.md)).
2. **Keep the order of lines** in format-1 BITalino recordings, which have no `capture` line;
   don't sort them by `seq` or by timestamp ([why](06-checking-your-data.md#what-the-timestamps-cant-tell-you)).
3. **No detected gap isn't the same as no loss.**
4. **No absolute timing accuracy has been established.** BITalino samples are spaced correctly to
   within a few milliseconds, but share an unmeasured delay; EmotiBit samples are timed only to the
   packet.

---

**Next:** [08 Troubleshooting](08-troubleshooting.md)
