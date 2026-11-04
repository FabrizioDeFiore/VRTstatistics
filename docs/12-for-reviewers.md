# 12 For reviewers

What was already in VRTstatistics, what this work added, what it changed, and why, file by file. Everything described as *added* or *changed* was done
by **Fabrizio De Fiore** as part of AMPlify; everything described as *upstream* is CWI's, unchanged.

> Part 3: Reviewing it. [Back to contents](README.md)

## The short version

- **One upstream source file changed**: `VRTrunserver/src/VRTrunserver.py`. It can start and stop
  sensor recordings, and no longer refuses to start outside a build folder. A role with no sensors
  file behaves exactly as before.
- **Everything else is new and self-contained**: the `VRTsensors` package, its tests, the example
  settings, and this documentation.
- **Nothing in `VRTstatistics/` or `VRTrun/` changed.** The ingest step uses VRTstatistics' own
  extension point, `--module` plus a registered annotation step.
- **The VR2Gather package isn't modified.** The Unity side is a folder of scripts in an
  application project.

```
UPSTREAM, unchanged          VRTstatistics/  VRTrun/  examples/simple  examples/latency*
                             testing.md  CLAUDE.md  LICENSE  test_plots.py

UPSTREAM, changed            VRTrunserver/src/VRTrunserver.py   sensor recordings; --executable
                             CHANGELOG.md                       entries under [Unreleased]
                             .gitignore                         recordings, local sensors files
                             readme.md                          a short section added at the top

ADDED                        VRTsensors/                        the package
                             test_bitalino.py  test_emotibit.py
                             test_sensor_ingest.py  test_sensor_regressions.py  test_sensor_validate.py
                             examples/amplify-demo/             example settings
                             constraints.txt                    pinned third-party versions
                             tools/tap_test.py                  the timing analyser for the hardware tap tests
                             docs/                              this documentation
```

To see every line: `git diff 41212bd HEAD --stat`. Commit `41212bd` is the last upstream commit the
work is based on.

## Where it comes from

The work arrives as one change on top of upstream commit `41212bd`; nothing in upstream's history
was rewritten. The Unity side arrives the same way, as one change to
[`cwi-dis/VR2Gather_sample`](https://github.com/cwi-dis/VR2Gather_sample).

## What was already there, and is used unchanged

| Upstream piece | Used for |
|---|---|
| `VRTrun` sending `config/` and then `config/<role>/` to each machine | delivering each role its sensors file |
| `VRTrun` fetching every file from each role's working folder | bringing the recordings back next to `stats.log` |
| `VRTstatistics-ingest --module` and `AnnotationStep` / `engine.register` | loading the `sensors` step without touching ingest (upstream added `--module` in `41212bd`, before this work) |
| `SessionNormalizer`: each role's clock offset, `session_start_time` | putting recordings on the session clock with the offset already measured |
| The `component_role` annotation step | declared as a dependency, so it runs first and doesn't overwrite the sensor records' fields |
| `DataStore` and its flat records | sensor records are the same kind of dict; no schema change |
| VR2Gather's statistics component | Unity logs its per-second sensor summaries into the same `stats.log` |
| VR2Gather's session messages (`BaseMessage`, `RegisterEventType`) | sharing each participant's snapshot |
| VR2Gather's `ScenarioRegistry` | registering the demo scenario without editing the sample |

## What was added

### `VRTsensors/`, the package

| File | Lines | What it is | Why it's shaped this way |
|---|---:|---|---|
| `src/VRTsensors/__init__.py` | ~280 | the record type; `open_capture` and `record_sink`, the output path every recording shares; the capture format, its header and clock readings; the sensors-file rule | one place for the rules every recording must follow |
| `src/VRTsensors/bitalino.py` | ~640 | `VRTsensors-bitalino`: the sample clock, press detection, `--setup` | the board's quirks (no module identification, floating ports, a 4-bit counter, batch delivery) are dealt with here, not downstream |
| `src/VRTsensors/emotibit.py` | ~465 | `VRTsensors-emotibit` via LSL; `--list`, `--scan` | several rates at once; loss judged per time window because the device delivers in packets |
| `src/VRTsensors/ingest.py` | ~150 | the `sensors` annotation step | the whole connection to ingest |
| `src/VRTsensors/validate.py` | ~410 | `VRTsensors-validate` | separates findings from observations, so it can gate a session without false alarms |
| `setup.cfg`, `pyproject.toml` | | packaging and the three commands | the same `setuptools_scm` layout as upstream's packages |

Dependencies added: `numpy`, `pyserial` (for PLUX's `bitalino` library, installed separately with
`--no-deps`) and `pylsl`. No upstream package's requirements changed.

### Tests

Scripts run with plain `python`, no pytest, matching upstream's `test_plots.py`: the first three
print one `ok` line per check, the last two are `unittest` suites. Each exits non-zero on failure.
None needs hardware.

| Suite | Checks (or tests) | Covers |
|---|---:|---|
| `test_bitalino.py` | 110 | the sample clock (jitter, a slow crystal, a hidden lost cycle), capture and clock records kept out of `--udp`, the header's commit, dependencies, participant and session, a board that never answers ending `unreachable` with exit 1, record shape, port independence, the button, lost samples, reconnection, recording time across a failed attempt, a board that never answers, releasing the port when `stop()` fails, channel naming, `--setup`, a wall clock stepped mid-connection (+0.5 s and +1 s) not counted as loss |
| `test_emotibit.py` | 64 | LSL streams, multi-channel records, stale and loss windows, clock correction and a sender that never answers it, a lost stream reopened once, a failed clock correction recorded as such, retried and counted by `--study`, nineteen senders that never answer without any read held up, a stream held until its correction answers, a held stream kept when the capture ends or is interrupted (also beside one that never left), two devices visible to two bound captures before and after a restart |
| `test_sensor_ingest.py` | 32 | participant IDs on every record and two participants on one computer refused, the capture format recorded and a newer one refused, the merge, the **direction** of the clock correction (including a negative offset), records before the session, the refusals, a capture holding two devices and one device under two participants refused, two computers' BITalinos on the same port name accepted |
| `test_sensor_regressions.py` | 23 | the server's recording lifecycle, a start refused on a used working folder or a capture that exits at once, the local sensors file winning and naming what it leaves out, a real recording asked to stop and ending with `stopped`, cleanup after a failed launch, Windows command lines and byte-order marks, the server running without the sensor package |
| `test_sensor_validate.py` | 18 | invalid records, device identity, reconnects, recording faults, a signal recorded twice, steps back judged by format, a stepped clock, losses found by timing, a missing `stopped` record, a newer format, `--study`, drift over a capture, exit status, `--study` refusing unknown checkout state, unbound EmotiBit captures, two devices in one capture, samples without LSL clock correction and samples with no correction state at all |

```powershell
python test_bitalino.py; python test_emotibit.py; python test_sensor_ingest.py; python test_sensor_regressions.py; python test_sensor_validate.py
```

`test_sensor_ingest.py` is the one most worth reading: it checks that a recording lands on the
session clock with its role's offset applied **in the right direction**, which is easy to get
wrong and impossible to see in a plot. Tests added after a review were first run against the code
they replace and seen to fail.

### The tap-test analyser

[`tools/tap_test.py`](../tools/README.md) turns the hardware tap tests (deliberate taps on
taped-together sensors) into offsets and a pass or fail; it judges only the signals and the tap
count it is told to expect, so a sensor that felt nothing fails rather than drops out. It is a
development tool, outside every package.

## The one upstream file that changed

`VRTrunserver/src/VRTrunserver.py`: 188 lines added, 26 removed, in three separate changes.

**1. Starting and stopping the recordings** (`start_sensors()`, `stop_sensors()`). When a session
starts, the server reads `sensors.local.json`, or failing that `sensors.json`, in the session's
working folder, starts every command in it, then starts the player. When the player exits, or on
`/kill`, it stops them. Without a sensors file, nothing happens.

| What a reviewer will check | Why it's this way |
|---|---|
| No sensors file isn't an error | most roles have no sensors |
| Commands found with `shutil.which`, then next to `sys.executable` | the commands live in the server's own virtual environment, which needn't be on PATH |
| A command can be a string or a list of arguments | Windows paths don't survive POSIX-style splitting |
| `shlex.split(posix=os.name != 'nt')` plus stripping quotes on Windows | lets a string command contain `"C:\Program Files\…"` |
| A failure while starting stops what had started, then re-raises | no half-started set left behind |
| `process_lock` (re-entrant) and `run_lock` | the server is threaded; a second `/run` is refused |
| Stopping also stops `children(recursive=True)` | on Windows, a `.exe` launcher runs Python as a child |
| The player's output goes to a temporary file, not a pipe | an unread pipe can fill and freeze the player |
| The server has its own copy of the sensors-file rule | so it doesn't depend on `VRTsensors`; a test checks the copies agree |
| It warns about every sensor `sensors.json` starts and the local file leaves out | the local file replaces the shared one, so such a sensor would otherwise vanish without an error |
| A run is refused before the player starts when a capture's `--output` already holds a recording, or a capture exits within `SENSOR_START_CHECK` (2 s) | captures never overwrite, so a reused working folder (two runs in one minute) or a bad argument left the session running without sensor data, silently |
| Each recording gets a stdin pipe and `VRTSENSORS_STOP_ON_STDIN_EOF=1` | closing the pipe asks it to stop; a killed recording can't be told from a crashed one, and a BITalino killed mid-read is never told to stop |

Stopping closes each recording's stdin and waits up to `SENSOR_STOP_GRACE` (five seconds) for it to
finish by itself. Then, for whatever is left, it sends a terminate, waits up to five seconds, kills
what's still there, and waits up to five more.

**2. `--executable` works outside a build folder.** The server used to fail at startup when it
wasn't inside a build folder, before reading its arguments. Now it waits for `--executable`, and
when no player is known, says so: `No VR2Gather.exe found above <argv[0]>; pass --executable to say
where the player is`.

**3. `/kill` stops the recordings too**, and succeeds when only recordings are running.

## The Unity side

The Unity code is added to CWI's [VR2Gather sample project](https://github.com/cwi-dis/VR2Gather_sample).

| Added | What it is |
|---|---|
| `Assets/VRTSensors/SensorStream.cs`, `SensorJson.cs` | the UDP listener, channels, logging |
| `Assets/VRTSensors/SensorShare.cs` | sharing per participant |
| `Assets/VRTSensors/CalmLantern.cs`, `SensorBoard.cs`, `GardenSignpost.cs` | derived signals, the board, signs |
| `Assets/VRTSensors/SleepingGiant.cs` and the other garden scripts, `Scenes/` | the demo scene and its registration |
| `Assets/VRTSensors/Editor/` | starting recordings on Play, the sensor rig, the build's `VRTrunserver.ps1` |
| `sensor-scenarios/sensors-*.json`, `sensors.ps1` | ready-made sensors files, and a script that copies one to `sensors.json` |

| Changed from the sample | Why |
|---|---|
| `config.json`: adds `"SensorConfig": {"udpPort": 9010}` | names the listening port |
| `ProjectSettings/EditorBuildSettings.asset`: adds the demo scene | so a built player contains it |
| `.gitignore`: ignores the sensors file and recordings | a sensors file describes one computer |

## Conventions this code follows

Taken from the existing codebase: `from __future__ import annotations` and type hints; attributes
declared at class level; no abstract base classes or logging framework, only `verbose` flags and
`print`, errors to stderr; private helpers prefixed `_`; `__all__` naming the public surface; error
messages that name the fix; no `TODO` markers; comments that explain why, not what; `CHANGELOG.md`
updated for every user-visible change.

## Things to be sceptical about

| | |
|---|---|
| **BITalino timestamps share an unmeasured delay** | samples are dated by count and spaced to within a few milliseconds, but the least delay of the Bluetooth link stays in every timestamp. Format-1 recordings still have the old back-dated timestamps |
| **The BITalino skin conductance module has never been run** | same code path as the other analog modules, but no recording exists |
| **Long sessions with several people and every sensor haven't been tested** | see [13 Status](13-status.md) |
| **The activity envelope crossing the network hasn't run in a real session** | tested up to the point a message is received |
| **Nothing is calibrated** | raw readings or the manufacturer's values; the scene's scaling is a demo choice |
| **Record fields are documented, with a format number but no schema file** | the `capture` record's `format` says what the fields mean; nothing checks a record against a schema |
| **`CalmLantern.movement` is worked out once per frame** | unlike muscle and brain; kept on purpose, a judgement rather than a proven choice |
| **One participant per computer** | several sensors on one computer all belong to its participant |

One upstream observation, reported rather than changed: `VRTrun/src/VRTrun/runner.py` has a bare
`return` straight after `self.verbose = verbose` in `Runner.__init__`, which makes the rest of that
method unreachable. It works because every caller passes `host` and `role` directly.

## If this were adopted upstream

1. **`VRTsensors` as a fourth package**, installed only where sensors are used.
2. **The `VRTrunserver` change doesn't depend on the package** and can be taken on its own.
3. **The Unity scripts** belong with VR2Gather, not VRTstatistics; the garden scripts are the
   demo's.

Where to start reading the code: [11 How it works](11-how-it-works.md), then
`VRTsensors/src/VRTsensors/ingest.py`, then `git diff 41212bd HEAD -- VRTrunserver/src/VRTrunserver.py`,
then `bitalino.py`.

---

**Next:** [13 Status](13-status.md)
