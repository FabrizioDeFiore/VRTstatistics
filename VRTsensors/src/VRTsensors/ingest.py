from __future__ import annotations
import glob
import json
import math
import os
import warnings
from typing import Any, Dict, List, Tuple

from VRTstatistics.annotation import AnnotationStep, engine
from VRTstatistics.datastore import DataStore, DataStoreError, DataStoreRecord

from . import FORMAT

__all__ = ["SensorAnnotation"]


class SensorAnnotation(AnnotationStep):
    """
    Merges VRTsensors captures into a session, on the session clock.

    A capture is found the way the per-role logs are found: at
    <workdir>/<role>/<something>.jsonl, next to that role's stats.log. The role
    therefore names the machine the sensor was attached to, which is what decides
    the clock correction; nothing has to be configured.
    """
    name = "sensors"
    # component_role first: it stamps the records already in the DataStore, and would
    # blank the field on ours if it ran afterwards.
    dependencies = ["component_role"]
    description = "Merge VRTsensors JSON-lines captures found at <workdir>/<role>/*.jsonl into the session."
    params = {
        "pattern": "Filename pattern to look for in each role directory (default: *.jsonl)",
    }

    def apply(self, ds: DataStore, **params) -> Dict[str, Any]:
        """
        Merge every capture found beside the roles' logs into `ds`, on the session clock.

        Returns the per-capture summary that lands in the combined file's annotations:
        record, sample and event counts, and the session-time span, per capture. Those
        counts are what a reader reconciles against the raw files, so they count what was
        merged rather than what was read.

        Raises DataStoreError when the store has no filename (there is then nowhere to look
        for captures) or when SessionNormalizer has not run (there is then no session start
        time and no per-role offset to apply, and guessing either would be worse than
        failing).
        """
        if not ds.filename:
            raise DataStoreError("Sensor captures are looked for next to the combined file, which has no filename")
        workdir = os.path.dirname(ds.filename)
        pattern = params.get("pattern", "*.jsonl")

        session_start_time = ds.session_metadata.get("session_start_time")
        if session_start_time is None:
            raise DataStoreError("session_start_time not found in session_metadata — was SessionNormalizer run?")
        desyncs = ds.session_metadata.get("desyncs", {})

        captures: Dict[str, Any] = {}
        merged: List[DataStoreRecord] = []
        participants: Dict[str, str] = {}
        owners: Dict[str, Tuple[str, str]] = {}
        for role in ds.session_metadata.get("roles", []):
            for path in sorted(glob.glob(os.path.join(workdir, role, pattern))):
                records = self._load(path, role, desyncs.get(role, 0.0), session_start_time)
                name = os.path.relpath(path, workdir).replace(os.sep, "/")
                summary = self._describe(role, records)
                # One participant per computer: a role whose captures name two participants
                # would put one person's signals under another's ID.
                participant = summary.get("participant")
                if participant is not None:
                    if participants.setdefault(role, participant) != participant:
                        raise DataStoreError(f"{name}: participant {participant!r}, but {role}'s other captures say {participants[role]!r}")
                    # And one person per device: a capture that took in another computer's
                    # device (LSL shows every outlet on the network) would otherwise file that
                    # person's signals under this one.
                    devices = sorted({r.get("device") or r.get("source_id") for r in records
                                      if "event" not in r and (r.get("device") or r.get("source_id"))})
                    if len(devices) > 1:
                        raise DataStoreError(f"{name}: samples from {len(devices)} devices ({', '.join(devices)}) under participant {participant!r}; "
                                             f"bind each capture to its own device (VRTsensors-emotibit --source)")
                    # Only identities unique across computers: an EmotiBit's device ID is, but a
                    # BITalino's `device` is its port, and two computers may both say COM4.
                    for device in sorted({r["source_id"] for r in records if "event" not in r and r.get("source_id")}):
                        owner = owners.setdefault(device, (participant, name))
                        if owner[0] != participant:
                            raise DataStoreError(f"{name}: device {device} is participant {participant!r}'s, but {owner[1]} has it as {owner[0]!r}")
                    for record in records:
                        record["participant"] = participant
                merged.extend(records)
                captures[name] = summary

        if not merged:
            raise DataStoreError(f"No sensor records found: no role directory under {workdir} contains {pattern}")

        ds.data.extend(merged)
        ds.sort(key=lambda r: r["sessiontime"])
        return {"captures": captures, "records": len(merged), "participants": participants}

    def _load(self, path: str, role: str, desync: float, session_start_time: float) -> List[DataStoreRecord]:
        """Read one capture and put its records on the session clock."""
        records: List[DataStoreRecord] = []
        with open(path, encoding="utf-8") as fp:
            for number, line in enumerate(fp, 1):
                terminated = line.endswith('\n')
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as e:
                    if not terminated:
                        warnings.warn(f"{path}:{number}: ignoring incomplete final record after capture interruption", RuntimeWarning)
                        break
                    raise DataStoreError(f"{path}:{number}: {e}")
                if not isinstance(record, dict) or "timestamp" not in record or "sensor" not in record:
                    raise DataStoreError(f"{path}:{number}: not a VRTsensors record (no timestamp or no sensor field)")
                if not isinstance(record['timestamp'], (int, float)) or isinstance(record['timestamp'], bool) or not math.isfinite(record['timestamp']):
                    raise DataStoreError(f"{path}:{number}: timestamp must be a finite number")
                # A newer format may mean a field this code would misread, so it is refused,
                # as DataStore refuses a combined.json newer than its FILEVERSION.
                if record.get("event") == "capture" and isinstance(record.get("format"), int) and record["format"] > FORMAT:
                    raise DataStoreError(f"{path}:{number}: capture format {record['format']} is newer than this VRTsensors understands ({FORMAT}); update it")
                # The capture carries the wall clock of the machine it ran on, so it needs
                # the correction the normalizer applied to that role's own logs: the
                # orchestrator's clock is the session timebase, and this role's is
                # desync seconds behind it.
                record["orchtime"] = record["timestamp"] + desync
                record["sessiontime"] = record["orchtime"] - session_start_time
                record["role"] = role
                # The pipeline keys on component; a sensor is one, and streams within a
                # multi-rate device (EmotiBit) are separate ones.
                stream = record.get("stream")
                record["component"] = f"{record['sensor']}.{stream}" if stream else record["sensor"]
                record["component_role"] = f"{role}.sensor.{record['component']}"
                records.append(record)
        return records

    def _describe(self, role: str, records: List[DataStoreRecord]) -> Dict[str, Any]:
        """Summarise one capture, including how much of it falls outside the session."""
        if not records:
            warnings.warn(f"Sensor capture for {role} contains no complete records", RuntimeWarning)
            return {"role": role, "sensor": None, "format": None, "version": None, "revision": None, "dirty": None, "participant": None, "session": None, "records": 0, "samples": 0, "events": 0,
                    "before_session_start": 0, "first_sessiontime": None, "last_sessiontime": None}
        events = sum(1 for r in records if "event" in r)
        before = sum(1 for r in records if r["sessiontime"] < 0)
        header = records[0] if records[0].get("event") == "capture" else {}
        return {
            "role": role,
            "sensor": records[0]["sensor"],
            # Which timestamps these are: 1 is batch arrival for a BITalino, 2 its sample count.
            "format": header.get("format", 1),
            "version": header.get("version"),
            "revision": header.get("revision"),
            "dirty": header.get("dirty"),
            "participant": header.get("participant"),
            "session": header.get("session"),
            "records": len(records),
            "samples": len(records) - events,
            "events": events,
            "before_session_start": before,
            "first_sessiontime": round(records[0]["sessiontime"], 3),
            "last_sessiontime": round(records[-1]["sessiontime"], 3),
        }


engine.register(SensorAnnotation)
