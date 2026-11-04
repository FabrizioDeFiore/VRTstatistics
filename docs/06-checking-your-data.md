# 06 Checking your data

A session that looked fine on screen isn't proof that its data is sound. This page shows how to
check a recording and a merge, and, just as important, what a clean check does *not* prove.

> Part 1: Using it. [Back to contents](README.md)

## Check the recordings

Point `VRTsensors-validate` at a recording, or at a whole run folder:

```powershell
VRTsensors-validate run-YYYYMMDD-HHMM
```

Its output has two kinds of line:

```
  - 1 channel(s) never changed: bitalino / COM5 / i2 - unused line, steady signal or
    disconnected sensor; check which
  ! 1 acquisition 'disconnected' event(s); inspect the capture for details
```

**`-` is an observation, `!` is a finding.** Only findings count against a recording. A channel
that never changes is the classic unclear case (an unused input, a steady signal and an electrode
that was never connected all look the same), so it's mentioned but never counts against you.

| Finding | What it means |
|---|---|
| Invalid record | broken JSON, or a missing `sensor` or `timestamp` |
| No samples | the file is empty, or has only events |
| Sequence gap | the BITalino's counter skipped; the loss figure is a minimum |
| `loss` or `gap` | the recording itself noticed missing data |
| `stale` or `disconnected` | an interruption, even if the stream came back |
| `unreachable` | the recording **gave up**: the sensor never answered. Everything after that is missing |
| Recorded twice | two streams of one device and one signal overlap in time by more than half a second: the same samples written twice |
| Lost, found by timing | whole cycles of 16 BITalino samples the counter couldn't show |
| Clock stepped | the wall clock moved against the monotonic clock by more than 0.1 s between two `clock` records |
| Newer format | the recording was written by a newer VRTsensors than the checker: update it |
| No `stopped` record | from format 2, the recording was killed or crashed, so nothing says its end is complete |
| With `--study` only | format 1, no participant ID, an unknown code revision, code with uncommitted changes in `VRTsensors/src`, a checkout whose state could not be read, samples from more than one device, an EmotiBit recording without `--source`, or EmotiBit samples dated without LSL clock correction (or recorded before VRTsensors noted whether it succeeded, or with no `connected` event before them) |

One observation is worth reading on every long recording: how far the computer's wall clock moved
against its monotonic clock over the recording. Time synchronisation moves the wall clock, and every
timestamp with it; a few milliseconds in half an hour is normal, a step is reported as `clock stepped`.
| Time goes backwards | a finding from format 2 on; in a format-1 BITalino recording only a note. See [What the timestamps can't tell you](#what-the-timestamps-cant-tell-you) |

**It's a first check, not proof.** A clean result doesn't show the data is complete, calibrated
or accurately timed; it means the checks it knows found nothing. Its exit status (0 clean, 1
findings, 2 no recordings found) makes it usable in scripts. `--verbose` and `--quiet` change the
detail, never the result.

**A recording that spans more time than you asked for isn't lossy.** For the BITalino, `--duration`
counts recording time, not clock time; connecting over Bluetooth doesn't count. A recording
*shorter* than asked for was interrupted or gave up, and an `unreachable` event says which.

## Check the merge separately

The recordings can be perfect and the merge still wrong. This prints, for each recording, how many
records made it into `combined.json`:

```python
import json
with open(r"run-YYYYMMDD-HHMM\combined.json", encoding="utf-8") as fp:
    combined = json.load(fp)
for name, capture in combined["annotations"]["sensors"]["captures"].items():
    print(name, capture["records"], capture["samples"], capture["events"])
```

Compare each `records` count with the number of lines in the raw recording (one fewer when the
recording's last line was cut off, which ingest skips with a warning):

```powershell
(Get-Content run-YYYYMMDD-HHMM\participant2\bitalino.jsonl | Measure-Object -Line).Lines
```

Counts alone can't rule out one missing row replaced by a duplicate, so the stronger check is to
compare every raw record with the merged rows, ignoring the fields ingest adds. This says nothing
about samples lost before they were recorded.

## What the timestamps can't tell you

BITalino recordings made before 2026-10-07 (format 1, no `capture` record) have **10 to 34% of
samples not strictly later than the one before**: each batch was dated backwards from when it
arrived, and batches that arrive in bursts overlap. The checker notes that for format 1 and doesn't
count it against the recording. Since then samples are dated by count
([10](10-command-reference.md#vrtsensors-bitalino)), and a step back is a finding.

> **Keep the order of lines in the file. Never sort by `seq`.** It wraps every 16 samples, so
> sorting by it scrambles the recording. Records in `combined.json` are sorted by session time; go
> back to the raw file when the exact order matters.

- **A loss of a whole cycle of 16 is found by timing, two seconds late.** The samples written in
  those seconds are dated 16 periods too early; the `gap` record's `affected_since` says from when.
  The counter alone still can't see it in a format-1 recording.
- **BITalino timestamps keep the link's least delay**, the same for every sample. It isn't
  measured, so compare a physical event seen in both streams before claiming an absolute delay.
- **EmotiBit timestamps follow packet arrival** (about every 100 ms), so their spacing says
  nothing about loss, and nothing finer than a packet can be timed from them. This is kept on
  purpose: spreading a packet's samples evenly would be an estimate, not something the device
  reports, and would need its own hardware recording to validate.
- **A wall clock stepped during a session moves everything dated after it**, except BITalino
  samples: those keep the wall-clock mapping their connection opened with, so they neither jump
  nor look lost, but they no longer line up with what moved. The `clock` records show the step,
  and the checker reports it as `clock stepped`; treat such a session as two halves, or not at all.

## Logs written with a comma as the decimal separator

On Windows, a language setting that uses `,` for decimals makes VR2Gather write numbers like
`58720,189` into `stats.log`, which ingest can't read. Sensor recordings aren't affected, which is
why this hides: every recording checks out while the session's own log can't be read. Check each
computer before a session ([04](04-running-a-session.md#part-1-once-per-computer)).

If a log was already written with commas, keep the original, fix a copy, and write down what you
changed. A session merged from a fixed log is a repaired result, not a clean run, and should never
be reported as one.

---

**Next:** [07 Working with the data](07-working-with-the-data.md)
