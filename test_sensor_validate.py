"""Capture quality checks at malformed records, device boundaries and reconnects."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from VRTsensors.validate import Capture, main


class ValidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "capture.jsonl"

    def capture(self, records):
        self.path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        return Capture(str(self.path))

    def sample(self, timestamp, **fields):
        return dict(sensor="bitalino", device="COM4", timestamp=timestamp, acc=400, **fields)

    def test_bad_records_report_lines_and_keep_scanning(self):
        records = [{}, [], {"timestamp": 1}, self.sample(float("nan")), self.sample(True),
                   self.sample(1, seq=16), self.sample(2, seq=False), self.sample(3, value=float("inf")),
                   self.sample(4)]
        capture = self.capture(records)
        self.assertEqual(capture.unparseable, 8)
        self.assertEqual(capture.samples, 1)
        self.assertTrue(any(f"{self.path}:4:" in f for f in capture.report()))

    def test_loss_and_disconnect_events_are_findings(self):
        # `unreachable` belongs here too: the acquisition gave up on a device that never
        # answered, so the capture is short by decision and the verdict has to say so.
        for event in ("loss", "gap", "stale", "disconnected", "unreachable"):
            with self.subTest(event=event):
                c = self.capture([self.sample(1), self.sample(2, event=event), self.sample(3)])
                self.assertTrue(any(event in f for f in c.report()))

    def test_reconnect_resets_sequence_without_hiding_real_gaps(self):
        c = self.capture([self.sample(1, seq=5), self.sample(2, event="connected"),
                          self.sample(3, seq=0), self.sample(4, seq=1)])
        self.assertEqual(c.seq_lost, 0)
        self.assertEqual(c.report(), [])
        c = self.capture([self.sample(1, seq=14), self.sample(2, seq=1)])
        self.assertEqual(c.seq_lost, 2)

    def test_devices_and_channels_do_not_share_state(self):
        rows = [dict(sensor="emotibit", stream=stream, source_id=device, stream_uid=device + stream,
                     timestamp=t, value=value) for device, stream, t, value in
                [("A", "EDA", 10, 1), ("A", "EDA", 11, 1), ("B", "EDA", 10, 100),
                 ("B", "EDA", 11, 101), ("A", "TEMP1", 10, 30), ("A", "TEMP1", 11, 31)]]
        c = self.capture(rows)
        self.assertEqual(len(c.streams), 3)
        self.assertEqual(len(c.channels), 3)
        self.assertEqual(c.report(), [])
        self.assertEqual(sum(lo == hi for lo, hi, _ in c.channels.values()), 1)

    def emotibit(self, uid, t, value=1.0):
        return dict(sensor="emotibit", stream="PPG_GRN", source_id="MD-1", stream_uid=uid, timestamp=t, value=value)

    def test_one_signal_recorded_twice_is_a_finding(self):
        # What a recovering inlet produced after an Oscilloscope restart: the old uid and
        # the new one delivering the same samples side by side.
        rows = [self.emotibit("old", t) for t in range(0, 10)] + [self.emotibit("new", t) for t in range(5, 10)]
        c = self.capture(sorted(rows, key=lambda r: r["timestamp"]))
        self.assertTrue(any("recorded twice" in f for f in c.report()), c.report())

    def test_a_stream_replaced_by_its_successor_is_not(self):
        rows = [self.emotibit("old", t) for t in range(0, 5)] + [self.emotibit("new", t) for t in range(6, 10)]
        c = self.capture(rows)
        self.assertEqual(c.overlapping_copies(), [])

    def test_steps_back_are_a_note_in_format_1_and_a_finding_after(self):
        backwards = [self.sample(1.00), self.sample(1.02), self.sample(1.01), self.sample(1.03)]
        old = self.capture(backwards)
        self.assertFalse(any("not strictly increasing" in f for f in old.report()), old.report())
        self.assertTrue(any("not strictly increasing" in n and "format 1" in n for n in old.notes()), old.notes())
        new = self.capture([self.sample(0.5, event="capture", format=2)] + backwards)
        self.assertTrue(any("not strictly increasing" in f for f in new.report()), new.report())

    def test_a_stepped_wall_clock_is_a_finding(self):
        clock = lambda wall, monotonic: self.sample(wall, event="clock", wall=wall, monotonic=monotonic)
        steady = self.capture([clock(100.0, 5.0), self.sample(101), clock(110.001, 15.0)])
        self.assertEqual(steady.clock_steps, [])
        stepped = self.capture([clock(100.0, 5.0), self.sample(101), clock(110.8, 15.0)])
        self.assertTrue(any("clock stepped" in f and "+0.800" in f for f in stepped.report()), stepped.report())

    def test_losses_found_by_timing_are_counted(self):
        c = self.capture([self.sample(1), self.sample(2, event="gap", lost=16, detected="timing"), self.sample(3)])
        self.assertTrue(any("16 samples lost that the sequence number could not show" in f for f in c.report()), c.report())

    def test_a_capture_that_was_not_stopped_is_a_finding(self):
        header = self.sample(0.5, event="capture", format=2)
        cut = self.capture([header, self.sample(1), self.sample(2)])
        self.assertTrue(any("no 'stopped' record" in f for f in cut.report()), cut.report())
        whole = self.capture([header, self.sample(1), self.sample(2), self.sample(3, event="stopped", reason="finished")])
        self.assertEqual(whole.report(), [])
        old = self.capture([self.sample(1), self.sample(2)])
        self.assertEqual(old.report(), [], "format 1 never had a stopped record")

    def test_study_mode_requires_participant_and_committed_code(self):
        good = dict(event="capture", format=2, participant="P07", revision="a" * 40, dirty=False)
        stopped = self.sample(9, event="stopped", reason="finished")
        c = self.capture([self.sample(0.5, **good), self.sample(1), stopped])
        self.assertEqual(c.report() + c.study_findings(), [])
        for change, expected in ((dict(participant=None), "no participant ID"), (dict(dirty=True), "uncommitted changes"),
                                 (dict(revision=None), "revision is unknown"), (dict(revision=""), "revision is unknown"),
                                 (dict(dirty=None), "is unknown"), (dict(dirty="no"), "is unknown")):
            with self.subTest(change=change):
                c = self.capture([self.sample(0.5, **{**good, **change}), self.sample(1), stopped])
                self.assertEqual(c.report(), [], "a demo is not wrong for lacking these")
                self.assertTrue(any(expected in f for f in c.study_findings()), c.study_findings())
        old = self.capture([self.sample(1)])
        self.assertTrue(any("format 1" in f for f in old.study_findings()))

    def test_study_mode_rejects_uncorrected_lsl_time(self):
        head = dict(sensor="emotibit", stream="", timestamp=0.5, event="capture", format=2, participant="P07",
                    revision="a" * 40, dirty=False, sources=["MD-A"])
        eda = lambda t: dict(sensor="emotibit", stream="EDA", source_id="MD-A", stream_uid="u", timestamp=t, value=1.0)
        connected = lambda **f: dict(sensor="emotibit", stream="EDA", source_id="MD-A", stream_uid="u", timestamp=0.9, event="connected", **f)
        stopped = dict(sensor="emotibit", stream="", timestamp=9, event="stopped", reason="finished")
        good = self.capture([head, connected(time_correction=0.0, time_correction_valid=True), eda(1), eda(2), stopped])
        self.assertEqual(good.report() + good.study_findings(), [])
        bad = self.capture([head, connected(time_correction=0.0, time_correction_valid=False), eda(1), eda(2),
                            dict(connected(time_correction=0.01, time_correction_valid=True), event="time_correction", timestamp=2.5), eda(3), stopped])
        self.assertEqual(bad.report(), [], "a demo is not wrong for it")
        self.assertTrue(any("2 samples dated without LSL clock correction" in f for f in bad.study_findings()), bad.study_findings())
        old = self.capture([head, connected(time_correction=0.0), eda(1), stopped])
        self.assertTrue(any("was not recorded" in f for f in old.study_findings()), old.study_findings())

    def test_study_mode_rejects_samples_with_no_correction_state(self):
        # Review follow-up, 2026-10-08: with no connected or time_correction event the
        # stream never had a correction state, and the capture passed --study.
        head = dict(sensor="emotibit", stream="", timestamp=0.5, event="capture", format=2, participant="TEST",
                    revision="a" * 40, dirty=False, sources=["MD-A"])
        eda = lambda t: dict(sensor="emotibit", stream="EDA", source_id="MD-A", stream_uid="u", timestamp=t, value=1.0)
        connected = dict(sensor="emotibit", stream="EDA", source_id="MD-A", stream_uid="u", timestamp=1.5, event="connected",
                         time_correction=0.0, time_correction_valid=True)
        stopped = dict(sensor="emotibit", stream="", timestamp=9, event="stopped", reason="finished")
        absent = self.capture([head, eda(1), stopped])
        self.assertTrue(any("1 samples before any connected" in f for f in absent.study_findings()), absent.study_findings())
        early = self.capture([head, eda(1), eda(1.2), connected, eda(2), stopped])
        self.assertTrue(any("2 samples before any connected" in f for f in early.study_findings()), early.study_findings())
        self.assertEqual(self.capture([head, connected, eda(2), stopped]).study_findings(), [])

    def test_study_mode_requires_one_bound_device(self):
        head = dict(event="capture", format=2, participant="P07", revision="a" * 40, dirty=False)
        eda = lambda t, device: dict(sensor="emotibit", stream="EDA", source_id=device, stream_uid=device, timestamp=t, value=1.0)
        stopped = dict(sensor="emotibit", stream="", timestamp=9, event="stopped", reason="finished")
        connected = dict(sensor="emotibit", stream="EDA", source_id="MD-A", stream_uid="MD-A", timestamp=0.9, event="connected",
                         time_correction=0.0, time_correction_valid=True)
        bound = self.capture([dict(sensor="emotibit", stream="", timestamp=0.5, sources=["MD-A"], **head), connected, eda(1, "MD-A"), stopped])
        self.assertEqual(bound.report() + bound.study_findings(), [])
        unbound = self.capture([dict(sensor="emotibit", stream="", timestamp=0.5, sources=[], **head), eda(1, "MD-A"), stopped])
        self.assertTrue(any("not bound" in f for f in unbound.study_findings()), unbound.study_findings())
        both = self.capture([dict(sensor="emotibit", stream="", timestamp=0.5, sources=[], **head), eda(1, "MD-A"), eda(1.1, "MD-B"), stopped])
        self.assertTrue(any("2 devices" in f for f in both.study_findings()), both.study_findings())

    def test_drift_over_the_capture_is_noted(self):
        clock = lambda wall, monotonic: self.sample(wall, event="clock", wall=wall, monotonic=monotonic)
        c = self.capture([clock(100.0, 5.0), self.sample(101), clock(220.012, 125.0)])
        self.assertTrue(any("+12.0 ms" in n and "over 2 min" in n for n in c.notes()), c.notes())

    def test_a_newer_format_is_a_finding(self):
        c = self.capture([self.sample(0.5, event="capture", format=99), self.sample(1)])
        self.assertTrue(any("format 99" in f for f in c.report()), c.report())

    def test_verbose_does_not_change_exit_status(self):
        self.capture([self.sample(t, seq=t, i2=1) for t in range(3)])
        for flags in ([], ["--verbose"], ["--quiet"]):
            with patch("sys.argv", ["validate", str(self.path)] + flags), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)

    def test_missing_file_does_not_abort_directory_batch(self):
        self.capture([self.sample(1)])
        output = io.StringIO()
        with patch("sys.argv", ["validate", str(self.path.parent / "absent"), str(self.path)]), contextlib.redirect_stdout(output):
            self.assertEqual(main(), 1)
        self.assertIn("2 capture(s)", output.getvalue())


if __name__ == "__main__":
    unittest.main()
