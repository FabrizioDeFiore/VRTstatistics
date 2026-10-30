"""
Common checks for a per-experiment checker script, such as TrolleyExperiment's
vrts_session_check.py.

Each check takes a DataStore and its parameters, and returns a list of messages
describing what it found. The checker decides which checks to run, with which
values, and whether a message is an error or a warning.
"""
from typing import Dict, List, Sequence

from .datastore import DataStore, DataStoreError, DataStoreRecord

__all__ = [
    "check_clock_desync",
    "check_start_time_spread",
    "check_session_end",
    "check_referenced_files",
    "check_required_records",
    "check_scenes",
]


def _component_type(record: DataStoreRecord) -> str:
    """Return the name of a record's component without its instance number."""
    return str(record.get("component", "")).split("#")[0]


def check_clock_desync(ds: DataStore, max_ms: float = 30) -> List[str]:
    """Report roles whose clock differs from the orchestrator's by more than max_ms."""
    desyncs = ds.session_metadata.get("desyncs")
    if not desyncs:
        return ["no clock desync information in session metadata"]
    return [
        f"{role}: clock differs {desync * 1000:.0f} ms from orchestrator (allowed: {max_ms:g} ms)"
        for role, desync in desyncs.items()
        if abs(desync) * 1000 > max_ms
    ]


def check_start_time_spread(ds: DataStore, max_s: float = 1) -> List[str]:
    """Report if the roles joined the session more than max_s apart."""
    starts: Dict[str, float] = {}
    for record in ds.data:
        if record.get("component") == "SessionPlayerManager" and record["role"] not in starts:
            starts[record["role"]] = record["sessiontime"]
    missing = [role for role in ds.roles() if role not in starts]
    if missing:
        return [f"{role}: no session start record" for role in missing]
    spread = max(starts.values()) - min(starts.values())
    if spread > max_s:
        return [f"roles started the session {spread:.1f} s apart (allowed: {max_s:g} s): {starts}"]
    return []


def check_session_end(ds: DataStore) -> List[str]:
    """Report roles whose log doesn't show the end of the session."""
    ended = {
        record["role"] for record in ds.data
        if record.get("component") == "OrchestratorController" and "stopping" in record
    }
    return [f"{role}: no end of session in log" for role in ds.roles() if role not in ended]


def check_referenced_files(ds: DataStore, fields: List[str]) -> List[str]:
    """
    Report files that stats records refer to but that weren't collected.

    :param fields: What to look at, as "Component.field", e.g. "RecordUserVoice.filename".
    """
    wanted = {tuple(field.split(".", 1)) for field in fields}
    problems: Dict[str, None] = {}  # A file is usually mentioned by several records
    for record in ds.data:
        for component, field in wanted:
            if _component_type(record) == component and field in record:
                try:
                    ds.resolve_file(record, field)
                except DataStoreError as e:
                    problems[str(e)] = None
    return list(problems)


def check_required_records(ds: DataStore, required: Dict[str, int]) -> List[str]:
    """
    Report roles with fewer records of a component than required. This catches what
    check_referenced_files can't: a file that was never started has no record.

    :param required: Minimum number of records per role, by component, e.g. {"Questionnaire": 3}.
    """
    problems = []
    for role in ds.roles():
        for component, minimum in required.items():
            count = sum(1 for r in ds.data if r["role"] == role and _component_type(r) == component)
            if count < minimum:
                problems.append(f"{role}: {count} {component} records (required: at least {minimum})")
    return problems


def check_scenes(ds: DataStore, required: Sequence[str] = ()) -> List[str]:
    """
    Report scenes that are missing for a role, that weren't finished, or that roles disagree on.

    :param required: Names of the scenes that every role must have.
    """
    problems = []
    names = {}
    for role in ds.roles():
        scenes = ds.scenes(role)
        names[role] = list(scenes["name"])
        problems += [f"{role}: no scene {name}" for name in required if name not in names[role]]
        problems += [f"{role}: scene {name} wasn't finished" for name in scenes[scenes["t_end"].isna()]["name"]]
    if len({tuple(n) for n in names.values()}) > 1:
        problems.append(f"roles went through different scenes: {names}")
    return problems
