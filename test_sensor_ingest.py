#!/usr/bin/env python3
"""
Test for VRTsensors.ingest: merging sensor captures into a VRTstatistics session.

Covers the part that is easy to get silently wrong and impossible to see in a plot:
whether a capture written against one machine's wall clock ends up on the session
clock, with the role's measured offset applied in the right direction.

  python test_sensor_ingest.py
"""
import json
import os
import sys
import tempfile

from VRTstatistics.annotation import engine
from VRTstatistics.datastore import DataStore, DataStoreError
from VRTsensors.ingest import SensorAnnotation

SESSION_START = 1000.0
DESYNC = {"sender": 0.5, "receiver": -0.25}

failures = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global failures
    if condition:
        print(f"  ok    {label}")
    else:
        failures += 1
        print(f"  FAIL  {label}{': ' + detail if detail else ''}")


def _session(workdir: str, captures: dict) -> DataStore:
    """A DataStore as SessionNormalizer would leave it, with captures written beside it."""
    ds = DataStore(os.path.join(workdir, "combined.json"))
    ds.data = [
        {"sessiontime": 0.0, "role": "sender", "component": "OrchestratorController"},
        {"sessiontime": 20.0, "role": "receiver", "component": "OrchestratorController"},
    ]
    ds.session_metadata = {
        "session_start_time": SESSION_START,
        "roles": ["sender", "receiver"],
        "desyncs": dict(DESYNC),
        "component_map": {"sender": {"OrchestratorController": "sender.orch"}, "receiver": {}},
    }
    for role, (filename, records) in captures.items():
        os.makedirs(os.path.join(workdir, role), exist_ok=True)
        with open(os.path.join(workdir, role, filename), "w") as fp:
            for record in records:
                fp.write(json.dumps(record) + "\n")
    return ds


def _bitalino(timestamp: float, **fields) -> dict:
    return {"sensor": "bitalino", "device": "COM5", "timestamp": timestamp, **fields}


def test_session_clock():
    print("session clock:")
    with tempfile.TemporaryDirectory() as workdir:
        ds = _session(workdir, {"sender": ("bitalino.jsonl", [
            _bitalino(SESSION_START + 10.0, event="connected"),
            _bitalino(SESSION_START + 10.5, seq=0, eda=500),
            _bitalino(SESSION_START + 10.51, seq=1, eda=501),
        ])})
        result = SensorAnnotation().apply(ds)

        merged = [r for r in ds.data if r.get("sensor") == "bitalino"]
        check("every record merged", len(merged) == 3, f"{len(merged)} records")
        sample = next(r for r in merged if "seq" in r)
        # 10.5s after session start on the sender's clock, and that clock is 0.5s behind
        # the orchestrator, so the session saw it at 11.0s.
        check("role desync applied to sessiontime", abs(sample["sessiontime"] - 11.0) < 1e-9, repr(sample["sessiontime"]))
        check("orchtime recorded alongside", abs(sample["orchtime"] - (SESSION_START + 11.0)) < 1e-9, repr(sample["orchtime"]))
        check("original timestamp preserved", sample["timestamp"] == SESSION_START + 10.5, repr(sample["timestamp"]))
        check("role stamped", all(r["role"] == "sender" for r in merged))
        check("component is the sensor", sample["component"] == "bitalino", repr(sample["component"]))
        check("component_role names role and sensor", sample["component_role"] == "sender.sensor.bitalino", repr(sample["component_role"]))

        times = [r["sessiontime"] for r in ds.data]
        check("datastore left sorted", times == sorted(times), repr(times))

        capture = result["captures"]["sender/bitalino.jsonl"]
        check("capture summary counts samples and events", capture["samples"] == 2 and capture["events"] == 1, repr(capture))


def test_negative_desync_and_two_roles():
    print("two roles:")
    with tempfile.TemporaryDirectory() as workdir:
        ds = _session(workdir, {
            "sender": ("bitalino.jsonl", [_bitalino(SESSION_START + 10.0, seq=0, eda=500)]),
            "receiver": ("emotibit.jsonl", [
                {"sensor": "emotibit", "stream": "EmotiBit EDA", "timestamp": SESSION_START + 10.0, "value": 1.5},
                {"sensor": "emotibit", "stream": "EmotiBit HR", "timestamp": SESSION_START + 10.0, "value": 70.0},
            ]),
        })
        SensorAnnotation().apply(ds)

        sender = next(r for r in ds.data if r.get("sensor") == "bitalino")
        receiver = next(r for r in ds.data if r.get("sensor") == "emotibit")
        check("sender ahead of the orchestrator's view", abs(sender["sessiontime"] - 10.5) < 1e-9, repr(sender["sessiontime"]))
        # The receiver's clock runs 0.25s ahead, so the same instant is earlier in session time.
        check("negative desync moves the other way", abs(receiver["sessiontime"] - 9.75) < 1e-9, repr(receiver["sessiontime"]))

        components = {r["component"] for r in ds.data if r.get("sensor") == "emotibit"}
        check("each EmotiBit stream is its own component", components == {"emotibit.EmotiBit EDA", "emotibit.EmotiBit HR"}, repr(components))


def test_records_before_the_session():
    print("capture started before the session:")
    with tempfile.TemporaryDirectory() as workdir:
        ds = _session(workdir, {"sender": ("bitalino.jsonl", [
            _bitalino(SESSION_START - 5.0, event="connected"),
            _bitalino(SESSION_START + 1.0, seq=0, eda=500),
        ])})
        result = SensorAnnotation().apply(ds)

        capture = result["captures"]["sender/bitalino.jsonl"]
        check("early records kept, not dropped", capture["records"] == 2, repr(capture))
        check("early records reported", capture["before_session_start"] == 1, repr(capture))


def test_refusals():
    print("refusals:")
    with tempfile.TemporaryDirectory() as workdir:
        ds = _session(workdir, {})
        try:
            SensorAnnotation().apply(ds)
            check("empty session refused", False, "no error raised")
        except DataStoreError as e:
            check("empty session refused", "No sensor records found" in str(e), str(e))

    with tempfile.TemporaryDirectory() as workdir:
        ds = _session(workdir, {"sender": ("bitalino.jsonl", [{"timestamp": SESSION_START, "value": 1}])})
        try:
            SensorAnnotation().apply(ds)
            check("foreign JSON lines refused", False, "no error raised")
        except DataStoreError as e:
            check("foreign JSON lines refused", "not a VRTsensors record" in str(e), str(e))

    with tempfile.TemporaryDirectory() as workdir:
        ds = _session(workdir, {"sender": ("bitalino.jsonl", [_bitalino(SESSION_START)])})
        with open(os.path.join(workdir, "sender", "bitalino.jsonl"), "a") as fp:
            fp.write("{not json\n")
        try:
            SensorAnnotation().apply(ds)
            check("malformed line refused with its line number", False, "no error raised")
        except DataStoreError as e:
            check("malformed line refused with its line number", "bitalino.jsonl:2" in str(e), str(e))


def test_through_the_engine():
    print("through the annotation engine:")
    with tempfile.TemporaryDirectory() as workdir:
        ds = _session(workdir, {"sender": ("bitalino.jsonl", [_bitalino(SESSION_START + 1.0, seq=0, eda=500)])})
        engine.ensure(ds, "sensors")

        check("annotation recorded as applied", "sensors" in ds.applied_annotations)
        check("dependency applied first", "component_role" in ds.applied_annotations)
        existing = next(r for r in ds.data if r.get("component") == "OrchestratorController")
        check("existing records still stamped by component_role", existing.get("component_role") == "sender.orch", repr(existing))
        sensor = next(r for r in ds.data if r.get("sensor") == "bitalino")
        check("sensor component_role survived the dependency", sensor["component_role"] == "sender.sensor.bitalino", repr(sensor))


def test_capture_format():
    print("capture format:")
    from VRTsensors import FORMAT
    with tempfile.TemporaryDirectory() as workdir:
        header = _bitalino(SESSION_START + 9.0, event="capture", format=FORMAT, version="1.2.3")
        ds = _session(workdir, {"sender": ("bitalino.jsonl", [header, _bitalino(SESSION_START + 10.0, seq=0, eda=1)])})
        capture = SensorAnnotation().apply(ds)["captures"]["sender/bitalino.jsonl"]
        check("the summary says which format and version wrote it", capture.get("format") == FORMAT and capture.get("version") == "1.2.3", repr(capture))
    with tempfile.TemporaryDirectory() as workdir:
        ds = _session(workdir, {"sender": ("bitalino.jsonl", [_bitalino(SESSION_START + 10.0, seq=0, eda=1)])})
        capture = SensorAnnotation().apply(ds)["captures"]["sender/bitalino.jsonl"]
        check("a capture with no header is format 1", capture.get("format") == 1, repr(capture))
    with tempfile.TemporaryDirectory() as workdir:
        newer = _bitalino(SESSION_START + 9.0, event="capture", format=FORMAT + 1)
        ds = _session(workdir, {"sender": ("bitalino.jsonl", [newer, _bitalino(SESSION_START + 10.0, seq=0, eda=1)])})
        try:
            SensorAnnotation().apply(ds)
            check("a newer format is refused", False)
        except DataStoreError as e:
            check("a newer format is refused", "newer" in str(e), str(e))


def test_participants():
    print("participants:")
    header = lambda who: _bitalino(SESSION_START + 9.0, event="capture", format=2, participant=who)
    with tempfile.TemporaryDirectory() as workdir:
        ds = _session(workdir, {"sender": ("bitalino.jsonl", [header("P07"), _bitalino(SESSION_START + 10.0, seq=0, eda=1)])})
        result = SensorAnnotation().apply(ds)
        sensor = [r for r in ds.data if r.get("sensor") == "bitalino"]
        check("every merged record carries its participant", all(r.get("participant") == "P07" for r in sensor), repr(sensor))
        check("the summary maps role to participant", result.get("participants") == {"sender": "P07"}, repr(result.get("participants")))
        check("records of other components are left alone", all("participant" not in r for r in ds.data if r.get("sensor") is None))
    with tempfile.TemporaryDirectory() as workdir:
        ds = _session(workdir, {"sender": ("bitalino.jsonl", [header("P07"), _bitalino(SESSION_START + 10.0, seq=0, eda=1)])})
        with open(os.path.join(workdir, "sender", "emotibit.jsonl"), "w") as fp:
            fp.write(json.dumps({"sensor": "emotibit", "stream": "", "timestamp": SESSION_START + 9.0, "event": "capture", "format": 2, "participant": "P08"}) + "\n")
            fp.write(json.dumps({"sensor": "emotibit", "stream": "EDA", "timestamp": SESSION_START + 10.0, "value": 1.0}) + "\n")
        try:
            SensorAnnotation().apply(ds)
            check("two participants on one computer are refused", False)
        except DataStoreError as e:
            check("two participants on one computer are refused", "P08" in str(e) and "P07" in str(e), str(e))


def test_one_person_per_device():
    print("devices and participants:")
    # The review's case: both EmotiBits visible to both computers, each capture recording both,
    # gave (P01, A), (P01, B), (P02, A) and (P02, B).
    head = lambda who, sources: {"sensor": "emotibit", "stream": "", "timestamp": SESSION_START + 9.0, "event": "capture",
                                 "format": 2, "participant": who, "sources": sources}
    eda = lambda device, value: {"sensor": "emotibit", "stream": "EDA", "source_id": device, "stream_uid": device + "-1",
                                 "timestamp": SESSION_START + 10.0 + value, "value": value}
    cases = (
        ("both captures recording both devices",
         {"sender": [head("P01", []), eda("MD-A", 1), eda("MD-B", 2)], "receiver": [head("P02", []), eda("MD-A", 1), eda("MD-B", 2)]},
         "2 devices"),
        ("one device in two participants' captures",
         {"sender": [head("P01", []), eda("MD-A", 1)], "receiver": [head("P02", []), eda("MD-A", 1)]},
         "MD-A"),
    )
    for label, roles, expected in cases:
        with tempfile.TemporaryDirectory() as workdir:
            ds = _session(workdir, {role: ("emotibit.jsonl", records) for role, records in roles.items()})
            try:
                SensorAnnotation().apply(ds)
                check(f"{label} is refused", False)
            except DataStoreError as e:
                check(f"{label} is refused", expected in str(e), str(e))
    with tempfile.TemporaryDirectory() as workdir:
        ds = _session(workdir, {"sender": ("emotibit.jsonl", [head("P01", ["MD-A"]), eda("MD-A", 1)]),
                                "receiver": ("emotibit.jsonl", [head("P02", ["MD-B"]), eda("MD-B", 2)])})
        SensorAnnotation().apply(ds)
        pairs = sorted({(r["participant"], r["source_id"]) for r in ds.data if r.get("stream") == "EDA"})
        check("bound captures give each participant their own device", pairs == [("P01", "MD-A"), ("P02", "MD-B")], repr(pairs))
    with tempfile.TemporaryDirectory() as workdir:
        port = lambda who: [_bitalino(SESSION_START + 9.0, event="capture", format=2, participant=who, device="COM4"),
                            _bitalino(SESSION_START + 10.0, seq=0, eda=1, device="COM4")]
        ds = _session(workdir, {"sender": ("bitalino.jsonl", port("P01")), "receiver": ("bitalino.jsonl", port("P02"))})
        try:
            SensorAnnotation().apply(ds)
            check("two computers' BITalinos on the same port name are not one device", True)
        except DataStoreError as e:
            check("two computers' BITalinos on the same port name are not one device", False, str(e))


if __name__ == "__main__":
    test_one_person_per_device()
    test_participants()
    test_capture_format()
    test_session_clock()
    test_negative_desync_and_two_roles()
    test_records_before_the_session()
    test_refusals()
    test_through_the_engine()
    print(f"\n{'FAILED: ' + str(failures) + ' check(s)' if failures else 'all checks passed'}")
    sys.exit(1 if failures else 0)
