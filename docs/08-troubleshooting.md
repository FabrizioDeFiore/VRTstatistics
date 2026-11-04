# 08 Troubleshooting

Every problem here has actually happened while this was being built and tested. They're grouped
by where you'll notice them.

> Part 1: Using it. [Back to contents](README.md)

## While installing

### "running scripts is disabled on this system"

Windows is blocking the script that switches on the virtual environment. Once, for your user:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
.venv\Scripts\Activate.ps1
```

### "VRTsensors-bitalino is not recognized as the name of a cmdlet"

The virtual environment isn't switched on in this window. Run `cd <repo>` and
`.venv\Scripts\Activate.ps1`.

### pip fails while building PyBluez

You installed `bitalino` without `--no-deps`. Run `pip install --no-deps bitalino` instead.

## A session produced no sensor data

### Nothing looked wrong, but there's no sensor data

**Check the sensors file first, every time: `sensors.local.json` if there is one, otherwise
`sensors.json`.** A forgotten local file from an earlier session that lists a different sensor
is the usual cause, and it looks exactly like dead hardware. Both launchers say which file they
used: the `VRTrunserver` window prints `run: sensors from sensors.local.json`, and the Unity
Console has a `SensorCaptures:` line naming it. No such line means no file was found. `git pull`
won't fix a stale local file; git ignores it on purpose. If one sensor ran and another didn't, look
for a warning that `sensors.json also starts` it: the local file replaces the shared one, so add
the missing command to the local file.

### The EmotiBit recording is empty

EmotiBit Oscilloscope isn't publishing over LSL. Before the session, `VRTsensors-emotibit --list`
must show streams. If the Oscilloscope shows no device at all, `VRTsensors-emotibit --scan` tells
you whether the EmotiBit reached the network.

### A recording gave up, or ran much shorter than expected

**An `unreachable` event, and `no answer for 60.0s, giving up`.** The BITalino never answered.
Check the port, that the board is on and paired, and that nothing else is using the port.
`--connect-timeout` sets how long it waits; `0` means never give up. A BITalino can also get stuck
after it, or the computer, has been restarted: switch it off and on, and record from it once by
hand until it says `connected` ([04](04-running-a-session.md#part-3-record-the-session), step 2)
before starting `VRTrun` again.

**Fewer samples than expected, but it did connect.** Look for `disconnected` events. For the
BITalino, `--duration` counts recording time, so after a dropout the recording runs on until it
has the time asked for; the time between `disconnected` and the next `connected` still has no
data. The EmotiBit's `--duration` is plain clock time. A recording that ends without a `stopped`
record was killed or crashed.

### A reconnection fails because the port is already in use

Something else holds the port: another recording, PLUX's OpenSignals, or another PowerShell
window.

## In the Unity editor

### Unity shows a red `SensorCaptures:` line

Unity can't find the recording command. Put the virtual environment's `Scripts` folder on your
user PATH ([05](05-the-sleeping-giant.md#on-one-computer), step 1), then **close and reopen Unity
Hub, not only the editor**. With only the editor restarted, it keeps the old PATH, and may silently
run an old copy of the code. Writing the full path of each command in the sensors file works too.

### `Cannot listen on UDP 9010`, and nothing seems to be using the port

A recording from an earlier Play is still running. Stop it:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'VRTsensors' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

### The editor starts no recordings, and says nothing about it

It stays silent when there's no sensors file next to `config.json`, or the file's list is empty.
A script that arrived through `git pull` while the editor was open isn't always picked up: restart
the editor.

## In the scene

### The giant doesn't wake

It needs three knocks. If the other participant's giant needs one more than yours, presses are
being lost between computers: check both run the same build.

### The button does nothing

Record 15 seconds and watch `btn`:

```powershell
VRTsensors-bitalino --address <PORT> --batch 2 --duration 15 --channel A1=acc --channel I1=btn
```

It must drop to **0** during a press. If it stays at `1` while `acc` is clearly moving:

1. Reseat the connector. This is the usual cause.
2. Try `I2` instead of `I1`.
3. Swap the two cables. If `acc` then stops too, the cable is the problem; if not, the button is.
4. Charge the board.

### A meter shows a calm reading for a sensor that's unplugged

A scene object must decide "is this channel arriving right now?" with `CalmLantern.Arriving()`,
not "is a channel set?". If you wrote your own scene logic, check which one it uses.

### The other participant's muscle or brain meter stays flat

Check both computers run the same build, and that a `SensorShare` component is in the scene. A
remote meter that lags half a second is normal.

### A sign's meter barely moves

A big spike, like an electrode settling, squashes the meter's range for about 30 seconds. Wait
half a minute.

## With `VRTrun`

### `VRTrun` can't reach another computer

Each participant's computer must accept incoming connections on TCP port 5002
([04](04-running-a-session.md#part-1-once-per-computer), step 3). Test it both ways with
`Test-NetConnection <other computer> -Port 5002`; a successful ping isn't enough. Check the
addresses in `runconfig.json` are current, and not a virtual network adapter's.

**It works for a minute or two, then stops, then works again.** If both machines are on WiFi, the
network itself may be dropping the traffic between them, even while each still reaches the
internet. Changing firewall rules won't help. Run the `Test-NetConnection` above every 30 seconds
for a few minutes: if it switches between `True` and `False` with nothing changed, put both
machines on a network cable, or on a phone hotspot that both of them and the EmotiBit join
([04](04-running-a-session.md#part-2-prepare-the-settings-on-your-computer), step 2).

### `VRTrun` says a role returned 500: "sensors did not start"

The server refused to start that machine's session, and started no player, because its sensor
recordings would not have run. The message says why:

- **"already holds a recording from an earlier run"**: two runs were started in the same minute.
  `VRTrun` names the run folder by the minute, so the second run found the first one's recordings.
  Wait for the next minute and start again; the first run's files are untouched.
- **"the capture exited at once"**: a recording command failed straight away. The rest of the
  message is the end of its log: usually a wrong option in the sensors file, or a
  `participant.json` without a `participant`.

### `VRTrunserver` won't start

```
No VR2Gather.exe found above ...; pass --executable to say where the player is
```

Add `--executable <player>`, or use the `VRTrunserver.ps1` in the build folder, which does that
for you. Builds made before 2026-10-08 carry VR2Gather's own `VRTrunserver.ps1` instead, which
installs a server without checking that it starts sensors: rebuild, or don't use that script.
`.\VRTrunserver.ps1 -Check` says whether the active environment is ready and current. *"the
VRTrunserver this environment runs starts no sensor captures"* names the module it found: a
`VRTrunserver.py` from before sensor support, ahead of the current one on `PYTHONPATH` or installed in that
environment. Builds from before the review fixes of 2026-10-08 don't make this check.

### Both applications sit in the lobby and the interface clicks by itself

`autoCreateForUser` names a user who isn't in the session, so everyone tries to join a session
nobody created ([04](04-running-a-session.md#running-one-machine-by-hand)).

### The built application shows a login screen with no buttons

The Unity editor is still open on the same computer and holding `stats.log`. Close the editor
completely.

## After the session

### Ingest fails on a `stats.log`

```
Bare numeric key '584' in stats line — likely caused by a locale using comma as decimal separator
```

That computer writes decimals with a comma
([06](06-checking-your-data.md#logs-written-with-a-comma-as-the-decimal-separator)). If its
decimal separator is set to a dot, check how its `VRTrunserver` was started: from an
administrator window, the application can take that account's settings instead
([04](04-running-a-session.md#part-3-record-the-session), step 1).

### A new build behaves exactly like the old one

Unity doesn't always rewrite the `.exe` when only scripts changed. Check the date of
`<build>/<product>_Data/Managed/Assembly-CSharp.dll` instead.

### Evidence disappeared

In the editor, `stats.log` is overwritten on every Play. And another participant's sensor data is
never in your own `stats.log`, so collect both computers' logs before shutting anything down.

---

That's the end of Part 1. **Next**, if you're building on this: [09 Unity](09-unity.md). If you're
reviewing it: [11 How it works](11-how-it-works.md).
