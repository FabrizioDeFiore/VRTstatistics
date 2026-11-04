# 13 Status

Where the work stands at handover (October 2026): what has been run with real sensors, what has
only been checked without them, what's known not to work, and what to do next. If you're picking
this up, start here. The recordings behind it are kept by the project, not in this repository.

> Part 3: Reviewing it. [Back to contents](README.md)

## Tested with real sensors

| What | How it was checked |
|---|---|
| BITalino, one computer, through the whole pipeline | recording, live values in Unity, `stats.log`, `combined.json` |
| EmotiBit, one computer, through the whole pipeline | the same |
| BITalino and EmotiBit together on one computer | Unity's per-second summaries match the recordings |
| Two computers under `VRTrun`, one participant each, to `combined.json` | the records in `combined.json` match the raw recordings one for one; each computer logs only its own participant's signals; one computer's clock offset was measured independently and ingest corrected it |
| The current recording format on two computers | three full sessions: no BITalino samples lost, every recording ended cleanly, ingest corrected a clock 55 s off |
| A press on one computer waking the giant on both | seen in step on both computers |
| BITalino EMG and EEG on a person | through the whole pipeline; EEG only just ([why](03-connecting-the-sensors.md#what-a-working-electrode-signal-looks-like)) |
| BITalino light sensor | through the whole pipeline |
| Delay from BITalino to Unity, one computer, `--batch 10` | median 50 ms, maximum 91.5 ms, no records lost. Measured on one recording: a guide, not a guarantee |

The Python test suites pass on the delivered code; how to run them is in
[12](12-for-reviewers.md).

## Per signal

As used by [05 The Sleeping Giant](05-the-sleeping-giant.md#which-signal-does-what).

| Signal | Sensor and channel | State |
|---|---|---|
| Knock | BITalino `btn` on `I1` | tested, including a press on one computer waking both giants |
| Wind | BITalino `acc`, EmotiBit `ACC` | tested |
| Warmth | EmotiBit `EDA` | tested |
| Heartbeat | EmotiBit `HR` | tested |
| Pulse | EmotiBit `PPG` | not yet checked on a person |
| Snow | EmotiBit `TEMP` | not yet checked on a person |
| Night | BITalino `lux` | tested |
| Muscle | BITalino `emg` | tested on a person |
| Dream | BITalino `eeg` | tested on a person, only just |

## Checked without hardware only

These work in tests and simulation, but haven't yet happened with real sensors:

- **An EmotiBit Oscilloscope restart mid-session.** The lost streams are closed and their
  successors opened once, so no sample is recorded twice.
- **An EmotiBit sender that doesn't answer the clock check.** The recording carries on, marks the
  samples as uncorrected, and `--study` counts them.
- **Two EmotiBits on one network.** `--source` keeps each participant's capture to their own
  device. Each role's EmotiBit command needs its device's `--source` before such a session.
- **A long session.** A simulated 60-minute, two-participant session ingests in under a minute,
  with a memory peak of about 4 GB and a `combined.json` of about 1.2 GB.

## Never run

- The BITalino skin conductance (EDA) module.
- A long session with several people and every sensor attached. Until it's run, the pipeline is
  proven in parts, not as a whole.
- The activity envelope shared between computers in a real session; it's tested up to the point
  a message is received.
- The timing tap tests (`tools/tap_test.py`), between sensors and between computers.

## Still open

Known limitations of the current code and setup:

| | |
|---|---|
| **A crashed capture can pass validation (reviewed 2026-10-20)** | an unexpected acquisition exception can still leave a final `stopped` record with `reason: finished`. Reproduced with an EmotiBit capture that wrote one sample and then raised an exception: both normal and study validation reported no findings. BITalino has the same stop-handling pattern. Until fixed, inspect the recording process logs as well as validation. The fix is to record unsuccessful termination explicitly and make validation reject it |
| **A session can start without sensor data (reviewed 2026-10-20)** | `VRTrunserver` checks only that recording processes survive the first two seconds. An EmotiBit bound to an unavailable or mistyped `--source` can stay alive without samples while Unity runs; later recording failures are not propagated to the runner either. Check that each expected device is delivering samples before proceeding, and inspect the recording logs afterwards. The fix is an acquisition-readiness check and reporting recording failures during and at the end of the session |
| **Unity scenario selection can be masked by a local file (reviewed 2026-10-20)** | `sensors.ps1` writes and displays `sensors.json`, but editor Play uses `sensors.local.json` when it exists. Selecting another scenario, even `none`, can leave the previous recordings active. Check and update the local file when present. The fix is for the helper to update and display the same active configuration that the launcher reads |
| **EmotiBit delivered rate** | in a few early runs the EmotiBit delivered a fraction of its usual rate; it didn't happen again. Cause unknown |
| **EmotiBit `loss` events** | the loss check once reported nearly 100% loss while samples were being written. On a later recording its `loss` events matched a real shortfall of 6 to 14% per signal. Check them against the sample counts |
| **EmotiBit live values** | the network search for EmotiBit streams runs on the reading thread, so while it runs (about 1 s, every 5 s) live values reach Unity up to about 1 s late. The recording loses nothing |
| **EmotiBit sample times** | samples are timed by packet arrival, not re-spaced ([06](06-checking-your-data.md)) |
| **A wall clock stepped during a session** | reported as `clock stepped`. Reject the session or treat it as two halves: BITalino samples keep their connection's timing, everything else moves with the step |
| **Comma decimals** | a Windows machine set to a comma decimal separator writes a `stats.log` that ingest refuses until repaired ([06](06-checking-your-data.md#logs-written-with-a-comma-as-the-decimal-separator)) |
| **A restarted BITalino** | sometimes won't connect until it's switched off and on again; check it by hand before a run ([04](04-running-a-session.md#part-3-record-the-session)) |

## What to do next

1. **Run a long session** with several people and every sensor attached.
2. **Run the tap tests** with `tools/tap_test.py`, between sensors and between computers, for the
   timing figures a study will need.
3. **Put two EmotiBits on one network**, each capture bound with `--source`.
4. **Try the BITalino EDA module.**
5. **Connect the participant computers by cable** rather than WiFi.

---

You've reached the end. [Back to contents](README.md)
