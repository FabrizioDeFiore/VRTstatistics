"""Regression checks for failed launches and interrupted/empty captures. No hardware used."""
import importlib.util
import json
import os
import shutil
from pathlib import Path
import sys
import tempfile
import subprocess
import time
import types
import unittest
from unittest.mock import patch
import warnings

import psutil
from VRTstatistics.datastore import DataStore, DataStoreError
from VRTsensors.ingest import SensorAnnotation

ROOT = Path(__file__).resolve().parent
# The temperature driver is unrelated to lifecycle checks and requires Windows privileges.
sys.modules.setdefault('WinTmp', types.SimpleNamespace(CPU_Temp=lambda: 0))
server_path = ROOT / 'VRTrunserver' / 'src' / 'VRTrunserver.py'
if not server_path.exists():
    server_path = ROOT / 'VRTrunserver.py'
spec = importlib.util.spec_from_file_location('sensor_runner_review', server_path)
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

class CaptureTests(unittest.TestCase):
    def test_empty_and_interrupted_capture_preserve_valid_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            role = Path(tmp) / 'self'
            role.mkdir()
            (role / 'empty.jsonl').touch()
            (role / 'bitalino.jsonl').write_text('{"sensor":"bitalino","timestamp":1001,"acc":42}\n{"sensor":')
            ds = DataStore(str(Path(tmp) / 'combined.json'))
            ds.data = []
            ds.session_metadata = {'roles': ['self'], 'session_start_time': 1000, 'desyncs': {}}
            with warnings.catch_warnings(record=True) as reported:
                warnings.simplefilter('always')
                summary = SensorAnnotation().apply(ds)
            self.assertEqual(summary['records'], 1)
            self.assertEqual(ds.data[0]['acc'], 42)
            self.assertEqual(summary['captures']['self/empty.jsonl']['records'], 0)
            self.assertEqual(len(reported), 2)

    def test_malformed_complete_line_and_nonfinite_timestamp_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad.jsonl'
            for text in ['{not json}\n', '{"sensor":"bitalino","timestamp":NaN}\n']:
                path.write_text(text)
                with self.assertRaises(DataStoreError):
                    SensorAnnotation()._load(str(path), 'self', 0, 0)

class CaptureOutputTests(unittest.TestCase):
    """The output side every capture command shares: VRTsensors.open_capture and record_sink."""

    def test_a_capture_refuses_a_nonempty_file_and_creates_its_directory(self):
        from VRTsensors import open_capture
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'run' / 'self' / 'capture.jsonl'
            output = open_capture(str(path))
            output.write('{"sensor":"x","timestamp":1}\n')
            output.close()
            with self.assertRaisesRegex(RuntimeError, 'not overwritten'):
                open_capture(str(path))

    def test_every_record_is_one_flushed_line_and_mirrored_to_udp(self):
        from VRTsensors import record_sink
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'capture.jsonl'
            sent = []
            with open(path, 'w', encoding='utf-8') as output:
                sink = record_sink(output, sent.append)
                sink({'sensor': 'x', 'timestamp': 1.5, 'acc': 3})
                # Flushed per record: readable before the writer is closed.
                self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'sensor': 'x', 'timestamp': 1.5, 'acc': 3})
            self.assertEqual(sent, ['{"sensor": "x", "timestamp": 1.5, "acc": 3}'])

    def test_a_mistyped_udp_target_leaves_no_capture_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'capture.jsonl'
            result = subprocess.run([sys.executable, '-m', 'VRTsensors.bitalino', '--address', 'COM99',
                                     '--channel', 'A1=acc', '--udp', 'no-port-here', '--output', str(path)],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 1)
            self.assertIn('host:port', result.stderr)
            self.assertFalse(path.exists())

class RunnerIndependenceTests(unittest.TestCase):
    """VRTrunserver must run on a machine that has no sensor package installed."""

    def test_the_server_imports_without_the_sensor_package(self):
        load = importlib.util.spec_from_file_location('sensor_runner_alone', server_path)
        alone = importlib.util.module_from_spec(load)
        # None in sys.modules makes any `import VRTsensors` raise ImportError.
        with patch.dict(sys.modules, {'VRTsensors': None}):
            load.loader.exec_module(alone)
        self.assertTrue(callable(alone.start_sensors))

    def test_a_sensor_the_local_file_leaves_out_is_named(self):
        # A local file replaces the shared one, so a sensor listed only in sensors.json does
        # not run - which is how participant1 lost its EmotiBit without an error.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'sensors.json').write_text(json.dumps([
                "VRTsensors-emotibit --udp 127.0.0.1:9010 --output emotibit.jsonl",
                "VRTsensors-bitalino --address COM4 --channel A1=acc --output bitalino.jsonl"]))
            local = ["VRTsensors-bitalino --address COM5 --channel A1=eeg --output bitalino.jsonl"]
            (Path(tmp) / 'sensors.local.json').write_text(json.dumps(local))
            self.assertEqual(server.sensors_left_out(tmp, local), ['vrtsensors-emotibit'])
            self.assertEqual(server.sensors_left_out(tmp, local + [["C:\\venv\\Scripts\\VRTsensors-emotibit.exe"]]), [])
            (Path(tmp) / 'sensors.local.json').unlink()
            self.assertEqual(server.sensors_left_out(tmp, []), [], "with no local file nothing is left out")

    def test_the_server_and_the_package_choose_the_same_sensors_file(self):
        from VRTsensors import sensors_path
        for present in ([], ['sensors.json'], ['sensors.local.json'], ['sensors.json', 'sensors.local.json']):
            with tempfile.TemporaryDirectory() as tmp:
                for name in present:
                    (Path(tmp) / name).write_text('[]')
                self.assertEqual(server.sensors_path(tmp), sensors_path(tmp), present)

class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        server.SETTINGS.workdir = self.tmp.name
        self.real_popen = psutil.Popen
        self.started = []
        self.client = server.app.test_client()
        server.app.testing = True
        # The stand-in captures below are sleeps that ignore stdin; they need not be waited for.
        self.grace = patch.object(server, 'SENSOR_STOP_GRACE', 0.2)
        self.grace.start()
        self.addCleanup(self.grace.stop)
        # Most tests here start commands that exit at once on purpose (--help, a script that
        # writes a file); the start check has tests of its own below.
        self.start_check = patch.object(server, 'SENSOR_START_CHECK', 0)
        self.start_check.start()
        self.addCleanup(self.start_check.stop)

    def tearDown(self):
        server.stop_sensors()
        server.stop_processes(self.started)
        server.process = None
        self.tmp.cleanup()

    def commands(self, commands):
        (Path(self.tmp.name) / 'sensors.json').write_text(json.dumps(commands))

    def test_a_local_sensors_file_wins_over_the_shared_one(self):
        # The shared sensors.json is documentation and stays put; what is plugged into this
        # machine goes beside it. Editing the first to say the second is what silently took
        # one machine's EmotiBit out of the two-machine configuration.
        sleep = [sys.executable, '-c', 'import time; time.sleep(60)']
        (Path(self.tmp.name) / 'sensors.json').write_text(json.dumps([['nonexistent-shared-sensor']]))
        (Path(self.tmp.name) / 'sensors.local.json').write_text(json.dumps([sleep]))
        with patch.object(server.psutil, 'Popen', self.popen):
            server.start_sensors(self.tmp.name)
        self.assertEqual(len(server.sensor_processes), 1)
        server.stop_sensors()
        # And with no local file the shared one is still what runs.
        (Path(self.tmp.name) / 'sensors.local.json').unlink()
        self.commands([sleep])
        with patch.object(server.psutil, 'Popen', self.popen):
            server.start_sensors(self.tmp.name)
        self.assertEqual(len(server.sensor_processes), 1)

    def test_a_capture_is_asked_to_stop_before_it_is_killed(self):
        # A killed capture cannot be told from a crashed one, and a BITalino killed mid-read
        # is never told to stop. Closing its stdin lets it finish: a `stopped` record last.
        capture = shutil.which('VRTsensors-emotibit') or os.path.join(os.path.dirname(sys.executable), 'VRTsensors-emotibit')
        output = Path(self.tmp.name) / 'emotibit.jsonl'
        self.commands([[capture, '--stream', 'VRTsensorsNoSuchStream', '--output', str(output)]])
        with patch.object(server.psutil, 'Popen', self.popen), patch.object(server, 'SENSOR_STOP_GRACE', 10):
            server.start_sensors(self.tmp.name)
            deadline = time.time() + 20
            while time.time() < deadline and not (output.exists() and output.stat().st_size > 0):
                time.sleep(0.2)
            time.sleep(1.0)
            started = time.time()
            server.stop_sensors()
            took = time.time() - started
        last = json.loads(output.read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(last.get('event'), 'stopped', last)
        self.assertEqual(last.get('reason'), 'interrupted', last)
        self.assertLess(took, 10, 'it stopped by itself, before the grace ran out')

    def test_a_capture_that_exits_at_once_fails_the_start(self):
        # A capture gone within seconds - a bad argument, a malformed participant.json - used to
        # leave the player running a session with no sensor data and nothing said.
        self.commands([[sys.executable, '-c', 'import sys; print("bad participant.json"); sys.exit(1)']])
        with patch.object(server.psutil, 'Popen', self.popen), patch.object(server, 'SENSOR_START_CHECK', 1.0):
            with self.assertRaisesRegex(RuntimeError, 'exited at once .*bad participant.json'):
                server.start_sensors(self.tmp.name)
        self.assertEqual(server.sensor_processes, [], 'what had started is stopped')

    def test_a_used_working_folder_is_refused_before_anything_starts(self):
        # Two runs in the same minute share VRTrun's working folder; the second's captures would
        # refuse their non-empty outputs and exit, unseen.
        (Path(self.tmp.name) / 'bitalino.jsonl').write_text('{"sensor": "bitalino"}\n')
        self.commands([[sys.executable, '-c', 'import time; time.sleep(60)', '--output', 'bitalino.jsonl']])
        with patch.object(server.psutil, 'Popen', self.popen):
            with self.assertRaisesRegex(RuntimeError, 'already holds a recording'):
                server.start_sensors(self.tmp.name)
        self.assertEqual(self.started, [], 'nothing was started')

    def test_a_failed_start_is_a_readable_run_failure_and_no_player_starts(self):
        (Path(self.tmp.name) / 'bitalino.jsonl').write_text('{"sensor": "bitalino"}\n')
        self.commands([[sys.executable, '-c', 'import time; time.sleep(60)', '--output', 'bitalino.jsonl']])
        server.SETTINGS.executable = 'nonexistent-player-review'
        with patch.object(server.psutil, 'Popen', self.popen):
            response = self.client.post('/run')
        self.assertEqual(response.status_code, 500)
        self.assertIn(b'sensors did not start', response.data)
        self.assertEqual(self.started, [], 'neither a capture nor the player was started')

    def test_no_sensors_file_at_all_is_not_an_error(self):
        server.start_sensors(self.tmp.name)
        self.assertEqual(server.sensor_processes, [])

    def test_windows_powershell_utf8_config_is_accepted(self):
        (Path(self.tmp.name) / 'sensors.json').write_text('[]', encoding='utf-8-sig')
        server.start_sensors(self.tmp.name)
        self.assertEqual(server.sensor_processes, [])

    def popen(self, *args, **kwargs):
        proc = self.real_popen(*args, **kwargs)
        self.started.append(proc)
        return proc

    def assert_stopped(self):
        self.assertTrue(self.started)
        self.assertTrue(all(proc.poll() is not None for proc in self.started))
        self.assertEqual(server.sensor_processes, [])

    def test_second_sensor_launch_failure_reaps_first(self):
        self.commands([[sys.executable, '-c', 'import time; time.sleep(60)'], ['nonexistent-sensor-executable-review']])
        with patch.object(server.psutil, 'Popen', self.popen):
            with self.assertRaises(FileNotFoundError):
                server.start_sensors(self.tmp.name)
        self.assert_stopped()

    def test_unity_launch_failure_reaps_sensors_and_releases_run(self):
        self.commands([[sys.executable, '-c', 'import time; time.sleep(60)']])
        server.SETTINGS.executable = 'nonexistent-unity-executable-review'
        with patch.object(server.psutil, 'Popen', self.popen):
            with self.assertRaises(FileNotFoundError):
                self.client.post('/run')
        self.assert_stopped()
        self.assertFalse(server.run_lock.locked())

    def test_kill_works_without_unity(self):
        self.commands([[sys.executable, '-c', 'import time; time.sleep(60)']])
        with patch.object(server.psutil, 'Popen', self.popen):
            server.start_sensors(self.tmp.name)
        self.assertEqual(self.client.post('/kill').status_code, 200)
        self.assert_stopped()

    def test_collector_failure_reaps_player_and_sensors(self):
        self.commands([[sys.executable, '-c', 'import time; time.sleep(60)']])
        server.SETTINGS.executable = sys.executable
        def player_popen(args, **kwargs):
            if '-logFile' in args:
                args = [sys.executable, '-c', 'import time; time.sleep(60)']
            return self.popen(args, **kwargs)
        with patch.object(server.psutil, 'Popen', player_popen), patch.object(server, 'RUsageCollector', side_effect=RuntimeError('collector failed')):
            with self.assertRaises(RuntimeError):
                self.client.post('/run')
        self.assert_stopped()
        self.assertFalse(server.run_lock.locked())

    def test_normal_exit_reaps_sensors(self):
        self.commands([[sys.executable, '-c', 'import time; time.sleep(60)']])
        server.SETTINGS.executable = sys.executable
        def player_popen(args, **kwargs):
            if '-logFile' in args:
                args = [sys.executable, '-c', 'print("normal exit")']
            return self.popen(args, **kwargs)
        with patch.object(server.psutil, 'Popen', player_popen), patch.object(server, 'RUsageCollector'), patch.object(server, 'copy_stats_file'):
            result = self.client.post('/run')
        self.assertEqual(result.status_code, 200)
        self.assertIn(b'normal exit', result.data)
        self.assert_stopped()

    def test_sensor_launcher_children_are_reaped(self):
        child_pid = Path(self.tmp.name) / 'child.pid'
        code = 'import subprocess, sys, time; from pathlib import Path; child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]); Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(60)'
        self.commands([[sys.executable, '-c', code, str(child_pid)]])
        with patch.object(server.psutil, 'Popen', self.popen):
            server.start_sensors(self.tmp.name)
        deadline = time.monotonic() + 5
        while not child_pid.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(child_pid.exists())
        pid = int(child_pid.read_text())
        self.client.post('/kill')
        self.assertFalse(psutil.pid_exists(pid))
        self.assert_stopped()

    def test_command_beside_the_interpreter_is_found_without_path(self):
        # VRTrunserver and the sensor commands share a venv; a bare name must work when that venv is not on PATH.
        beside = [f for f in os.listdir(os.path.dirname(sys.executable)) if f.startswith('VRTsensors-bitalino')]
        self.assertTrue(beside, 'VRTsensors is not installed in this venv')
        self.commands([['VRTsensors-bitalino', '--help']])
        with patch.dict(os.environ, {'PATH': ''}), patch.object(server.psutil, 'Popen', self.popen):
            server.start_sensors(self.tmp.name)
        self.started[0].wait(10)
        self.assertEqual(self.started[0].returncode, 0)
        server.stop_sensors()
        self.commands([['no-such-sensor-command-review']])
        with patch.dict(os.environ, {'PATH': ''}):
            with self.assertRaisesRegex(FileNotFoundError, 'not found on PATH or beside'):
                server.start_sensors(self.tmp.name)

    @unittest.skipUnless(os.name == 'nt', 'Windows command-line quoting')
    def test_windows_command_line_preserves_path_with_spaces(self):
        script = Path(self.tmp.name) / 'sensor script.py'
        result = Path(self.tmp.name) / 'output file.txt'
        script.write_text('import pathlib, sys; pathlib.Path(sys.argv[1]).write_text("ok")')
        self.commands([subprocess.list2cmdline([sys.executable, str(script), str(result)])])
        with patch.object(server.psutil, 'Popen', self.popen):
            server.start_sensors(self.tmp.name)
        self.started[0].wait(5)
        self.assertEqual(result.read_text(), 'ok')

if __name__ == '__main__':
    unittest.main(verbosity=2)
