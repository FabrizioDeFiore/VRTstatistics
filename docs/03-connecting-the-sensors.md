# 03 Connecting the sensors

How to plug in each sensor, get your first recording from it, and tell whether the signal is
real. Every option of the recording commands is in [10 Command reference](10-command-reference.md).

> Part 1: Using it. [Back to contents](README.md)

**None of this is calibrated physiology.** The recordings store what each sensor reports: raw
readings for the BITalino, and the manufacturer's own values for the EmotiBit. A change in a
signal might be the body, or an electrode settling, a cable moving, or the person shifting in
their seat. Follow each manufacturer's instructions for placing electrodes and using the
equipment safely.

## Which sensor for what

| | BITalino Plugged Kit | EmotiBit |
|---|---|---|
| Connects by | Bluetooth, as a serial port | WiFi to EmotiBit Oscilloscope, which passes it on over LSL |
| Sampling rate | one rate for every port: 1, 10, 100 or 1000 per second | set per signal, up to 25 per second, some irregular |
| Good at | a button, and anything that needs electrodes (muscle, brain, skin conductance) | many signals at once, with no wiring |
| Needs | nothing beyond pairing | EmotiBit Oscilloscope running, with LSL output on |

## BITalino

We use the **BITalino Plugged Kit**: the board with modules that plug into its ports by cable.

### Connecting it

1. Switch it on and pair it: *Settings → Bluetooth & devices → Add device → Bluetooth*, choose
   the BITalino, and enter PIN `1234`.
2. Find its port. Pairing creates two serial ports, and you need the **outgoing** one. In
   *Settings → Bluetooth & devices → Devices*, click **More Bluetooth settings**, open the
   **COM Ports** tab, and find the BITalino's row with Direction **Outgoing**. That port (for
   example `COM5`) is your `<PORT>`.

The port number belongs to the computer, not the board: the same board paired with two computers
gets two different numbers.

### Ports

| Port | What goes there |
|---|---|
| `A1` to `A6` | analog modules. The names the demo scene understands are `acc`, `eda`, `emg`, `eeg` and `lux`; any other name is recorded too |
| `I1`, `I2` | digital inputs: **the button, and only the button** |

**Only the analog ports you name are recorded, and a port is whatever you named it.** The board
can't tell which module is connected, so `--channel A1=acc` is the only thing that says so. Name a
port wrongly and the data is wrong, with no warning.

**The two digital inputs are always recorded**, named or not, because the board sends them with
every sample. An unnamed one appears as `i1` or `i2` and stays constant, which is fine.

**Resolution depends on how many analog ports you record.** The first four give values from 0 to
1023, a fifth and sixth only 0 to 63.

### Your first recording

With the accelerometer on **A1** and the button on **I1**, record 20 seconds:

```powershell
VRTsensors-bitalino --address <PORT> --batch 2 --duration 20 --channel A1=acc --channel I1=btn
```

Move the board and press the button while it runs. The lines scrolling past are the records,
after a first line describing the recording itself (shortened here):

```json
{"sensor": "bitalino", "timestamp": 1789908464.712, "event": "capture", "format": 2, "version": "...", "command": ["VRTsensors-bitalino", "--address", "COM5", ...], "host": "...", "device": "COM5", "clock": "sample-count"}
{"sensor": "bitalino", "device": "COM5", "timestamp": 1789908469.296, "event": "connected", "version": "BITalino_v5.1", "rate": 100, "channels": {"acc": "A1", "btn": "I1"}, "bits": {"acc": 10}, "wall": 1789908469.296, "monotonic": 81234.507}
{"sensor": "bitalino", "device": "COM5", "timestamp": 1789908469.351, "seq": 0, "acc": 488, "btn": 1, "i2": 1}
...
{"sensor": "bitalino", "device": "COM5", "timestamp": 1789908471.204, "event": "press", "port": "I1", "channel": "btn", "value": 0}
```

Two quick checks catch nearly every problem:

- **`acc` should sit around 480 to 520** when the board is still, and jump by a few hundred when
  you move it. If it stays near 0 to 4, nothing is reaching that port: reseat the cable.
- **`btn` should drop to 0** while you hold the button. If it stays at `1`, reseat the button's
  connector. A loose connector looks exactly like a broken button.

If the BITalino never connects, the recording gives up after 60 seconds with an `unreachable`
line. Check it's on, paired, and that you used the outgoing port.

### Not sure what's plugged in where?

`--setup` shows you what every port reads while you press and move things, asks what's on each,
and writes the answer into a sensors file in the folder you give it:

```powershell
VRTsensors-bitalino --address <PORT> --setup <folder>
```

### Electrode modules

**`acc` and `lux` work as soon as they're plugged in.** The light sensor responds to a torch or a
hand covering it.

**`eda`, `emg` and `eeg` are amplifiers, not sensors in themselves:**

```
BITalino port --cable-- module --lead cable-- electrodes on the skin
```

They read nothing meaningful until the electrodes are on skin and clipped on. An electrode module
with nothing connected does **not** read zero: it floats, picks up mains hum, and partly copies
the port next to it.

> **The quick check for a module that isn't really connected:** move the board while watching. If
> the module's channel moves in step with `acc`, nothing is actually connected to it.

Count the leads to tell modules apart: two leads is EDA, three is EMG or EEG (two measuring and
one reference, often a different colour). The reference isn't optional; without it you get noise
that looks like signal. Run the board on its own battery, not plugged into a charger, while
electrodes are on someone's skin.

### What a working electrode signal looks like

Observations from our kit, not specifications, so you have something to compare against.

**EMG** worked straight away: an average around 509 out of 1023, never touching either end of the
range, and a standard deviation between about 6 and 78 depending on muscle activity.

**EEG**, on the forehead, worked but only just:

| | What we saw |
|---|---|
| Settling time | 3 to 15 seconds. Allow 20 before judging a recording |
| Best settled second | standard deviation 57 to 70, range 233 to 631, nothing at the limits |
| Whole recording | 10 to 32% of samples stuck at the limits of the range |
| Useful life | about 20 seconds, then the variation climbs steadily (the gel drying, or the electrode coming loose) |
| Mains interference (50 Hz) | present, about half the variation of a clean window; can be filtered out afterwards |

**Samples stuck at the limits are what matter.** They can't be recovered; mains interference can
be filtered out later. Don't test forehead EEG with a hard blink or eye movements: both overload
the module every time. Check instead that the channel settles, sits mid-range, and doesn't hit the
limits.

**Is it the hardware or the code?** PLUX's own library reads every port and the battery in one
call, `bitalino.BITalino.state()`, without involving this code. If every analog port reads 0 while
the battery reads a steady value, the board is fine and the modules aren't connected.

## EmotiBit

The EmotiBit doesn't talk to Python directly. **EmotiBit Oscilloscope**, its own desktop program,
receives the WiFi stream and passes it on as [Lab Streaming Layer (LSL)](https://labstreaminglayer.org)
streams, which the recording reads:

```
EmotiBit --WiFi--> EmotiBit Oscilloscope --LSL--> VRTsensors-emotibit --> JSON lines
```

### Connecting it

1. Put your WiFi name and password in `config.txt` on the EmotiBit's SD card. The EmotiBit only
   uses **2.4 GHz** WiFi, and it must be on the same network as the computer. University networks
   often block devices from talking to each other, which breaks this without any error; a
   separate router or a phone hotspot avoids that. On an iPhone hotspot, switch on **Maximize
   Compatibility**, which keeps it on 2.4 GHz, and keep the phone plugged in with its screen awake.
2. In EmotiBit Oscilloscope, switch on **LSL** in the *Output List*. The names of the streams it
   publishes come from the patchcords in its `lslOutputSettings.json`; the scene and these pages
   use `EDA`, `PPG_GRN`, `TEMP1`, `HR` and `ACC_X`/`ACC_Y`/`ACC_Z`, so keep those names. If Windows
   asks whether the Oscilloscope may use the network, allow it on private networks.
3. Check what's being published:

   ```powershell
   VRTsensors-emotibit --list
   ```

   You should see one line per stream, with its name, type, channel count and rate, for EDA,
   PPG, temperature, motion and HR. `source` is the EmotiBit's device ID (`MD-V5-…`): write it
   down for each participant's device, because study recordings name it with `--source`
   ([04](04-running-a-session.md#running-a-study-what-keeps-the-data-trustworthy)).
   `No LSL streams found` means a recording would be empty. If the Oscilloscope lists no device,
   `VRTsensors-emotibit --scan` tells you whether the EmotiBit reached the network at all.

### Your first recording

```powershell
VRTsensors-emotibit --duration 20
```

Two things look like faults but aren't: the skin-response streams (`SCR_AMP`, `SCR_RIS`) stay
silent until a response actually happens, and heart rate (`HR`) takes about 45 seconds before it
reports anything.

## Saving a recording to a file

So far the records went to the screen. Add `--output` to save them:

```powershell
VRTsensors-bitalino --address <PORT> --channel A1=acc --channel I1=btn --output bitalino.jsonl
```

Press **Ctrl+C** to stop. A recording never overwrites an existing file: if the file already has
something in it, pick a new name. To check what you recorded, see
[06 Checking your data](06-checking-your-data.md).

In a real session you don't type these commands yourself: each computer's sensors file lists them,
and they start with the session. That's the next page.

---

**Next:** [04 Running a session](04-running-a-session.md)
