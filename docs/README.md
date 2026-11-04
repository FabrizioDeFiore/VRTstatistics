# Documentation: physiological sensors for VRTstatistics and VR2Gather

This is an add-on to CWI's [VRTstatistics](https://github.com/cwi-dis/VRTstatistics). It records
physiological signals from a **BITalino** and an **EmotiBit** during a VR2Gather session, sends them
live into the running Unity scene, and merges them into the session's data afterwards, with each
participant's signals kept separate and everything on one clock.

The pages are numbered in reading order and split into three parts. Pick the path that fits you,
and follow the **Next** link at the bottom of each page.

## Pick your path

| You are | Read |
|---|---|
| **A researcher** who wants sensor data in a study | Part 1, from [01](01-introduction.md) to [07](07-working-with-the-data.md). [08 Troubleshooting](08-troubleshooting.md) when something goes wrong |
| **A developer** building a Unity scene, or adding a sensor | [01](01-introduction.md), then Part 2 |
| **A reviewer** deciding whether this belongs in VRTstatistics | [01](01-introduction.md), then Part 3 |

## Contents

**Part 1: Using it**

| | Page | What's in it |
|---|---|---|
| 01 | [Introduction](01-introduction.md) | What VR2Gather and VRTstatistics are, what this adds, and the few words you need |
| 02 | [Installing](02-installing.md) | Python, the code, and the packages, step by step |
| 03 | [Connecting the sensors](03-connecting-the-sensors.md) | Plugging in the BITalino and the EmotiBit, and your first recording from each |
| 04 | [Running a session](04-running-a-session.md) | Recording a session on several computers with `VRTrun`, and merging the results |
| 05 | [The Sleeping Giant](05-the-sleeping-giant.md) | The demo scene: what it does, and running it in the Unity editor |
| 06 | [Checking your data](06-checking-your-data.md) | How to check a recording and a merge, and what a clean check doesn't prove |
| 07 | [Working with the data](07-working-with-the-data.md) | What's in a record, what the numbers mean, and loading them for analysis |
| 08 | [Troubleshooting](08-troubleshooting.md) | Problems we've actually hit, and how to fix them |

**Part 2: Building on it**

| | Page | What's in it |
|---|---|---|
| 09 | [Unity](09-unity.md) | Getting sensor data into your own VR2Gather scene, per participant |
| 10 | [Command reference](10-command-reference.md) | Every command, option and event, and how to add a new sensor |

**Part 3: Reviewing it**

| | Page | What's in it |
|---|---|---|
| 11 | [How it works](11-how-it-works.md) | The design, and why each choice was made |
| 12 | [For reviewers](12-for-reviewers.md) | What was already there, what was added, what changed, file by file |
| 13 | [Status](13-status.md) | What works with real sensors, what hasn't been tested, the known limitations, and what to do next |

Example settings for sessions are in [`examples/amplify-demo/`](../examples/amplify-demo/), and
are explained in [04 Running a session](04-running-a-session.md#the-example-settings).
