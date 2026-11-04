> ## Physiological sensors for VRTstatistics
>
> **VRTstatistics now records physiological sensors**: a **BITalino** and an **EmotiBit** (for
> now). Signals are recorded during a VR2Gather session, sent live into the running
> scene, and merged into `combined.json` with each participant's data kept separate. The work was
> done by Fabrizio De Fiore as part of the AMPlify project.
>
> ### Start here: [the documentation](docs/README.md)
>
> | You are | Start with |
> |---|---|
> | A researcher who wants sensor data in a study | [01 Introduction](docs/01-introduction.md), then [02 Installing](docs/02-installing.md) |
> | A developer building a scene or adding a sensor | [09 Unity](docs/09-unity.md) and [10 Command reference](docs/10-command-reference.md) |
> | A reviewer at CWI | [12 For reviewers](docs/12-for-reviewers.md): what was added and changed, file by file |
>
> Everything below this line is the original VRTstatistics readme.

---

# VRTstatistics - running VR2Gather apps remotely

_VRTStatistics_ helps with automatically running multiple VR2Gather-based application instances, optionally collecting the results and producing graphs or tables of things like bandwidth usage, frames per second, interaction patterns and much more.

It is intended to do repeatable experiments, for example for scientific papers, or for regression testing.

VRTstatistics contains two packages that it uses, _VRTrunserver_ and _VRTrun_. These two are also useful outside of VRTStatistics. _VRTRunserver_ is incorporated into VR2Gather-based applications (after v1.2.1) built for Windows or Mac, and it can start the VR2Gather-based app under remote control. _VRTrun_ provides that remote control.

Together, these allow a setup where a complete multi-user experience can be controlled from a single controlling computer. This can be useful for things like demonstrations or exhibitions.

Note that it is possible for the controlling computer to be the same as one of the end-user computers, VRTrun and VRTrunserver are completely independent.

This document presumes the end-user computers are running Windows (the controlling computers can run any of Mac, Linux or Windows). It is possible to use Linux or Mac end-user computers, but this is somewhat convoluted. Instructions will be forthcoming.

## Use cases

**System-oriented experiments** (primary use): Automated runs with no participants, varying parameters such as transport protocol, framerate, encoder quality, and tiling. Goal: measure latency, bandwidth, and resource usage for system-focused scientific papers. Multiple runs per configuration provide statistical robustness.

**User-centric experiments**: Sessions with two real participants interacting. Goal: measure human behaviour — turn-taking in speech, gaze direction, movement patterns — for user-focused scientific papers. Fewer parameter variations; key metadata is a participant identifier for matching to questionnaires or interviews.

**Demo/exhibition control**: VRTrun used to start and stop VR2Gather on multiple machines from a single laptop, avoiding running between rooms. Logs are timestamped for post-hoc debugging. No upfront analysis intended.

## System-oriented experiment structure

Each experiment lives in its own directory (often its own git repo). The structure for a system experiment with multiple parameter variants:

```
my-experiment/
  _config/                    # base config template, shared across variants
    config.json
    config-user.json
    runconfig.json
    sender/config-user.json   # per-role overrides if needed
    receiver/config-user.json
  tiled_octree9_fps15/        # one directory per parameter combination
    config/                   # hand-edited copy of _config for this variant
    _run-20260528-1340/       # discarded or test run (underscore prefix)
    run-20260528-1410/        # actual run — multiple runs per variant
    run-20260528-1430/        # for statistical robustness
    run-20260528-1450/
  untiled_octree7_fps15/
    config/
    run-20260528-1510/
    ...
```

**Variant naming:** encode the key parameters in the directory name (e.g. `tiled_octree9_fps15_socketio`). Names should read as a description of what distinguishes this variant.

**Hand-code each variant config.** Do not generate them programmatically. The effort of hand-coding forces you to justify each variant before running it, which keeps the experiment scope manageable and the eventual paper story clear.

Running each variant multiple times (from the experiment directory):

```bash
VRTstatistics-ingest -a latency --config tiled_octree9_fps15/config
VRTstatistics-ingest -a latency --config tiled_octree9_fps15/config
VRTstatistics-ingest -a latency --config tiled_octree9_fps15/config
```

Each run lands in `tiled_octree9_fps15/run-YYYYMMDD-HHMM/combined.json`. Prefix a run directory with `_` to mark it as a test or excluded run.

**Using prerecorded data for parameter sweeps:** Running real participants for every parameter variant is impractical. The recommended pattern is:

1. Run one session with two real participants using live RGBD capture. Record their movement, gaze, and the raw RGBD camera streams (large files, stored outside the repo).
2. Validate that a prerecorded playback session gives comparable latency and performance numbers to the live session.
3. Run all parameter variants using prerecorded playback (`variant_str: prerecorded` in the VR2Gather config, pointing at the recorded data). Sessions run unattended overnight; human variability is eliminated and input is identical across all variants.

This is what the `prerecorded` variant in the VR2Gather `RepresentationPointcloudConfig` is for. The `synthetic` variant used in the `examples/` configs is a self-contained substitute that works without data files, but is not suitable for publication-quality measurements.

**Plotting:** Three levels of use, from quick to fully customised:

- **Quick look** — `VRTstatistics-plot --type latencies run-dir/combined.json` produces a standard plot interactively. Run `VRTstatistics-plot --list-types` to see all available types. Annotation is applied automatically if needed.

- **Experiment script** — the preferred approach for production output. Create a `create_plots.py` in your experiment directory using the extract / render / publish pipeline:

  ```python
  from VRTstatistics.datastore import DataStore
  from VRTstatistics.plots import PlotStyle, render_latencies, render_resources, render_framerates_and_dropped, publish_plots
  from VRTstatistics.views import extract_latencies, extract_resources, extract_framerates

  style = PlotStyle(figsize=(6.4, 3.5), legend_kwargs=dict(ncols=2, labelspacing=0.2))
  for run_dir in sorted(glob("variant/run-*")):
      ds = DataStore(f"{run_dir}/combined.json")
      ds.load()
      axes = render_latencies(extract_latencies(ds), style=style)
      publish_plots(axes, dirname=run_dir, file_name="latencies.pdf", saveplot=True, showplot=False)
  ```

  One `PlotStyle` instance is reused across all plots and all runs, keeping output visually consistent. The older `plot_latencies(ds, ...)` convenience wrappers still work but are no longer recommended.

- **Custom plot types** — define a `View` subclass with `name`, `default_filename`, and a docstring, plus standalone `extract_*` and `render_*` functions, then call `MyView.register_extractor(...)` and `MyView.register_renderer(...)`. After `import my_module` the new type appears in `--list-types` and is usable via `view_cls.extract(ds).render()`. See issue #31 for planned examples.

  > TODO: this whole area (annotation steps, `--module`/`--import`, the View registry) needs a proper documentation pass. The emerging pattern for a per-experiment analysis module: define a `vrts_<experiment>.py` at the root of the experiment's own repo, with custom `View` subclasses (as above) *and* `AnnotationStep` subclasses (see `annotation.py`) registered with the shared `engine`. Apply with `VRTstatistics-ingest -a <name> --module vrts_<experiment>` (`VRTstatistics-annotate` has the same `--module`). Both search upward from the current directory if the module isn't otherwise importable, so this works from a session subdirectory with no packaging/install step. Working example: `TrolleyExperiment`'s `vrts_trolley.py` (`TrolleyAnnotation` + `VoiceView`/`GazeView`/`PositionView`).
  >
  > Inconsistency to reconcile: `VRTstatistics-plot` uses a differently-named `--import MODULE` for the same purpose, and it's a plain `importlib.import_module` with no upward-search fallback — should probably become `--module` with the same behavior as the other two, for one consistent story across all three CLIs.

## Preparing the VR2Gather experience to run

- Build the Player for the experience you want to run, in the Unity Editor
- Zip, copy to all end-user machines, unzip
- On every end-user machine (Windows):
	- Ensure a compatible version of Python is installed
	- **Windows SmartScreen:** the app is not code-signed, so Windows will show a "Windows protected your PC" dialog on first launch. Click **More info**, then **Run anyway**.
	- Run `VRTrunserver.ps1` with Powershell. This should install the VRTrunserver in a local (venv-based) Python. The _VRTrunserver_ is then started.
	- The installation should happen only the first time. So after that running the powershell script will start VRTrunserver directly.
	- This means that you can setup a Windows machine to auto-login after boot, and you can set `VRTrunserver.ps1` as a `Startup` program. This will prepare the machine for remote control automatically after booting.
- On every end-user machine (Mac, for development only):
	- Install the built VR2Gather as `VRTApp-Develop-built.app`.
	- **macOS Gatekeeper:** the app is not code-signed, so macOS will block it on first launch. After attempting to open it and seeing "app can't be opened", go to **System Settings → Privacy & Security → Security**, click **Open Anyway** next to the blocked app, then click **Open Anyway** again in the security dialog.
	- Run with `./VRTApp-Develop-built.app/Contents/MacOS/VRTrunservermac.sh`
	- Note: you may have to add some symlinks here and there (specifically in the `.app` folder) to make the content work.

## Preparing the controlling computer

- Create a directory where you are going to run your experiments from, and where you will store the results.

  > If you care about repeatability it may be a good idea to store the ZIP file with the built VR2Gather experience here too.
  >
  > Also, you may want to think about storing this directory under `git`, but that may be a problem with the large ZIP file (which may be too large even for `git lfs`).
- Create a Python venv, and install _VRTrun_. Installing _VRTStatistics_ itself is optional.
	- Windows:
	
		```
		python -m venv .venv
		".venv\Scripts\activate.bat"
		pip install git+https://github.com/cwi-dis/VRTStatistics#subdirectory=VRTrun
		pip install git+https://github.com/cwi-dis/VRTStatistics#subdirectory=VRTstatistics
		```
	- Mac, Linux:
	
		```
		python3 -m venv .venv
		source .venv/bin/activate
		pip install git+https://github.com/cwi-dis/VRTStatistics#subdirectory=VRTrun
		pip install git+https://github.com/cwi-dis/VRTStatistics#subdirectory=VRTstatistics
		```
- Create a configuration directory `config` to store the configuration files for this experiment.
	- An example can be found in `examples/simple/config`
	- The configuration directory needs to contain at least a `runconfig.json` which lists the hostnames (or IP addresses) of the end-user machines and the role they take in this experiment.
	- More documentation is forthcoming.

## Running an experiment

- Ensure clocks are synchronized on all machines. Desync above ~30 ms (one frame time) will distort latency measurements and may cause plots to fail. Use **ChronyControl** on Mac and **NetTime** (also known as `timesynctool`) on Windows.
- Ensure Windows machines use a **dot as the decimal separator**. VR2Gather's stats formatter is locale-sensitive; a comma decimal separator (common in European Windows locales) corrupts the stats log (see [VR2Gather #318](https://github.com/cwi-dis/VR2Gather/issues/318)). Until fixed: change this in Settings → Time & Language → Region → Additional settings.
- Ensure the correct _VRTrunserver_ is running on all end-user computers.
- On the controlling computer, in the experiment directory:
	- Activate the venv with `source .venv/bin/activate` (Mac/Linux) or `.venv\Scripts\activate` (Windows)
	- Run `VRTrun`
- The end-user computers should start their VR2Gather applications.
- After the VR2Gather applications have all terminated the results will be collected.
	- They will be in an output directory that has a timestamped name, such as `run-20250319-1230`.
	- Within that directory, there are per-role subdirectories.
	- Within each of those, all the log files from the run are stored. The configuration files that were used are stored there too.
	- This all taken together means that the output directory contains all the information needed to repeat a run, if needed in the future.

## Analyzing the results

See [testing.md](testing.md) for a full end-to-end walkthrough including ingesting logs and producing plots.

Quick summary: after a run, use `VRTstatistics-ingest -a latency` (or `--norun <dir>` to re-ingest an existing run). Results land in `run-YYYYMMDD-HHMM/combined.json`.

For a quick interactive plot: `VRTstatistics-plot --type latencies run-YYYYMMDD-HHMM/combined.json`

For exploratory analysis in Jupyter:

```python
from VRTstatistics.datastore import DataStore
from VRTstatistics.views import LatencyView, ResourceView
from VRTstatistics.plots import PlotStyle, render_latencies, render_resources, publish_plots

ds = DataStore("run-YYYYMMDD-HHMM/combined.json")
ds.load()
view = LatencyView.extract(ds)
axes = view.render()                         # uses registered renderer with default style
```

Or use `View._registry` to enumerate all available plot types and render them uniformly. The `DataStore` API (`get_dataframe(predicate, fields)`) gives you a pandas DataFrame for free-form analysis.

To export selected fields to CSV for external tools, use `VRTstatistics-filter`:

```
VRTstatistics-filter -d combined.json -o output.csv -p 'predicate' field1 field2
```

The `--predicate` option is a Python boolean expression (`sessiontime > 10`, `"fps" in record`). Field arguments select and rename columns; `role=fps` uses the value of the `role` field as the column name.

> Examples to be provided. Also need examples for field constructs like `role=latency_ms`.


## Development

If you want to modify anything here it is best to check out or fork the repository.

### Installing for development

Install python in a virtual environment and install the needed packages in an editable way. For Mac or Linux:

```
python -m venv .venv
source .venv/bin/activate
pip install -e VRTrun
pip install -e VRTrunserver
pip install -e VRTstatistics
```

For Windows `CMD`:

```
python -m venv .venv
".venv\Scripts\activate.bat" 
pip install -e VRTrun
pip install -e VRTrunserver
pip install -e VRTstatistics
```

For Windows PowerShell:

```
python -m venv .venv
& .venv\Scripts\Activate.ps1
pip install -e VRTrun
pip install -e VRTrunserver
pip install -e VRTstatistics
```

> Note: you should not try to use `bash` on Windows, this will not work because the `activate` script doesn't know how to modify `PATH` correctly.

After you have created the `.venv` you can select it in `vscode` with the `Python: Select Interpreter` command. Then all type checking and such will work.

Also, you can use the scripts and utilities from any directory, by calling

```
. .../VRTstatistics/.venv/Scripts/activate
```

Finally, most tools take a `--debugpy` argument. If you pass this argument the tool will wait after startup, and you can use the `vscode` Python Debugger `attach` command to attach a debugger. The tool will continue running once you have attached the debugger.

There is also a `--pausefordebug` argument that also waits after startup, but it doesn't wait for the python debugger. So you can attach any debugger by PID.

## Performance testing guidelines

Writing this down here because I keep forgetting. After running the plots, check:

1. First graph is points per cloud. Ensure that this has the shape you expect, given the prerecorded stream you think you've selected, or however the subject moved in front of the camera.
2. Second graph is latencies, end-to-end and from different components. Eyeball and apply common sense.
3. Framerates per component. You may have to zoom in because of outliers. Check these confirm to what you think you've selected. 
4. Dropped frames per second, per component. If they're not all zeroes you should be able to come up with a good reason why not. 
   
   **Note** if you get serious frame dropping somewhere on the sender side your sender hardware is not powerful enough. Lowering framerate or pointcount is the only option.
5. Timestamp progression in source capturer and destination receiver. Should be linear upsloping lines with sender ahead of receiver.
