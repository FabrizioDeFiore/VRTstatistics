import os
import sys
import time
import subprocess
import shutil
import shlex
import json
import threading
import tempfile
from typing import Dict, Optional, List
import datetime
from flask import Flask, request, Response, jsonify, send_file
import platform
import psutil
import argparse
from importlib.metadata import version as _pkg_version

RunnerServerPort = 5002

# What this server does for sensor captures, by version: 2 starts sensors.json's captures,
# refuses a run whose captures would not record, and stops them by closing their stdin.
# CWI's server has none of this. A player's VRTrunserver.ps1 reads this line from the module
# the environment would import (without importing it), so it must stay a literal at the start
# of a line.
SENSOR_SUPPORT = 2

if platform.system() == "Windows":
    import WinTmp

app = Flask(__name__)

class Settings:
    def __init__(self):
        self.executable = "/Users/jack/src/VRTogether/VRTApp-Develop-built.app/Contents/MacOS/VR2Gather"
        self.topworkdir = "/Users/jack/tmp/VRTrunserver"
        self.workdir : Optional[str] = None

    def init_defaults(self):
        my_path = os.path.join(os.getcwd(), sys.argv[0])
        my_topdir = os.path.dirname(my_path)
        if sys.platform == "darwin":
            # On MacOS the VRTrunserver lives in the bundle where the VR2Gather executable is.
            # We create a temporary workdir in the same directory as the VR2Gather app.
            self.executable = os.path.join(my_topdir, "VR2Gather")
            app_dir = os.path.dirname(my_topdir) # Contents
            app_dir = os.path.dirname(app_dir) # VRTApp-Develop-built.app
            app_dir = os.path.dirname(app_dir) # Where the .app lives
            self.topworkdir = os.path.join(app_dir, "VRTrunserver-workdir")
        elif sys.platform == "win32":
            # On other platforms the VRTrunserver lives somewhere in the directory tree where the VR2Gather executable is.
            while not os.path.exists(os.path.join(my_topdir, "VR2Gather.exe")):
                next_topdir = os.path.dirname(my_topdir)
                if next_topdir == my_topdir:
                    # Not inside a build tree (a development venv, say): --executable has to say where the player is.
                    self.executable = None
                    self.topworkdir = None
                    return
                my_topdir = next_topdir
            # We create a temporary workdir in the same directory as the VR2Gather executable.
            self.executable = os.path.join(my_topdir, "VR2Gather.exe")
            self.topworkdir = os.path.join(my_topdir, "VRTrunserver-workdir")
        else:
            raise Exception(f"Unsupported platform {sys.platform}")

    def check_defaults(self):
        if not self.executable:
            raise Exception(f"No VR2Gather.exe found above {sys.argv[0]}; pass --executable to say where the player is")
        if not os.path.exists(self.executable):
            raise Exception(f"Executable {self.executable} does not exist")
        if not self.topworkdir:
            self.topworkdir = os.path.join(os.path.dirname(os.path.abspath(self.executable)), "VRTrunserver-workdir")
        if not os.path.exists(self.topworkdir):
            os.makedirs(self.topworkdir)
        print(f"VRTrunserver: executable={self.executable}")
        print(f"VRTRunserver: topworkdir={self.topworkdir}")

SETTINGS = Settings()

class RUsageCollector:
    def __init__(self, filename : str, proc : psutil.Process) -> None:
        self.fp = open(filename, 'w')
        self.proc = proc
        self.net_last_time = 0
        self.net_bytes_recv = 0
        self.net_bytes_sent = 0
        self._get_bandwidth()
        self.plt = platform.system()

    def close(self):
        self.fp.close()

    def step(self):
        now = datetime.datetime.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        ts = (now-midnight).total_seconds()
        try:
            cpu = self.proc.cpu_percent()
            mem = self.proc.memory_info()
        except psutil.ZombieProcess:
            return
        except psutil.NoSuchProcess:
            return
        allcpu = psutil.cpu_percent(percpu=True)
        maxcpu = max(allcpu)
        rss = mem.rss
        vms = mem.vms
        net = psutil.net_io_counters()
        recv, sent = self._get_bandwidth()
        stats_log = f'stats: component=ResourceConsumption, ts={ts:.3f}, cpu={cpu}, cpu_max={maxcpu}, mem={vms}, recv_bandwidth={recv:.0f}, sent_bandwidth={sent:.0f}'
        if self.plt == "Windows":
            temperature = WinTmp.CPU_Temp()
            stats_log += f', temperature={temperature}'
        print(stats_log, file=self.fp)

    def _get_bandwidth(self):
        net = psutil.net_io_counters()
        now = time.time()
        interval = now - self.net_last_time
        recv_bandwidth = (net.bytes_recv - self.net_bytes_recv) / interval
        sent_bandwidth = (net.bytes_sent - self.net_bytes_sent) / interval
        self.net_last_time = now
        self.net_bytes_recv = net.bytes_recv
        self.net_bytes_sent = net.bytes_sent
        return recv_bandwidth, sent_bandwidth

@app.route("/about")
def about():
    return "<p>Hello world!</p>"

@app.route("/start", methods=["POST"])
def start():
    args : Dict[str, str] = request.json
    print(f"start: {args}")
    SETTINGS.workdir = os.path.join(SETTINGS.topworkdir, args["workdir"])
    os.makedirs(SETTINGS.workdir, exist_ok=True)
    return Response("OK", mimetype="text/plain")

@app.route("/stop", methods=["GET", "POST"])
def stop():
    if SETTINGS.workdir == None:
        print("stop: start not called")
        return Response("400: stop: start not called", status=400)
    print("stop")
    SETTINGS.workdir = None
    return Response("OK", mimetype="text/plain")

process : Optional[psutil.Popen] = None


sensor_processes : List[psutil.Popen] = []
# Seconds a capture gets to stop by itself before it is killed.
SENSOR_STOP_GRACE = 5
# Seconds after starting the captures within which one that exits counts as failing to start;
# 0 skips the check.
SENSOR_START_CHECK = 2
process_lock = threading.RLock()
run_lock = threading.Lock()

def sensors_path(workdir : str) -> Optional[str]:
    """The sensors file for this run: sensors.local.json, a machine's own wiring, wins over
    the role's checked-in sensors.json. None when there is neither.

    The same rule as VRTsensors.sensors_path, kept here rather than imported so that the
    server does not depend on the sensor package: a machine that runs no sensors need not
    install it, or its Bluetooth and LSL dependencies. test_sensor_regressions.py checks
    that the two agree."""
    for name in ("sensors.local.json", "sensors.json"):
        filename = os.path.join(workdir, name)
        if os.path.exists(filename):
            return filename
    return None

def sensors_left_out(workdir : str, commands : list) -> List[str]:
    """The programs sensors.json starts that the sensors.local.json in use leaves out.

    A local file replaces the shared one rather than adding to it, so that a machine can
    leave a sensor out. That makes a sensor listed only in the shared file vanish without
    an error, which is why each one is named."""
    def program(command):
        args = shlex.split(command, posix=os.name != 'nt') if isinstance(command, str) else command
        return os.path.splitext(os.path.basename(args[0].strip('"\'')))[0].lower() if args else ''
    shared = os.path.join(workdir, "sensors.json")
    if sensors_path(workdir) == shared or not os.path.exists(shared):
        return []
    try:
        with open(shared, encoding='utf-8-sig') as fp:
            listed = json.load(fp)
    except (OSError, ValueError):
        return []
    if not isinstance(listed, list):
        return []
    running = {program(c) for c in commands}
    return sorted({program(c) for c in listed} - running - {''})

def output_argument(args : List[str]) -> Optional[str]:
    """The value of a capture command's --output, or None."""
    for index, arg in enumerate(args):
        if arg == '--output' and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith('--output='):
            return arg.split('=', 1)[1]
    return None

def start_sensors(workdir : str) -> None:
    """Start the sensor captures listed in <workdir>/sensors.json, if there is one.

    The file is a JSON list of command lines, uploaded by VRTrun from <config>/<role>/sensors.json,
    for example ["VRTsensors-bitalino --address COM5 --channel A1=acc --channel I1=btn --udp 127.0.0.1:9010 --output bitalino.jsonl"].
    They run in the workdir, so an --output without a directory lands beside stats.log and
    is fetched by VRTrun and merged by the VRTsensors.ingest annotation.

    A sensors.local.json uploaded alongside it wins - that is how a machine says what is
    plugged into it without editing the shared example. Which of the two was used is
    printed, because a stale one is otherwise invisible until the run comes back empty.
    """
    global sensor_processes
    filename = sensors_path(workdir)
    if filename is None:
        return
    print(f"run: sensors from {os.path.basename(filename)}")
    with open(filename, encoding='utf-8-sig') as fp:
        commands = json.load(fp)
    if not isinstance(commands, list):
        raise ValueError(f"{filename}: expected a list of commands")
    for program in sensors_left_out(workdir, commands):
        print(f"run: WARNING: sensors.json also starts {program}, which sensors.local.json does not; it will not run")
    with process_lock:
        if sensor_processes:
            raise RuntimeError("Sensor captures are already running")
        logs : List[str] = []
        try:
            for index, command in enumerate(commands):
                # Argument arrays preserve Windows paths without POSIX shell escaping.
                args = shlex.split(command, posix=os.name != 'nt') if isinstance(command, str) else command
                if isinstance(command, str) and os.name == 'nt':
                    args = [arg[1:-1] if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in ('"', "'") else arg for arg in args]
                if not isinstance(args, list) or not args or not all(isinstance(arg, str) for arg in args):
                    raise ValueError(f"{filename}: command {index + 1} must be a nonempty argument list or command line")
                # The sensor commands are installed in the same venv as this server, which need not be on PATH.
                found = shutil.which(args[0]) or shutil.which(args[0], path=os.path.dirname(sys.executable))
                if not found:
                    raise FileNotFoundError(f"{filename}: command {index + 1}: {args[0]} not found on PATH or beside {sys.executable}")
                args = [found] + args[1:]
                # A capture never overwrites a recording, so one aimed at a file an earlier run
                # left here would exit at once while the player ran on without it. That happens
                # when two runs start in the same minute: VRTrun names the working folder by the
                # minute, and the second run reuses the first one's folder.
                output = output_argument(args)
                if output is not None:
                    target = output if os.path.isabs(output) else os.path.join(workdir, output)
                    if os.path.exists(target) and os.path.getsize(target) > 0:
                        raise RuntimeError(f"{target} already holds a recording from an earlier run in this working folder "
                                           f"(two runs started in the same minute share one); start the run again in the next minute")
                print(f"run: sensor command={' '.join(args)}")
                stem = os.path.splitext(os.path.basename(args[0]))[0]
                log = os.path.join(workdir, f'{stem}-{index + 1}.log')
                with open(log, 'w') as logfile:
                    # The capture's stdin is held so that closing it asks it to stop; see stop_sensors.
                    env = dict(os.environ, VRTSENSORS_STOP_ON_STDIN_EOF='1')
                    sensor_processes.append(psutil.Popen(args, cwd=workdir, stdin=subprocess.PIPE, stdout=logfile, stderr=subprocess.STDOUT, env=env))
                logs.append(log)
            # A capture that is already gone - a bad argument, a malformed participant.json -
            # would otherwise be noticed only when its file comes back empty.
            if SENSOR_START_CHECK > 0:
                time.sleep(SENSOR_START_CHECK)
            for proc, log in zip(sensor_processes, logs if SENSOR_START_CHECK > 0 else []):
                if proc.poll() is not None:
                    with open(log, encoding='utf-8', errors='replace') as fp:
                        tail = fp.read()[-400:].strip()
                    raise RuntimeError(f"{os.path.basename(log)}: the capture exited at once (status {proc.returncode}): {tail}")
        except Exception:
            stop_sensors()
            raise

def stop_processes(processes : List[psutil.Popen]) -> None:
    """Reap captures and their launcher children, including Windows entry-point wrappers."""
    targets = []
    for proc in processes:
        try:
            targets.extend(proc.children(recursive=True))
            targets.append(proc)
        except psutil.NoSuchProcess:
            pass
    for proc in targets:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(targets, timeout=5)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(alive, timeout=5)

def stop_sensors() -> None:
    """Reap every capture this server started, and forget them.

    Safe with none running and safe to call twice: it is both the normal end of a run and
    the cleanup path when starting the captures fails partway through."""
    global sensor_processes
    with process_lock:
        # Ask first: a capture whose stdin closes stops the way Ctrl-C stops it, with the
        # device released and a final `stopped` record. Whatever is still there after a few
        # seconds is killed, as before.
        for proc in sensor_processes:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
        psutil.wait_procs(sensor_processes, timeout=SENSOR_STOP_GRACE)
        stop_processes(sensor_processes)
        sensor_processes = []

@app.route("/run", methods=["GET", "POST"])
def run():
    global process
    workdir = SETTINGS.workdir
    if workdir == None:
        print("run: start not called")
        return Response("400: run: start not called", status=400)
    if not run_lock.acquire(blocking=False):
        return Response("409: a run is already active", status=409)
    logfile = os.path.join(workdir, 'unity.log')
    command = [
        SETTINGS.executable,
        "-logFile", logfile
        ]
    configfile = os.path.join(workdir, 'config.json')
    if os.path.exists(configfile):
        command += ["-vrt-config", configfile]    
    print(f"run: command={' '.join(command)}")
    
    usage_collector = None
    try:
        # A file avoids filling an unread stdout pipe while Unity is running.
        with tempfile.TemporaryFile() as output:
            with process_lock:
                try:
                    start_sensors(workdir)
                except Exception as e:
                    # Before the player: a session without its sensor data is not started at all,
                    # and VRTrun prints the reason with this role's failure.
                    print(f"run: sensors did not start: {e}")
                    return Response(f"500: sensors did not start: {e}", status=500, mimetype="text/plain")
                process = psutil.Popen(command, stdout=output, stderr=subprocess.STDOUT)
                player = process
            usage_file = os.path.join(workdir, 'rusage.log')
            usage_collector = RUsageCollector(usage_file, player)
            while player.poll() == None:
                time.sleep(0.1)
                usage_collector.step()
            player.wait()
            output.seek(0)
            stdout_data = output.read()
    finally:
        try:
            with process_lock:
                if process is not None:
                    stop_processes([process])
                    process = None
                stop_sensors()
            if usage_collector is not None:
                usage_collector.close()
        finally:
            run_lock.release()
    #
    # Find the statsfilename and copy that file to the workdir
    #
    copy_stats_file(logfile, workdir)
    return Response(stdout_data, mimetype="text/plain")

@app.route("/kill", methods=["GET", "POST"])
def kill():
    global process
    with process_lock:
        active = process is not None or bool(sensor_processes)
        if process is not None:
            stop_processes([process])
        stop_sensors()
    return Response("OK" if active else "400: kill: no process", status=200 if active else 400, mimetype="text/plain")

def copy_stats_file(logfile : str, workdir : str):
    # See if we can find the stats file
    stats_filename = None
    for line in open(logfile):
        if line.startswith("stats:"):
            fields = line.split(",")
            for field in fields:
                field = field.strip()
                try:
                    key, value = field.split("=")
                    if key == "statsFilename":
                        stats_filename = value
                        break
                except ValueError:
                    print(f"WARNING: parse error in stats: line: {line}")
    if stats_filename:
        stats_dir = os.path.dirname(stats_filename)
        if os.path.samefile(stats_dir, workdir):
            print("Not copying statsfile, already in correct directory")
        else:
            print(f"Copy {stats_filename} to {workdir}")
            shutil.copy(stats_filename, workdir)

@app.route("/putfile", methods=["POST"])
def putfile():
    if SETTINGS.workdir == None:
        print("putfile: start not called")
        return Response("400: putfile: start not called", status=400)
    file = request.files['file']
    filename = file.filename
    if not filename:
        return Response("400: putfile: no filename", status=400)
    filename = os.path.join(SETTINGS.workdir, filename)
    dirname = os.path.dirname(filename)
    if not os.path.exists(dirname):
        os.makedirs(dirname)
    file.save(filename)
    full_filename = os.path.abspath(filename)
    print(f"put: return fullpath {full_filename}")
    rv = {"fullpath" : full_filename}
    return jsonify(rv)

@app.route("/get/<path:filename>")
def getfile(filename : str):
    if SETTINGS.workdir == None:
        print("getfile: start not called")
        return Response("400: getfile: start not called", status=400)
    
    print(f"get: {filename}")
    try:
        file = open(os.path.join(SETTINGS.topworkdir, filename), 'rb')
    except FileNotFoundError:
        print("failed")
        return Response(status=404)
    return send_file(file, mimetype="application/binary", download_name=filename, as_attachment=True)

@app.route("/listdir")
def listdir():
    if SETTINGS.workdir == None:
        print("listdir: start not called")
        return Response("400: listdir: start not called", status=400)
    files = []
    try:
        for dirpath, _, filenames in os.walk(SETTINGS.workdir):
            files : List[str] = []
            for filename in filenames:
                relpath = os.path.relpath(os.path.join(dirpath, filename), SETTINGS.topworkdir)
                relpath = relpath.replace("\\", "/")
                files.append(relpath)
    except FileNotFoundError:
        print("listdir: failed")
        return Response(status=404)
    print(f"listdir: {len(files)} files: {files}")
    return jsonify(files)

def main():
    print("WARNING: this is a dangerous server that allows executing anything on this machine.")
    SETTINGS.init_defaults()
    parser = argparse.ArgumentParser(description="Run a VR2Gather player server")
    parser.add_argument("--version", action="version", version=f"%(prog)s {_pkg_version('VRTrunserver')}")
    parser.add_argument("--executable", metavar="EXE", default=SETTINGS.executable, help="Executable to run (default: %(default)s)")
    parser.add_argument("--topworkdir", metavar="DIR", default=SETTINGS.topworkdir, help="Top work directory (default: %(default)s)")
    parser.add_argument("--pausefordebug", action="store_true", help="Pause before starting to allow attaching a debugger")
    args = parser.parse_args()
    if args.pausefordebug:
        sys.stderr.write(f"Attach debugger to pid={os.getpid()}. Press return to continue - ")
        sys.stderr.flush()
        sys.stdin.readline()
    SETTINGS.executable = args.executable
    SETTINGS.topworkdir = args.topworkdir
    SETTINGS.check_defaults()
    app.run(host='0.0.0.0', port=RunnerServerPort)

if __name__ == '__main__':
    main()
