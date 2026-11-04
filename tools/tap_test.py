"""
Tap test: how well two accelerometers, on one computer or two, agree about the same moments.

Tape the sensors together (or hold two boards side by side) and tap them sharply on a table a
few seconds apart. Each tap is a spike in every accelerometer that felt it; the spread of the
time differences between the same tap in two signals is the timing agreement between them,
measured against a physical event rather than taken from the arithmetic.

  python tools/tap_test.py bitalino.jsonl emotibit.jsonl                 list the signals and taps found
  python tools/tap_test.py bitalino.jsonl emotibit.jsonl --taps 10 \\
      --signal "bitalino COM5.acc" --signal "emotibit MD-V5-0001107.ACC_X" ...   judge them
  python tools/tap_test.py --combined run-.../combined.json ...          after ingest, session time
  python tools/tap_test.py --selftest                                    planted taps, known answers

Signals are a BITalino port named `acc` and EmotiBit streams whose name starts with ACC. A
judgement needs what is expected, because a signal that felt nothing cannot be told from one
that was not there: every `--signal` (the first is the reference) and the number of taps made,
`--taps`. Each expected signal must show exactly that many taps, paired in order with the
reference's, one to one. An EmotiBit is stamped per ~100 ms packet, so its offsets read about half
a packet late.

The taps are also split into a start group (the first `--start-taps`, by default half) and an end
group (the rest), reported separately: their difference is drift over the session, which a
whole-run median hides. `--drift-ms` sets its limit.

Exit status 0 when every expected signal has its taps, pairs one to one and meets --limit-ms
(median offset), --spread-ms (interquartile range) and --drift-ms; 1 when anything is missing or
outside a limit; 2 when nothing was asked for (no --taps or fewer than two --signal), which only
lists what was found and is never an acceptance.
"""
import argparse
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

Series = Dict[str, List[Tuple[float, float]]]


def from_captures(paths: List[str]) -> Series:
    """Accelerometer series from raw captures, on each capture's recording clock."""
    series: Series = defaultdict(list)
    for path in paths:
        with open(path, encoding="utf-8") as fp:
            for line in fp:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                _take(series, r, r.get("timestamp"))
    return series


def from_combined(path: str) -> Series:
    """Accelerometer series from combined.json, on the session clock, one per role and signal."""
    with open(path, encoding="utf-8") as fp:
        data = json.load(fp)["data"]
    series: Series = defaultdict(list)
    for r in data:
        _take(series, r, r.get("sessiontime"), prefix=f"{r.get('role', '?')}/")
    return series


def _take(series: Series, r: dict, t, prefix: str = "") -> None:
    if "event" in r or not isinstance(t, (int, float)):
        return
    if r.get("sensor") == "bitalino" and isinstance(r.get("acc"), (int, float)):
        series[f"{prefix}bitalino {r.get('device', '')}.acc"].append((t, float(r["acc"])))
    elif r.get("sensor") == "emotibit" and str(r.get("stream", "")).upper().startswith("ACC") and isinstance(r.get("value"), (int, float)):
        series[f"{prefix}emotibit {r.get('source_id', '')}.{r['stream']}"].append((t, float(r["value"])))


def taps(samples: List[Tuple[float, float]], k: float = 8.0, refractory: float = 0.8) -> List[float]:
    """
    Times of the spikes in one series: samples further from the running median than k robust
    standard deviations, one per burst (the largest), bursts at least `refractory` s apart.
    """
    if len(samples) < 20:
        return []
    samples = sorted(samples)
    values = [v for _, v in samples]
    base = statistics.median(values)
    dev = [abs(v - base) for v in values]
    mad = statistics.median(dev) or 1e-9
    threshold = k * 1.4826 * mad
    found: List[Tuple[float, float]] = []
    for (t, _), d in zip(samples, dev):
        if d < threshold:
            continue
        if found and t - found[-1][0] < refractory:
            if d > found[-1][1]:
                found[-1] = (t, d)
            continue
        found.append((t, d))
    return [t for t, _ in found]


def pair(reference: List[float], other: List[float]) -> Optional[List[float]]:
    """
    Offsets (other - reference) of the taps paired in order, or None when they do not pair one
    to one: unequal counts, or a pair further apart than half the gap to a neighbouring tap, where
    the nearest tap could be the wrong one.
    """
    if len(reference) != len(other) or not reference:
        return None
    gaps = [b - a for a, b in zip(reference, reference[1:])]
    room = min(gaps) / 2 if gaps else 1.0
    offsets = [o - r for r, o in zip(reference, other)]
    if any(abs(d) >= room for d in offsets):
        return None
    return offsets


def _summary(offsets: List[float]) -> Tuple[float, float]:
    """Median and interquartile range, in ms."""
    ms = [o * 1000 for o in offsets]
    if len(ms) < 2:
        return ms[0], 0.0
    q = statistics.quantiles(ms, n=4, method="inclusive")
    return statistics.median(ms), q[2] - q[0]


def judge(series: Series, expected: List[str], count: int, limit_ms: float, spread_ms: float,
          drift_ms: Optional[float], start_taps: Optional[int] = None, quiet: bool = False) -> int:
    say = (lambda *a: None) if quiet else print
    found = {name: taps(s) for name, s in series.items()}
    for name, t in sorted(found.items()):
        say(f"  {name}: {len(series[name])} samples, {len(t)} taps{'' if name in expected else '  (not judged)'}")
    status = 0
    for name in expected:
        if name not in found:
            say(f"  FAIL  {name}: not in the recordings")
            status = 1
        elif len(found[name]) != count:
            say(f"  FAIL  {name}: {len(found[name])} taps found, {count} made")
            status = 1
    if status:
        return status
    reference = expected[0]
    start = count // 2 if start_taps is None else start_taps
    say(f"\nReference: {reference}; {count} taps, start group {start}, end group {count - start}")
    for name in expected[1:]:
        offsets = pair(found[reference], found[name])
        if offsets is None:
            say(f"  FAIL  {name}: its taps do not pair one to one with the reference's")
            status = 1
            continue
        median, spread = _summary(offsets)
        ok = abs(median) <= limit_ms and spread <= spread_ms
        line = (f"{name}: offset median {median:+.1f} ms (limit +/-{limit_ms:g}), IQR {spread:.1f} ms (limit {spread_ms:g}), "
                f"range {min(offsets) * 1000:+.1f} .. {max(offsets) * 1000:+.1f} ms")
        if 0 < start < count:
            first, _ = _summary(offsets[:start])
            last, _ = _summary(offsets[start:])
            drift = last - first
            line += f"; start {first:+.1f} ms, end {last:+.1f} ms, drift {drift:+.1f} ms"
            if drift_ms is not None:
                line += f" (limit +/-{drift_ms:g})"
                ok = ok and abs(drift) <= drift_ms
        elif drift_ms is not None:
            line += "; no start and end groups to compare"
            ok = False
        say(f"  {'PASS' if ok else 'FAIL'}  {line}")
        status = max(status, 0 if ok else 1)
    return status


def _signal(times: List[float], rate: float, seconds: float, level: float, noise: float, height: float,
            width: float, packet: float, rng: random.Random) -> List[Tuple[float, float]]:
    out = []
    for i in range(int(seconds * rate)):
        t = i / rate
        v = level + rng.gauss(0, noise) + sum(height * math.exp(-((t - tt) / width) ** 2) for tt in times)
        out.append(((math.floor(t / packet) * packet + packet) if packet else t, v))
    return out


def selftest() -> int:
    """Planted taps with known answers: one that must pass, and every way the review found to pass wrongly."""
    rng = random.Random(3)
    tap_times = [10.0 + 6.0 * i + rng.uniform(0.0, 0.1) for i in range(10)]   # anywhere within a packet
    board = lambda times: _signal(times, 100, 75, 512, 4, 300, 0.01, 0, rng)                 # 100 Hz, raw counts
    axis = lambda times: _signal(times, 25, 75, -0.98, 0.01, 1.5, 0.04, 0.1, rng)            # 25 Hz in g, per packet
    late = lambda d: [t + d for t in tap_times]
    one_machine = lambda **axes: {"bitalino SIM.acc": board(tap_times),
                                  **{f"emotibit SIM.{a}": s for a, s in axes.items()}}
    axes = ["bitalino SIM.acc", "emotibit SIM.ACC_X", "emotibit SIM.ACC_Y"]
    boards = ["participant1/bitalino SIM.acc", "participant2/bitalino SIM.acc"]
    flat = _signal([], 25, 75, -0.98, 0.01, 0, 0.04, 0.1, rng)
    cases = [
        ("every axis felt every tap, 40 ms late plus half a packet", 0,
         one_machine(ACC_X=axis(late(0.04)), ACC_Y=axis(late(0.04))), axes, dict(limit_ms=100, spread_ms=60, drift_ms=None)),
        ("an axis that felt nothing", 1,
         one_machine(ACC_X=axis(late(0.04)), ACC_Y=flat), axes, dict(limit_ms=100, spread_ms=60, drift_ms=None)),
        ("an axis that is not in the recording", 1,
         one_machine(ACC_X=axis(late(0.04))), axes, dict(limit_ms=100, spread_ms=60, drift_ms=None)),
        ("an axis that felt half the taps", 1,
         one_machine(ACC_X=axis(late(0.04)[:5]), ACC_Y=axis(late(0.04))), axes, dict(limit_ms=100, spread_ms=60, drift_ms=None)),
        ("two computers' boards 10 ms apart, start and end alike", 0,
         {boards[0]: board(tap_times), boards[1]: board(late(0.01))}, boards, dict(limit_ms=50, spread_ms=60, drift_ms=30)),
        ("two computers' boards 80 ms apart, against +/-50 ms", 1,
         {boards[0]: board(tap_times), boards[1]: board(late(0.08))}, boards, dict(limit_ms=50, spread_ms=60, drift_ms=30)),
        ("two computers' boards drifting from 0 to 45 ms, against 30 ms", 1,
         {boards[0]: board(tap_times), boards[1]: board(tap_times[:5] + [t + 0.045 for t in tap_times[5:]])}, boards,
         dict(limit_ms=50, spread_ms=60, drift_ms=30)),
    ]
    failed = 0
    for label, want, series, expected, limits in cases:
        got = judge(series, expected, 10, quiet=True, **limits)
        good = got == want
        failed += not good
        print(f"  {'ok  ' if good else 'FAIL'}  {label}: exit {got}, expected {want}")
    found = {n: taps(s) for n, s in cases[0][2].items()}
    offsets = pair(found["bitalino SIM.acc"], found["emotibit SIM.ACC_X"]) or []
    median = statistics.median(offsets) * 1000 if offsets else float("nan")
    planted = 40 + 50     # the planted delay plus half a packet, on average
    measured = len(offsets) == 10 and abs(median - planted) <= 60
    failed += not measured
    print(f"  {'ok  ' if measured else 'FAIL'}  {len(offsets)} of 10 taps paired, median {median:+.1f} ms against about +{planted} ms planted")
    print(f"\nselftest: {'ok' if not failed else f'FAILED ({failed})'}")
    return 0 if not failed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Timing agreement between accelerometers, from deliberate taps")
    parser.add_argument("captures", nargs="*", help="VRTsensors captures (.jsonl), compared on their recording clocks")
    parser.add_argument("--combined", metavar="FILE", help="a combined.json, compared on the session clock across roles")
    parser.add_argument("--signal", action="append", default=[], metavar="NAME",
                        help="a signal that must have felt every tap, as listed without arguments; repeat; the first is the reference")
    parser.add_argument("--taps", type=int, help="how many taps were made; every --signal must show exactly this many")
    parser.add_argument("--start-taps", type=int, help="how many of them were at the start (default: half)")
    parser.add_argument("--limit-ms", type=float, default=100.0, help="largest acceptable median offset (default 100)")
    parser.add_argument("--spread-ms", type=float, default=60.0, help="largest acceptable interquartile range (default 60)")
    parser.add_argument("--drift-ms", type=float, help="largest acceptable difference between the start and end groups' medians")
    parser.add_argument("--selftest", action="store_true", help="run on planted taps and check the answers")
    args = parser.parse_args()
    if args.selftest:
        return selftest()
    if not args.captures and not args.combined:
        parser.error("give captures, --combined, or --selftest")
    series = from_combined(args.combined) if args.combined else from_captures(args.captures)
    if not args.taps or len(args.signal) < 2:
        for name, s in sorted(series.items()):
            print(f"  {name}: {len(s)} samples, {len(taps(s))} taps")
        print("\nNot judged: give --taps (how many were made) and at least two --signal names from the list above,"
              " the reference first. A listing is not an acceptance.")
        return 2
    return judge(series, args.signal, args.taps, args.limit_ms, args.spread_ms, args.drift_ms, args.start_taps)


if __name__ == "__main__":
    sys.exit(main())
