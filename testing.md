# Testing VRTstatistics end-to-end

This document walks through a complete test of the VRTrun / VRTrunserver / VRTstatistics pipeline against a live VR2Gather build. It doubles as a documentation quality check: if any step is unclear or broken, fix it here.

## Prerequisites

- Two machines, each with a VR2Gather build installed.
- A running VR2Gather orchestrator reachable from both machines.
- Python ≥ 3.12 on all machines.
- The VRTstatistics repo checked out on the controlling machine with the shared `.venv` set up (see `CLAUDE.md` → Development Setup).

## Config directory structure

VRTrun uploads a `config/` directory to each machine. The layout:

```
config/
  runconfig.json        machine list and global VRTrun parameters
  config.json           VR2Gather session parameters sent to every machine
  config-user.json      RepresentationConfig overrides sent to every machine (optional)
  sender/               optional per-role overrides for the "sender" role
    config.json         overwrites config.json on the sender machine
    config-user.json    overwrites config-user.json on the sender machine
  receiver/             same for "receiver"
    config.json
    config-user.json
```

VRTrun uploads top-level files first, then per-role files — so per-role files win.

VR2Gather loads `config.json` at startup, then overlays `config-user.json` onto `RepresentationConfig` only.

### runconfig.json

```json
{
    "global": {},
    "machines": [
        {"role": "sender",   "address": "SENDER-HOSTNAME.local"},
        {"role": "receiver", "address": "RECEIVER-HOSTNAME.local"}
    ]
}
```

The `role` values are used as subdirectory names when collecting results.

### config.json (VR2Gather ≥ v1.4 format)

> **Important:** the config format changed in VR2Gather v1.4. All section names now have a `Config` suffix (`AutoStartConfig`, `StatisticsConfig`, etc.), `LocalUser` is now `RepresentationConfig`, and point-cloud transmission settings moved to `PointCloudTransmissionConfig`. Old-format keys are silently ignored by VR2Gather.
>
> **Critical:** `StatisticsConfig.outputFile` must be set to `"stats.log"` (non-empty). Otherwise stats go to the console log and VRTstatistics-ingest cannot find them.
>
> **Critical:** `configVersion` must match the value in the VR2Gather build (`VRTConfig.cs` → `CurrentConfigVersion`). A mismatch is logged as an error but does not stop the app.

Minimal config for a two-machine synthetic-PC session:

```json
{
    "configVersion": 20260311,
    "StatisticsConfig": {
        "interval": 10.0,
        "outputFile": "stats.log",
        "outputFileAppend": false
    },
    "AutoStartConfig": {
        "autoCreateForUser": "SENDER-MACHINE-HOSTNAME",
        "sessionName": "vrtrun-test",
        "sessionScenario": "Pilot 0",
        "sessionTransportProtocol": "socketio",
        "autoStartWith": 2,
        "autoLeaveAfter": 60.0,
        "autoStopAfterLeave": true,
        "autoDelay": 1.0
    },
    "PointCloudTransmissionConfig": {
        "tiled": false,
        "EncoderConfigs": [{"octreeBits": 9}]
    },
    "RepresentationConfig": {
        "microphoneName": "Muted",
        "representation_str": "PointCloud",
        "RepresentationPointcloudConfig": {
            "variant_str": "synthetic",
            "SyntheticConfig": {"nPoints": 16000},
            "frameRate": 15.0
        }
    }
}
```

**`orchestratorURL`**: omit this field to use the orchestrator URL baked into the build (set in the Unity scene). Only include it if you need to override the build default.

**`autoCreateForUser`**: VR2Gather invents a username from the machine hostname (lowercase, max 20 chars) when `RepresentationConfig.userName` is empty. Set `autoCreateForUser` to match that hostname so the sender machine creates the session and all others join. Alternatively, set explicit per-role usernames — see "Per-role usernames" below.

### config-user.json

This file is overlaid onto `RepresentationConfig` only. Minimal use is to disable the microphone or set a username:

```json
{
    "userName": "",
    "microphoneName": "Muted"
}
```

Leave `userName` empty to use the auto-invented hostname-based name.

### Per-role usernames

For explicit control over who creates the session, put per-role `config-user.json` files in `config/sender/` and `config/receiver/`:

`config/sender/config-user.json`:
```json
{"userName": "sender", "microphoneName": "Muted"}
```

`config/receiver/config-user.json`:
```json
{"userName": "receiver", "microphoneName": "Muted"}
```

Then in the top-level `config.json` set `"autoCreateForUser": "sender"`.

Alternatively, skip `autoCreateForUser` entirely and use per-role `config.json` files:

`config/sender/config.json`:
```json
{"configVersion": 20260311, "AutoStartConfig": {"autoCreate": true, "autoStartWith": 2, "autoLeaveAfter": 60.0, "autoStopAfterLeave": true, "autoDelay": 1.0}}
```

`config/receiver/config.json`:
```json
{"configVersion": 20260311, "AutoStartConfig": {"autoJoin": true, "autoLeaveAfter": 60.0, "autoStopAfterLeave": true, "autoDelay": 1.0}}
```

## Step 0: Prepare the VR2Gather build

1. In the Unity Editor, build the VR2Gather application for Windows (and optionally Mac).
2. Create a GitHub release on the VR2Gather repository with the resulting binaries attached — e.g., `VR2Gather-v1.4.0-win.zip` and `VR2Gather-v1.4.0-mac.zip`.
3. On each end-user machine, download and extract the appropriate release zip:
   - **Windows**: extract the zip to a convenient location. The directory contains `VR2Gather.exe`, `VR2Gather_Data/`, and `VRTrunserver.ps1`.
   - **Mac**: extract or copy the `.app` bundle to a convenient location.

The `VRTrunserver` scripts (`VRTrunservermac.sh` on Mac, `VRTrunserver.ps1` on Windows) are included in the release and handle their own venv/workdir setup automatically — no separate workdir needs to be created manually.

## Step 1: Start VRTrunserver on each end-user machine

**Mac:**
```bash
VRTApp-Develop-built.app/Contents/MacOS/VRTrunservermac.sh
```

This script auto-installs VRTrunserver into a venv inside the `.app` bundle on first run, then starts it.

> **Stale build trap:** the Mac script discovers the VR2Gather executable from its own location inside the `.app`. If you have an old `.app` lying around and accidentally run its `VRTrunservermac.sh`, it will silently start the old binary. Always run the script from the specific `.app` you intend to test.

**Windows:**
```powershell
.\VRTrunserver.ps1
```

Run from the directory containing the VR2Gather `.exe`. Auto-installs VRTrunserver into a local venv on first run.

**Verify connectivity** from the controlling machine:
```bash
curl http://MACHINE-HOSTNAME:5002/about
# expected: <p>Hello world!</p>
```

Port 5002 must be reachable. On Windows, check the firewall.

## Step 2: Create your experiment directory

```bash
mkdir my-experiment
cd my-experiment
cp -r /path/to/VRTstatistics/examples/simple/config ./config
```

Edit `config/config.json`:
- Set `orchestratorURL` to your orchestrator address.
- Set `autoCreateForUser` to the sender machine's hostname (or use per-role directories as above).
- Adjust `sessionScenario` to match a scenario available in your VR2Gather build.
- Update `configVersion` if your build has a newer version.

Edit `config/runconfig.json`:
- Set the `address` fields to the actual hostnames or IP addresses of your machines.

## Step 3: Activate the venv

```bash
source /path/to/VRTstatistics/.venv/bin/activate   # Mac/Linux
# or
& /path/to/VRTstatistics/.venv/Scripts/Activate.ps1  # Windows PowerShell
```

## Step 4: Run the session and ingest

You can run from any directory by passing `--config` to point at the config directory:

```bash
VRTstatistics-ingest --config /path/to/examples/simple/config
```

Or without `--config` if you are already in a directory containing a `config/` subdirectory:

```bash
VRTstatistics-ingest
```

For a latency experiment with PC data, add the annotator:

```bash
VRTstatistics-ingest -a media_path --config /path/to/examples/latency/config
```

This:
1. Calls VRTrunserver `/start` + `/run` on each machine to launch VR2Gather.
2. Waits for all instances to exit.
3. Downloads result files to `run-YYYYMMDD-HHMM/<role>/`.
4. Parses `stats.log` and `rusage.log` from each role.
5. Runs the annotator (if given) to add `component_role`, align clocks, and merge.
6. Writes `run-YYYYMMDD-HHMM/combined.json`.

To re-ingest an existing run without re-running VR2Gather:
```bash
VRTstatistics-ingest --config /path/to/config --norun run-YYYYMMDD-HHMM
```

To only run the session without ingesting (collect results, no analysis):
```bash
VRTrun --config /path/to/config
```

## Step 5: Check the results

After a successful run, `run-YYYYMMDD-HHMM/` should contain:

```
sender/
  unity.log       Unity console log
  stats.log       VRTstatistics data (only present if StatisticsConfig.outputFile was set)
  rusage.log      CPU/memory/bandwidth data collected by VRTrunserver
  config.json     Config actually used
receiver/
  unity.log
  stats.log
  rusage.log
  config.json
combined.json     Merged annotated data written by VRTstatistics-ingest
```

If `stats.log` is missing from a role directory, the most likely cause is that `StatisticsConfig.outputFile` was not set (or the old config format was used — see "Common issues").

## Step 6: Plot

From Python (e.g. a Jupyter notebook or a script):

```python
from VRTstatistics.datastore import DataStore
from VRTstatistics.plots import plot_latencies
import matplotlib.pyplot as plt

ds = DataStore("run-YYYYMMDD-HHMM/combined.json")
ds.load()
plot_latencies(ds)
plt.show()
```

See `plots.py` for other available plot functions: `plot_framerates`, `plot_resources`, `plot_latencies_per_tile`, etc.

## Common issues

**`stats.log` missing from results**
- Most likely cause: `StatisticsConfig.outputFile` is empty or the old config format was used (old keys like `statsOutputFile` are silently ignored).
- Fix: ensure `config.json` uses the new format with `"StatisticsConfig": {"outputFile": "stats.log", ...}`.

**VR2Gather logs "config file is version 0 instead of expected version NNNN"**
- Your `config.json` either has the wrong `configVersion` or no `configVersion` at all.
- Fix: set `configVersion` to the value of `CurrentConfigVersion` in the VR2Gather build (`VRTConfig.cs`).

**VR2Gather does not auto-start (stays on login screen)**
- `autoCreateForUser` doesn't match the auto-invented username, or the auto-start fields are not set correctly.
- Fix: check the VR2Gather log for the invented username (`VRTConfig: Invented username: ...`) and update `autoCreateForUser` to match, or use per-role `autoCreate`/`autoJoin` as described above.
- Known issue (VR2Gather #317): on Windows, the auto-invented username is uppercase (e.g. `BEELZEBUB`) rather than lowercase (`beelzebub`). Until that is fixed, either set `autoCreateForUser` to the uppercase hostname when the sender is on Windows, or set an explicit `userName` in the sender's `config-user.json`.

**ingest fails with `FileNotFoundError` on stats.log**
- The `stats.log` was not collected. See "stats.log missing" above.

**VRTrunserver fails to find the VR2Gather executable**
- On Mac: the script uses `${myDir}/VR2Gather` — ensure the `.app` structure is intact and the executable exists.
- On Windows: VRTrunserver walks up from the venv directory to find `VR2Gather.exe`. If the build layout changed this may fail.

**Port 5002 connection refused**
- VRTrunserver is not running, or the port is blocked by a firewall.
- On Windows, you may need to allow the port through Windows Defender Firewall.
