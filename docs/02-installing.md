# 02 Installing

From nothing to a working install, step by step. It takes about fifteen minutes, most of it
waiting for downloads. You don't need Unity or a headset for any of this.

The steps are written for **Windows**, because that's what the lab machines run. Where macOS or
Linux differ, there's a note at the end of the step.

> Part 1: Using it. [Back to contents](README.md)

## How to read the commands

Every grey box on these pages is something you type into **PowerShell**, then press Enter. To
open it: press the Windows key, type `PowerShell`, and open **Windows PowerShell**.

Words in angle brackets are yours to fill in. Replace the whole thing, brackets included:

| Placeholder | What to put there | Example |
|---|---|---|
| `<repo>` | the folder you downloaded the code into | `C:\Users\you\VRTstatistics` |
| `<PORT>` | the serial port of your paired BITalino ([03](03-connecting-the-sensors.md#connecting-it)) | `COM5` |
| `<player>` | the full path to your built VR2Gather application (the `.exe`) | |

## Step 1: Install Python and Git

1. Download **Python 3.12** from [python.org/downloads](https://www.python.org/downloads/). Everything
   here was tested with 3.12.10; newer versions are untested.
   When the installer opens, **tick "Add python.exe to PATH"** at the bottom of the first screen,
   then click *Install Now*. That tick box is the one thing people most often miss.
2. Download Git from [git-scm.com](https://git-scm.com/download/win) and install it with the
   default options.
3. Close PowerShell if it was open, open it again, and check both worked:

   ```powershell
   python --version
   git --version
   ```

   You should see `Python 3.12.x` and a Git version. If `python` isn't found, the PATH
   box wasn't ticked: run the Python installer again, choose *Modify*, and tick it.

## Step 2: Download the code

```powershell
cd $HOME
git clone https://github.com/cwi-dis/VRTstatistics
cd VRTstatistics
```

No Git? On the repository's GitHub page, click **Code → Download
ZIP**, unzip it, and `cd` into the unzipped folder instead; a ZIP has no git history, so recordings
made from it name no code revision (`VRTsensors-validate --study` reports that).

## Step 3: Make a virtual environment

A virtual environment is a private folder of Python packages just for this project, so it can't
clash with anything else on the computer. Make one, then switch it on:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

Your prompt now starts with `(.venv)`. That's how you know it's on.

**If you see "running scripts is disabled on this system"**, Windows is blocking the activation
script. Allow scripts for your own user (you only need to do this once), then activate again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
.venv\Scripts\Activate.ps1
```

*macOS and Linux:* use `python3 -m venv .venv` and `source .venv/bin/activate`.

## Step 4: Install the packages

With `(.venv)` showing, install the three VRTstatistics packages and the sensor package in one go,
pinned to the versions everything here was tested with:

```powershell
pip install -c constraints.txt -e VRTstatistics -e VRTsensors -e VRTrun -e VRTrunserver
```

Then, **only if you're using a BITalino**, install PLUX's BITalino library like this:

```powershell
pip install -c constraints.txt --no-deps bitalino
```

`constraints.txt` fixes the exact version of every package installed. Use it on every machine of a
study, so they all run the same code; leave out `-c constraints.txt` only to try newer versions.
After every `git pull`, run the first command again: each recording records which commit of this
repository made it.

Don't leave out `--no-deps`. Without it, pip tries to build an old Bluetooth library that doesn't
work with current Python and the install fails. It isn't needed: the recordings talk to the
BITalino through the serial port Windows creates when you pair it.

## Step 5: Check it worked

```powershell
VRTsensors-bitalino --help
VRTsensors-validate --version
VRTstatistics-ingest --version
```

Each one should print some text. If you get *"not recognized as the name of a cmdlet"*, the
virtual environment isn't switched on: run `.venv\Scripts\Activate.ps1` again.

## Every time you open a new PowerShell window

The virtual environment switches off when you close PowerShell. Each time you come back:

```powershell
cd <repo>
.venv\Scripts\Activate.ps1
```

---

**Next:** [03 Connecting the sensors](03-connecting-the-sensors.md)
