# VRTstatistics

This repo contains three Python packages with a shared `.venv`, managed as a monorepo.

## Packages

| Package | Entry point(s) | Role |
|---|---|---|
| `VRTrunserver` | `VRTrunserver` | Flask HTTP server (port 5002) embedded in VR2Gather builds; responds to commands from VRTrun |
| `VRTrun` | `VRTrun` | Remote-control client: reads `config/runconfig.json`, uploads per-machine configs, starts players, collects results |
| `VRTstatistics` | `VRTstatistics-ingest`, `-annotate`, `-filter`, `-plot` | Parses log files, annotates and aligns multi-machine data, produces matplotlib plots; `-filter` exports selected fields to CSV |

## Development workflow

- Update `CHANGELOG.md` for any user-visible change: new features, bug fixes, behaviour changes, new examples. Keep entries concise (one line each). Do not update it for pure refactors with no behaviour change or for chores (CI, tooling).

## Use Cases and Experiment Structure

See `readme.md` for the full description of use cases and system-oriented experiment structure. Key points for suggesting code or configs:

- **System experiments**: identify which parameter is varying; suggest hand-coding each variant config (don't generate programmatically — that historically led to too many variants and an unclear paper story). Enforce the `_run-` prefix convention for discarded runs.
- **User-centric experiments**: participant number is key metadata; analysis needs are completely different from system experiments (gaze, speech, movement — not latency/resources).
- **Demo use**: reliability and log capture matter; no analysis pipeline needed.

## Development Setup

```
python3 -m venv .venv
source .venv/bin/activate          # Mac/Linux
pip install -e VRTrun
pip install -e VRTrunserver
pip install -e VRTstatistics
```

All three packages share the `.venv` at the repo root. Python ≥ 3.12 is required.

VS Code: select the `.venv` interpreter with `Python: Select Interpreter`. A `launch.json` for debugpy attach is in `.vscode/`.

Most tools accept `--debugpy` (waits for debugpy attach on port 5678) or `--pausefordebug` (waits for a keypress to attach by PID).

## Data Flow

```
VR2Gather app  →  "stats: key=value,..." lines in log file
                          ↓
               VRTrun collects into run-YYYYMMDD-HHMM/<role>/
                          ↓
          VRTstatistics-ingest parses + annotates → combined.json
                          ↓
        VRTstatistics-plot / -filter / Jupyter notebook
```

### Running a session

See `testing.md` for a full step-by-step walkthrough including current config file formats and common failure modes.

1. Ensure VRTrunserver is running on each end-user machine.
2. Create a `config/` directory containing `runconfig.json` (list of `{role, address}` entries).
   Per-role subdirectories (`config/<role>/`) are uploaded to the corresponding machine — files there overwrite the top-level files.
3. Run `VRTrun` from the experiment directory.
   Results land in `run-YYYYMMDD-HHMM/<role>/` (log files + configs used).

### Ingesting and analyzing

`VRTstatistics-ingest` can both run the session and ingest:
- Runs the session (unless `--norun <dir>` is passed to re-ingest an existing run).
- Parses `stats.log` and (if present) `rusage.log` or `vq-brisque.log` from each role.
- Applies an annotator (`-a media_path` is the main one) to add `component_role`, align clocks, and merge.
- Saves `combined.json` in the run directory.

## Key Concepts

### The stats log format

VR2Gather components write lines like:

```
stats: component=PointCloudRenderer#7, seq=1234, ts=12.345, fps=30, ...
```

`parser.py` extracts these into dicts, converting numeric strings to int/float. It also converts the midnight-based `ts=` timestamps to `localtime` and `orchtime` (NTP-based) once it sees the orchestrator time-sync record.

### DataStore

`datastore.py` is the central data container. It loads `.log` or `.json` files and exposes:
- `get_dataframe(predicate, fields)` — returns a pandas DataFrame
- `find_first_record(predicate)` / `find_all_records(predicate)`

Predicates are Python boolean expressions evaluated with the record fields as globals. The special name `record` refers to the whole dict, e.g. `'"fps" in record'`.

#### combined.json versioning

Saved files include `"fileversion": YYYYMMDD` (integer). `FILEVERSION` and `OLDEST_COMPATIBLE_VERSION` are constants at the top of `datastore.py`. **Bump `FILEVERSION` whenever the on-disk schema changes; also bump `OLDEST_COMPATIBLE_VERSION` if the change is breaking (old files can no longer be read).** Use the date of the change as YYYYMMDD; if two incompatible changes land on the same day, use YYYYMMDD+1 for the second. Loading errors if the file's version is outside `[OLDEST_COMPATIBLE_VERSION, FILEVERSION]`. Files without `fileversion` are treated as pre-versioning (old format handled via the `metadata` key, or new-schema files from before versioning was added).

### FieldSpecifier syntax

When calling `get_dataframe(fields=[...])` or `filter(fields=[...])`:

| Syntax | Meaning |
|---|---|
| `"fps"` | include field `fps` as-is |
| `"role=fps"` | use value of field `role` as the output column name, value from `fps` |
| `"component_role.=fps"` | column name = `component_role` value + `.` + `"fps"` |
| `"f1.f2=fps"` | column name = `f1` value + `.` + `f2` value |

This is how per-component columns like `sender.pc.grabber.fps` are generated from records that each have a `component_role` and an `fps` field.

### component_role

The `Annotator` examines structure records early in each log to identify pipeline components by name, then adds a `component_role` field to every record. The naming hierarchy:

**Sender side:**
- `sender.pc.grabber` / `sender.pc.encoder` / `sender.pc.writer.{tile}`
- `sender.voice.grabber` / `sender.voice.encoder` / `sender.voice.writer`

**Receiver side:**
- `receiver.pc.reader.{tile}` / `receiver.pc.decoder.{tile}` / `receiver.pc.preparer.{tile}` / `receiver.pc.renderer.{tile}`
- `receiver.pc.tileselector`
- `receiver.synchronizer`
- `receiver.voice.reader` / `receiver.voice.decoder` / `receiver.voice.preparer` / `receiver.voice.renderer`

`{tile}` is a zero-based integer, or `all` for protocols (SocketIO, TCPReflector) that use a single reader/writer for all tiles.

### Annotators

`annotation.py` contains the annotation system:
- `AnnotationStep` — base class for a single annotation pass
- `AnnotationEngine` — registry and driver; `engine.ensure(ds, "media_path")` runs the named step if not already applied
- `ComponentRoleAnnotation` — adds `component_role` field; discovers protocol, nTiles, nQualities
- `MediaPathAnnotation` (`media_path`, old name `latency`) — records the media path to look at (the route from one sender to one receiver) and its setup

### Plotting (plots.py)

Main functions (all accept a `DataStore` as first argument):
- `plot_latencies()` — stacked area (queue/encode/decode durations) + line overlay (end-to-end latency). The main plot for latency experiments.
- `plot_framerates()` / `plot_framerates_dropped()` / `plot_framerates_and_dropped()`
- `plot_pointcounts()` — receiver point counts over time
- `plot_resources()` — CPU, memory, bandwidth (from `rusage.log`)
- `plot_latencies_per_tile()` — per-tile breakdown (requires nTiles > 1)

`analyze.py` provides `TileCombiner` and `SessionTimeFilter`: chainable `DataFrameFilter` objects used internally by the plot functions to aggregate per-tile columns.

## Output Directory Structure

```
run-YYYYMMDD-HHMM/
  sender/
    stats.log       # VR2Gather stats output
    rusage.log      # resource usage (CPU, mem, bandwidth)
    config*.json    # config files that were used
  receiver/
    stats.log
    rusage.log
    ...
  combined.json     # merged + annotated data (written by VRTstatistics-ingest)
```

## Working with existing run directories

Before suggesting a fresh VR2Gather run, check whether a run directory already exists:

- If `combined.json` is present and its `fileversion` is **compatible** with the current `FILEVERSION` in `datastore.py` → use it directly, no re-ingest needed.
- If `combined.json` is absent or its `fileversion` is **incompatible** (e.g. produced before a schema-changing refactor) → re-ingest with `VRTstatistics-ingest --norun <run-dir>` to regenerate it from the existing `stats.log` files.
- Only suggest a completely fresh run if `stats.log` is missing from the role directories or too short to be useful.

## Design principle: fail fast on bad data

VRTstatistics should detect data quality problems as early as possible and raise an error rather than silently producing garbage output. The cost of struggling on with corrupt data is a full day of participant experiment data that turns out to be unusable — discovered only at analysis time.

- **Strict parsing**: if a `stats:` line looks malformed, raise an error or emit a loud warning immediately at parse time, not silently at analysis time.
- **No silent fallbacks**: do not invent default values or skip records to paper over format errors. Surface the problem so it can be fixed at the source.
- **Validate early**: ingest should check basic sanity of the parsed data (e.g. expected fields present, numeric values in plausible ranges) before writing `combined.json`.
- **Experiment-level checks** live in a per-experiment checker script, run right after ingest (e.g. TrolleyExperiment's `vrts_session_check.py`). It decides what to check, with which thresholds, and what counts as an error; common checks are in `VRTstatistics.checks`.

The specific case that motivated this: VR2Gather on Windows with a European locale writes commas as decimal separators (e.g. `fps=109,22`). The parser silently split this into `fps=109` plus an orphaned key `22`, producing hundreds of spurious numeric fields. This went undetected until manual inspection of the output.

## Known Issues / Historical Artifacts

- **`VRTstatistics-combine`** appears in the installed `.venv/bin` but is not in the current `setup.cfg` — leftover from an older version.
- **`analyze.py` TileCombiner logic** — the interaction between `combined`, `keep`, and `previous_filter` is subtle and the code has some rough edges (e.g. the length-adjustment loop at lines 95-99). Worth a cleanup pass.
- **No example Jupyter notebooks** despite Jupyter being a stated dependency and the readme recommending it.
- **`readme.md`** still has an "Analyzing the results: To be provided" section and an "Old readme" section that contradicts the current workflow.
