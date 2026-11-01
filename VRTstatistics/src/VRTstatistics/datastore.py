from __future__ import annotations
import ntpath
import os
import sys
import json
from typing import Optional, List, Any, cast, Dict, Union
from types import CodeType
from .parser import StatsFileParser
import pandas

__all__ = ["DataStoreRecord", "DataStore", "DataStoreError", "scene_predicate"]

# Combined-JSON file versioning.
# FILEVERSION is written into every saved file.
# OLDEST_COMPATIBLE_VERSION is the oldest version this code can read.
# Bump FILEVERSION (and OLDEST_COMPATIBLE_VERSION if the change is breaking)
# whenever the on-disk schema changes. Use the date of the change as an integer
# (YYYYMMDD). If two incompatible changes happen on the same day, use YYYYMMDD+1
# for the second one.
FILEVERSION = 20261030
OLDEST_COMPATIBLE_VERSION = 20260531

def scene_predicate(scene: Any) -> str:
    """
    Return a predicate for the records within a scene, a row of DataStore.scenes().
    A row rather than a name, because a session can have several scenes with the same name.

    Components can log a few ms after the scene's end while they are being destroyed
    (e.g. a GazeTarget's final gazing=0), so such records fall outside the scene.
    """
    t_start = float(scene["t_start"])
    if pandas.isna(scene["t_end"]):
        return f"sessiontime >= {t_start}"
    return f"{t_start} <= sessiontime <= {float(scene['t_end'])}"


# Fields of the VRTConfig stats record that VR2Gather logs at startup.
VERSION_FIELDS = ("app", "app_version", "vr2gather", "cwipc_unity", "unity", "platform")

_ROLE_COLORS = ['tab:blue', 'tab:orange', 'tab:green', 'tab:red']


class DataStoreError(RuntimeError):
    pass

type DataStoreRecord = dict[str, Any]
"""
A single entry in a DataStore.
"""

type Predicate = Union[str, CodeType]
"""
A Predicate is a boolean Python expression, evaluated with a DataStoreRecord as its globals.

Field names can be used in the expression, and the special name "record" signifies the whole record.
"""

type FieldSpecifier = str
"""
A FieldSpecifier specifies now an input record field is mapped to an output record field.

The simplest form is just an identifier: the field with that name will be in the output recurd under the same name.
The form "out=in" will take the value of input field "out" and use that as the output record field name for the value of "in".
The form "out1.out2=in" will take the value of input field "out1" and "out2", concatenate them with a "." and use that as the output record field name for the value of "in".
The form "out1.=in" will take the value of input field "out1", concatenate that with a "." and the name of the input field, and use that as the output record field name for the value of "in".

"""
class DataStore:
    """
    All data obtained from a single run.

    Can be loaded from various file formats (and saved to it).
    Can be filtered and searched.
    Can be accessed as Pandas DataFrame.
    """
    debug = True
    verbose = True

    filename: Optional[str]
    data: list[DataStoreRecord]
    session_metadata: Dict[str, Any]
    applied_annotations: Dict[str, Any]

    def __init__(self, filename: Optional[str] = None, filename2: Optional[str] = None) -> None:
        """
        Create a DataStore, does not load anything yet.

        :param filename: The filename to load from (and save to). Can be json or stats-style log.
        :type filename: Optional[str]
        :param filename2: Optional second file to load, for some filetypes.
        :type filename2: Optional[str]
        """
        self.filename = filename
        self.filename2 = filename2
        self.data = []
        self.session_metadata = {}
        self.applied_annotations = {}

    def load(self) -> None:
        """
        Load the datastore from the filename(s) passed during creation
        """
        assert self.filename
        if self.filename == "-":
            pass
        elif self.filename.endswith(".json"):
            self._load_json()
        elif self.filename.endswith(".log"):
            self._load_log()
        else:
            raise DataStoreError(f"Don't know how to load {self.filename}")

    def _load_json(self) -> None:
        assert self.filename
        assert not self.filename2
        raw = json.load(open(self.filename, "r"))
        if isinstance(raw, list):
            # Bare list — no metadata at all
            self.data = raw
        elif "session" in raw:
            # New schema (with or without fileversion)
            fv = raw.get("fileversion")
            if fv is not None:
                if fv < OLDEST_COMPATIBLE_VERSION:
                    raise DataStoreError(
                        f"{self.filename}: fileversion {fv} is older than oldest supported {OLDEST_COMPATIBLE_VERSION}"
                    )
                if fv > FILEVERSION:
                    raise DataStoreError(
                        f"{self.filename}: fileversion {fv} is newer than this code ({FILEVERSION}); upgrade VRTstatistics"
                    )
            self.session_metadata = raw.get("session", {})
            self.applied_annotations = raw.get("annotations", {})
            self.data = raw["data"]
        elif "metadata" in raw:
            # Old schema — backward-compat conversion
            self.data = raw["data"]
            self._load_old_metadata(raw["metadata"])
        else:
            self.data = raw.get("data", raw)

    def _load_old_metadata(self, metadata: Dict[str, Any]) -> None:
        """Convert old annotator to_dict() metadata into new session/annotations schema."""
        ann_type = metadata.get("type", "")
        roles = []
        if "role" in metadata:
            roles = [metadata["role"]]

        self.session_metadata = {
            "session_id": metadata.get("session_id", metadata.get("sessionId", "")),
            "session_start_time": metadata.get("session_start_time", 0),
            "roles": roles,
            "user_names": {metadata["role"]: metadata.get("user_name")} if "role" in metadata else {},
            "desyncs": {metadata["role"]: metadata.get("desync", 0)} if "role" in metadata else {},
            "component_map": {},
            "role_topology": {},
        }

        if "LatencyCombined" in ann_type or "Combined" in ann_type:
            sender_user = metadata.get("sender")
            receiver_user = metadata.get("receiver")
            self.session_metadata["roles"] = ["sender", "receiver"]
            self.session_metadata["user_names"] = {"sender": sender_user, "receiver": receiver_user}
            self.applied_annotations["component_role"] = {"roles": ["sender", "receiver"]}
            self.applied_annotations["latency"] = {
                "sender": "sender",
                "receiver": "receiver",
                "protocol": metadata.get("protocol"),
                "nTiles": metadata.get("nTiles", 1),
                "nQualities": metadata.get("nQualities", 1),
                "compressed": metadata.get("compressed", False),
                "sender_user": sender_user,
                "receiver_user": receiver_user,
                "desync": metadata.get("desync", 0),
                "desync_uncertainty": metadata.get("desync_uncertainty", 0),
            }

    def _load_log(self, nocheck : bool=False) -> None:
        assert self.filename
        parser = StatsFileParser(self.filename, self.filename2)
        self.data = parser.parse()
        if not nocheck:
            parser.check()

    def load_data(self, data : List[DataStoreRecord] | pandas.DataFrame) -> None:
        """
        Load data from a list of DataStoreRecords or a pandas DataFrame
        :param data: The content to load into this DataStore
        :type data: List[DataStoreRecord] | pandas.DataFrame
        """
        if hasattr(data, 'to_dict'):
            df : pandas.DataFrame = cast(pandas.DataFrame, data)
            data = df.to_dict('records') # type: ignore
        self.data = cast(List[DataStoreRecord],data)

    def get_dataframe(
        self, predicate: Optional[Predicate] = None, fields: Optional[List[FieldSpecifier]] = None
    ) -> pandas.DataFrame:
        """
        Return the DataStore as a pandas DataFrame.
        
        :param predicate: A boolean predicate function. Only records that match this predicate are included in the output. If no predicate is specified all records are returned.
        :type predicate: Predicate
        :param fields: A list of fields wanted in the returned DataFrame records. If not specified all fields are included.
        :type fields: Optional[List[FieldSpecifier]]
        :return: The resultant DataFrame
        :rtype: DataFrame
        """
        if not self.data:
            raise DataStoreError("DataStore is empty")
        if predicate or fields:
            data = self._filter_data(predicate, fields)
        else:
            data = self.data
        rv = pandas.DataFrame(data)
        return rv

    def _filter_data(self, predicate: Any, fields: Optional[List[str]]) -> List[DataStoreRecord]:
        """
        Inputdata is a list of dictionaries, they are filtered and the resulting list of dictionaries is returned.
        predicate is a Python expression returning True or False, if True the record is output.
        fields is a list of fieldnames to include in the output,
        use namefield=field to obtain field name from namefield
        """
        rv : List[DataStoreRecord] = []
        for record in self.data:
            nsrecord = dict(record) # shallow copy
            nsrecord["record"] = nsrecord
            if predicate == None or eval(predicate, nsrecord):
                if fields:
                    entry : Dict[Any, Any] = dict()
                    for k in fields:
                        if "=" in k:
                            newk, oldk = k.split("=")
                            if "." in newk:
                                # Use field1.field2=field notation
                                newk1, newk2 = newk.split(".")
                                if not newk1 in record:
                                    continue
                                if not newk2:
                                    newk = record[newk1] + "." + oldk
                                else:
                                    if not newk2 in record:
                                        continue
                                    newk = record[newk1] + "." + record[newk2]
                            else:
                                if not newk in record:
                                    continue
                                newk = record[newk]
                            if not newk:
                                print(f'Warning: "{k}" produced no value for {record}', file=sys.stderr)
                        else:
                            newk = oldk = k

                        if oldk in record:
                            entry[newk] = record[oldk]
                else:
                    entry = record
                rv.append(entry)
        if len(rv) == 0:
            print(f"_filter_data: Warning: empty dataset for fields={fields}, predicate: {predicate}")
        return rv

    def save(self) -> None:
        """
        Save the DataStore to its JSON filename.
        """
        if not self.data:
            raise DataStoreError("DataStore is empty")
        assert self.filename
        if self.filename.endswith(".json"):
            self._save_json()
        else:
            raise DataStoreError(f"Don't know how to save {self.filename}")

    def _save_json(self) -> None:
        out: Dict[str, Any] = {}
        out["fileversion"] = FILEVERSION
        if self.session_metadata:
            out["session"] = self.session_metadata
        if self.applied_annotations:
            out["annotations"] = self.applied_annotations
        out["data"] = self.data
        assert self.filename
        json.dump(out, open(self.filename, "w"), indent="\t")

    def find_first_record(self, predicate : Predicate, descr : str) -> DataStoreRecord:
        """
        Return the first record in the DataStore that matches a predicate.
        
        :param predicate: The predicate
        :type predicate: Predicate
        :param descr: Human-readable description of what we are searching for, for error messages only.
        :type descr: str
        :return: The first record matching the predicate
        :rtype: DataStoreRecord
        """
        if not self.data:
            raise DataStoreError("DataStore is empty")
        if type(predicate) == str and not self.debug:
            predicate = compile(predicate, "<string>", "eval")
        for record in self.data:
            nsrecord = dict(record) # shallow copy
            nsrecord["record"] = nsrecord
            if eval(predicate, nsrecord):
                return record
        raise DataStoreError(f"missing {descr}. No record found for predicate: {repr(predicate)}")
        
    def find_all_records(self, predicate : Predicate, descr : str) -> List[DataStoreRecord]:
        """
        Return all records in the DataStore that match a predicate.
        
        :param predicate: The predicate
        :type predicate: Predicate
        :param descr: Human-readable description of what we are searching for, for error messages only.
        :type descr: str
        :return: All records matching the predicate
        :rtype: List[DataStoreRecord]
        """
        if not self.data:
            raise DataStoreError("DataStore is empty")
        if type(predicate) == str and not self.debug:
            predicate = compile(predicate, "<string>", "eval")
        rv : List[DataStoreRecord] = []
        for record in self.data:
            nsrecord = dict(record) # shallow copy
            nsrecord["record"] = nsrecord
            if eval(predicate, nsrecord):
                rv.append(record)
        if not rv:
            raise DataStoreError(f"missing {descr}. No record found for predicate: {predicate}.")
        return rv

    def roles(self) -> List[str]:
        """
        Return the roles that took part in this session.

        Taken from the session metadata (in session order) when present. Otherwise
        derived from the ``role`` fields of the records (sorted), for data that has
        no session metadata, such as bare-list JSON files.

        :raises DataStoreError: if no roles can be found at all (e.g. a raw stats.log).
        """
        roles = self.session_metadata.get("roles")
        if roles:
            return list(roles)
        roles = sorted({r["role"] for r in self.data if r.get("role")})
        if not roles:
            raise DataStoreError(f"{self.filename}: no roles found in session metadata or records")
        return roles

    def versions(self, role: str) -> Dict[str, str]:
        """
        Return what a role's application was built from: app, app_version, vr2gather,
        cwipc_unity, unity and platform. Each is "unknown" if the log doesn't say, as in
        logs from before these were logged (cwi-dis/VR2Gather#373).
        """
        known = self.session_metadata.get("versions", {}).get(role, {})
        return {field: known.get(field, "unknown") for field in VERSION_FIELDS}

    def role_labels(self, source: str = "role") -> Dict[str, str]:
        """
        Return the label to show for each role. Which label is meaningful differs per
        experiment, so the experiment says which it wants.

        :param source: ``"role"``, ``"user_name"`` or ``"participant_number"``. The latter
            comes from ``session_metadata["participant_numbers"]``, which the experiment fills in.
        :raises DataStoreError: if the source is unknown, or doesn't have a label for every role.
        """
        roles = self.roles()
        if source == "role":
            return {role: role for role in roles}
        if source == "user_name":
            values = self.session_metadata.get("user_names", {})
        elif source == "participant_number":
            values = self.session_metadata.get("participant_numbers", {})
        else:
            raise DataStoreError(f"Unknown source for role labels: {source}")
        missing = [role for role in roles if not values.get(role)]
        if missing:
            raise DataStoreError(f"{self.filename}: no {source} for roles {missing}")
        return {role: values[role] for role in roles}

    def role_color(self, role: str) -> str:
        """Return the colour for a role, which is the same in every view of this session."""
        return _ROLE_COLORS[self.roles().index(role) % len(_ROLE_COLORS)]

    def master_role(self) -> str:
        """
        Return the role that created the orchestrator session, from its
        ``OrchestratorController`` record with ``isMaster=1``.

        :raises DataStoreError: if there is no such record, or if more than one role claims to be master.
        """
        masters = {
            r["role"] for r in self.data
            if r.get("component") == "OrchestratorController" and r.get("isMaster") and r.get("role")
        }
        if len(masters) != 1:
            raise DataStoreError(f"{self.filename}: expected exactly one master role, found {sorted(masters)}")
        return masters.pop()

    def orchtime_to_sessiontime(self, orchtime: float) -> float:
        """
        Convert an orchestrator-clock time to session time, for timestamps that
        a record carries in a field of its own. Both are in seconds.

        :raises DataStoreError: if the session start time is not known.
        """
        session_start_time = self.session_metadata.get("session_start_time")
        if not session_start_time:
            raise DataStoreError(f"{self.filename}: no session start time in session metadata")
        return orchtime - session_start_time

    def resolve_file(self, record: DataStoreRecord, field: str) -> str:
        """
        Return the local path of a file that a stats record refers to, such as a
        voice recording. VRTrun collects these into ``<run>/<role>/``, next to
        ``stats.log``. The record has the name the file had on the machine that
        wrote it, possibly a full path with Windows separators, so only the last
        part of it is used.

        :param record: The record, which must have a ``role``.
        :param field: The field of the record that holds the filename.
        :raises DataStoreError: if the record or this DataStore lacks what is needed, or the file doesn't exist.
        """
        if not self.filename or self.filename == "-":
            raise DataStoreError(f"Can't resolve {field}: this DataStore wasn't loaded from a file")
        if field not in record or "role" not in record:
            raise DataStoreError(f"{self.filename}: can't resolve {field}: record lacks {field} or role: {record}")
        run_dir = os.path.dirname(os.path.abspath(self.filename))
        path = os.path.join(run_dir, record["role"], ntpath.basename(record[field]))
        if not os.path.exists(path):
            raise DataStoreError(f"{self.filename}: file referred to by {field} not found: {path}")
        return path

    def scenes(self, role: Optional[str] = None) -> pandas.DataFrame:
        """
        Return the scenes of the session in order, from the ``started`` and ``stopped``
        records of VR2Gather's pilot controllers. There can be several scenes with the
        same name. A scene that was never stopped has no ``t_end``.

        :param role: Whose times to use, because scenes change at slightly different
            times per role. The default is the first role.
        :return: A DataFrame with columns ``name``, ``t_start``, ``t_end``, empty if the log has no scenes.
        """
        if role is None:
            role = self.roles()[0]
        rows: List[Dict[str, Any]] = []
        for record in self.data:
            if record.get("role") != role or "Pilot" not in str(record.get("component")) or "scene" not in record:
                continue
            if "started" in record:
                rows.append({"name": record["scene"], "t_start": record["sessiontime"], "t_end": None})
            elif "stopped" in record and rows and rows[-1]["name"] == record["scene"]:
                rows[-1]["t_end"] = record["sessiontime"]
        return pandas.DataFrame(rows, columns=["name", "t_start", "t_end"]).astype({"t_end": float})

    def describe(self) -> str:
        """Return a short human-readable description of this session and its annotations."""
        from .annotation import describe as _describe
        return _describe(self)

    def sort(self, key: Any) -> None:
        """
        Sort the data in-place.

        :param key: Standard sort() ordering function.
        :type key: Any
        """
        if not self.data:
            raise DataStoreError("DataStore is empty")
        self.data.sort(key=key)
