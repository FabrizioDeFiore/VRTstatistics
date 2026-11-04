# The tap-test analyser

`tap_test.py` is a development tool, not part of any package: run it from the repository root
with the `.venv` interpreter.

**For the hardware tests.** It finds deliberate taps in BITalino `acc` and EmotiBit `ACC_*`
signals. Without arguments it only lists the signals and the taps found in each (exit 2: not an
acceptance). To judge, name every signal that must have felt the taps with `--signal` (the first
is the reference) and how many taps were made with `--taps`: each signal must show exactly that
many, paired one to one with the reference's, or the test fails. It reports each signal's median
offset and interquartile range against `--limit-ms` and `--spread-ms`, and the start and end
groups' medians separately against `--drift-ms`. On raw captures it compares recording clocks on
one computer; with `--combined`, session time across computers. An EmotiBit reads about half a
packet (~50 ms) late, by construction. `--selftest` plants taps with known answers, including an
axis that felt nothing, a missing axis, missing taps, an 80 ms offset and a drifting one, all of
which must fail.

```powershell
python tools/tap_test.py bitalino.jsonl emotibit.jsonl
python tools/tap_test.py --combined <run>/combined.json --taps 10 --signal "participant1/bitalino COM5.acc" --signal "participant2/bitalino COM4.acc" --limit-ms 50 --drift-ms 30
python tools/tap_test.py --selftest
```
