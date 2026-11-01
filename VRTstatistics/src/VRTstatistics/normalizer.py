from __future__ import annotations
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple, Any

from .datastore import DataStore, DataStoreError, DataStoreRecord, VERSION_FIELDS

__all__ = ["SessionNormalizer"]


# Fields that one component outputs and that hold a reference to another component, so any
# record with one of these may be important for discovering the topology. ("self" is the
# exception: it holds no reference, but says whether a pipeline is outgoing or incoming.)
_STRUCTURE_FIELDS = ("self", "reader", "writer", "renderer", "decoder", "pusher", "pull_thread", "pipeline")


class _RoleInfo:
    role: str
    session_id: str
    session_start_time: float
    desync: float
    desync_uncertainty: float
    user_name: Optional[str]
    # component_name → component_role string (e.g. "sender.pc.grabber")
    component_map: Dict[str, str]
    # topology metadata (protocol, nTiles, etc.)
    topology: Dict[str, Any]
    # app and package versions, empty for logs from before they were logged
    versions: Dict[str, str]

    def __init__(self, role: str) -> None:
        self.role = role
        self.versions = {}
        self.component_map = {}
        self.topology = {}


class SessionNormalizer:
    """
    Phase 1 of the annotation pipeline.

    Validates that all per-role DataStores belong to the same session,
    aligns clocks, converts orchtime→sessiontime, stamps role, drops
    pre-session records, merges, and sorts.

    Also discovers pipeline topology from the raw per-role DataStores
    (while they still contain pre-orchtime structure records) and stores
    the component→component_role mapping in session_metadata so that
    ComponentRoleAnnotation can apply it later without needing the raw data.
    """
    verbose: bool = True

    def __init__(self, datastores: List[Tuple[str, DataStore]], output: DataStore) -> None:
        self._inputs = datastores
        self._output = output

    def normalize(self) -> bool:
        if not self._inputs:
            raise RuntimeError("No datastores to normalize")

        roles: List[_RoleInfo] = []
        for role, ds in self._inputs:
            info = self._collect_role(role, ds)
            roles.append(info)

        session_ids = {r.session_id for r in roles}
        if len(session_ids) > 1:
            raise DataStoreError(f"Session ID mismatch across roles: {session_ids}")

        start_times = [r.session_start_time for r in roles]
        if max(start_times) - min(start_times) > 1:
            print(
                f"Warning: session start times spread {max(start_times) - min(start_times):.1f}s across roles",
                file=sys.stderr,
            )

        if len(roles) > 2:
            print(
                f"Warning: {len(roles)} roles: a component_role doesn't say whose media it carries, so incoming media of different senders is merged",
                file=sys.stderr,
            )

        builds = {
            tuple(sorted((k, v) for k, v in r.versions.items() if k != "platform")) for r in roles
        }
        if len(builds) > 1:
            print(f"Warning: roles ran different builds: { {r.role: r.versions for r in roles} }", file=sys.stderr)

        for r in roles:
            if abs(r.desync) > 0.030:
                print(
                    f"Warning: {r.role} clock {r.desync:.3f}s (+/- {r.desync_uncertainty / 2:.3f}s) behind orchestrator",
                    file=sys.stderr,
                )

        session_start_time = min(start_times)

        all_data: List[DataStoreRecord] = []
        for info in roles:
            _, ds = next((role_ds for role_ds in self._inputs if role_ds[0] == info.role), (None, None))
            assert ds is not None
            for record in ds.data:
                if "orchtime" not in record:
                    continue
                new_record = dict(record)
                new_record["sessiontime"] = record["orchtime"] - session_start_time
                new_record["role"] = info.role
                all_data.append(new_record)

        self._output.load_data(all_data)
        self._output.sort(key=lambda r: r["sessiontime"])

        self._output.session_metadata = {
            "session_id": roles[0].session_id,
            "session_start_time": session_start_time,
            "roles": [r.role for r in roles],
            "user_names": {r.role: r.user_name for r in roles},
            "desyncs": {r.role: r.desync for r in roles},
            "desync_uncertainties": {r.role: r.desync_uncertainty for r in roles},
            "component_map": {r.role: r.component_map for r in roles},
            "role_topology": {r.role: r.topology for r in roles},
        }
        versions = {r.role: r.versions for r in roles if r.versions}
        if versions:
            self._output.session_metadata["versions"] = versions

        if self.verbose:
            for r in roles:
                print(f"{r.role}: session_id={r.session_id}, user={r.user_name}, desync={r.desync:.3f}s")
            print(f"Session start: {session_start_time}, roles: {[r.role for r in roles]}")

        return True

    def _collect_role(self, role: str, ds: DataStore) -> _RoleInfo:
        info = _RoleInfo(role)

        r = ds.find_first_record(
            '"starting" in record and component == "OrchestratorController"',
            f"{role} session start",
        )
        info.session_id = r["sessionId"]

        r = ds.find_first_record('component == "SessionPlayerManager"', f"{role} session start time")
        info.session_start_time = r["orchtime"]

        r = ds.find_first_record(
            '"localtime_behind_ms" in record and component == "OrchestratorController"',
            f"{role} session time synchronization",
        )
        info.desync = r["localtime_behind_ms"] / 1000.0
        info.desync_uncertainty = r["uncertainty_interval_ms"] / 1000.0

        try:
            r = ds.find_first_record(
                'component == "SessionPlayerManager" and "userName" in record and self == "True"',
                f"{role} user name",
            )
            info.user_name = r["userName"]
        except DataStoreError:
            info.user_name = None

        # Logged at startup, before time sync, so it is only in these raw records.
        for record in ds.data:
            if record.get("component") == "VRTConfig" and "app_version" in record:
                info.versions = {field: record[field] for field in VERSION_FIELDS if field in record}
                break

        self._discover_topology(role, ds, info)
        return info

    def _discover_topology(self, role: str, ds: DataStore, info: _RoleInfo) -> None:
        """
        Discover pipeline topology for one role and populate info.component_map and info.topology.

        Every scene creates its pipelines and components anew, with new instance numbers,
        so all instances are added: they map to the same component_role, which is what
        hides the instance numbers from everything above this.
        """
        # Only a few dozen of a log's records describe structure, so look at those only.
        by_component: Dict[str, List[DataStoreRecord]] = defaultdict(list)
        protocol = None
        for record in ds.data:
            if any(field in record for field in _STRUCTURE_FIELDS):
                by_component[str(record.get("component", ""))].append(record)
            if protocol is None and "proto" in record:
                protocol = record["proto"]
        info.topology["protocol"] = protocol

        def pipelines(kind: str) -> List[str]:
            return [component for component in by_component if kind in component]

        pc_setups: List[Tuple[int, int, bool]] = []
        for pipeline in pipelines("PointCloudPipelineSelf"):
            setup = self._discover_self_pc(role, pipeline, by_component, protocol, info)
            if setup:
                pc_setups.append(setup)
        for pipeline in pipelines("VoicePipelineSelf"):
            for r in by_component[pipeline]:
                if "writer" in r:
                    info.component_map[r["reader"]] = f"{role}.voice.grabber"
                    info.component_map[r["encoder"]] = f"{role}.voice.encoder"
                    info.component_map[r["writer"]] = f"{role}.voice.writer"
                    break
        for pipeline in pipelines("PointCloudPipelineOther"):
            self._discover_other_pc(role, pipeline, by_component, info)
        for pipeline in pipelines("VoicePipelineOther"):
            for r in by_component[pipeline]:
                if "reader" in r:
                    info.component_map[pipeline] = f"{role}.voice.renderer"
                    if "preparer" in r:
                        info.component_map[r["preparer"]] = f"{role}.voice.preparer"
                    break

        if len(set(pc_setups)) > 1:
            print(f"Warning: {role}: point cloud setup differs between scenes, using the first: {pc_setups}", file=sys.stderr)
        info.topology["nTiles"], info.topology["nQualities"], info.topology["compressed"] = (
            pc_setups[0] if pc_setups else (1, 1, False)
        )

    def _discover_self_pc(
        self, role: str, pipeline: str, by_component: Dict[str, List[DataStoreRecord]], protocol: Optional[str], info: _RoleInfo
    ) -> Optional[Tuple[int, int, bool]]:
        """Add one outgoing point cloud pipeline. Returns its nTiles, nQualities and compressed, if it is one."""
        records = by_component[pipeline]
        umbrella = next((r for r in records if "writer" in r), None)
        if umbrella is None or not any(r.get("self") == 1 for r in records):
            return None
        grabber = umbrella["reader"]
        encoder = umbrella["encoder"]
        writer_umbrella = umbrella["writer"]
        info.component_map[grabber] = f"{role}.pc.grabber"
        info.component_map[encoder] = f"{role}.pc.encoder"

        writers = [r for r in by_component[writer_umbrella] if "pusher" in r]
        if protocol in ("socketio", "tcpreflector"):
            info.component_map[writer_umbrella] = f"{role}.pc.writer.all"
        elif writers:
            for wr in writers:
                tile = wr["tile"] - 1 if "tile" in wr else wr.get("stream", 0)
                info.component_map[wr["pusher"]] = f"{role}.pc.writer.{tile}"
        else:
            info.component_map[writer_umbrella] = f"{role}.pc.writer.0"

        setup = (umbrella.get("ntile", 1), umbrella.get("nquality", 1), "PCEncoder" in encoder)
        if self.verbose:
            print(f"{role}: self pc pipeline found: grabber={grabber}, encoder={encoder}, nTiles={setup[0]}")
        return setup

    def _discover_other_pc(
        self, role: str, pipeline: str, by_component: Dict[str, List[DataStoreRecord]], info: _RoleInfo
    ) -> None:
        """Add one incoming point cloud pipeline."""
        records = by_component[pipeline]
        umbrella = next((r for r in records if "reader" in r), None)
        if umbrella is None or not any(r.get("self") == 0 for r in records):
            return
        reader_umbrella = umbrella["reader"]
        synchronizer = umbrella.get("synchronizer")
        if synchronizer:
            info.component_map[synchronizer] = f"{role}.synchronizer"

        for component, component_records in by_component.items():
            if "TileSelector" in component and any(r.get("pipeline") == pipeline for r in component_records):
                info.component_map[component] = f"{role}.pc.tileselector"
                break

        readers = [r for r in by_component[reader_umbrella] if "pull_thread" in r]
        if "SocketIOReader" in reader_umbrella or "TCPReflectorReader" in reader_umbrella:
            info.component_map[reader_umbrella] = f"{role}.pc.reader.all"
        elif readers:
            for rr in readers:
                tile = rr["tile"] - 1 if "Sub" in reader_umbrella else rr["tile"]
                info.component_map[rr["pull_thread"]] = f"{role}.pc.reader.{tile}"
        else:
            info.component_map[reader_umbrella] = f"{role}.pc.reader.0"

        for rr in records:
            if "decoder" in rr:
                info.component_map[rr["decoder"]] = f"{role}.pc.decoder.{rr['tile']}"
            if "renderer" in rr:
                info.component_map[rr["preparer"]] = f"{role}.pc.preparer.{rr['tile']}"
                info.component_map[rr["renderer"]] = f"{role}.pc.renderer.{rr['tile']}"
        if self.verbose:
            print(f"{role}: other pc pipeline found: synchronizer={synchronizer}")
