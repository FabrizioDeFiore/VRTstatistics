from __future__ import annotations
import datetime
from typing import Dict, List, Optional, Type, Any

from .datastore import DataStore, DataStoreError

__all__ = ["AnnotationStep", "AnnotationEngine", "engine"]


class AnnotationStep:
    """
    Base class for a single declarative, idempotent annotation step.

    Subclasses set class-level `name`, `dependencies`, `description`, and
    `params`, and implement `apply()`.

    Set `multi_instance` if one DataStore can meaningfully hold several results
    of this step with different params. That is only true for a step that
    records metadata: a step that stamps fields on records would overwrite the
    fields of the earlier instance.

    Register subclasses with the module-level `engine` singleton:
        from VRTstatistics.annotation import engine
        engine.register(MyAnnotationStep)
    """
    name: str = ""
    dependencies: List[str] = []
    description: str = ""
    params: Dict[str, str] = {}  # param_name → description
    multi_instance: bool = False

    def apply(self, ds: DataStore, **params) -> Dict[str, Any]:
        """
        Apply this annotation to the DataStore.

        Called only when no instance with these params is in ds.applied_annotations.
        Returns a metadata dict, which the engine stores together with the params.
        """
        raise NotImplementedError


class AnnotationEngine:
    """
    Declarative, dependency-driven, idempotent annotation runner.

    Maintains a registry of AnnotationStep classes. Call ensure() to apply a
    named annotation (and all its dependencies) to a DataStore, skipping any
    that are already applied.

    ds.applied_annotations holds every instance under its full name,
    "name(key=value,...)" with all parameters of the step, or just "name" for a
    step without parameters. Files from before instances existed can also have
    "name" for a step with parameters. Read instances through ensure(), not
    through these keys.
    """

    def __init__(self) -> None:
        self._registry: Dict[str, Type[AnnotationStep]] = {}

    def register(self, step_class: Type[AnnotationStep]) -> None:
        """Register an AnnotationStep class by its name."""
        if not step_class.name:
            raise ValueError(f"AnnotationStep class {step_class} has no name")
        self._registry[step_class.name] = step_class

    def list_steps(self) -> str:
        """Return a human-readable listing of all registered annotation steps."""
        if not self._registry:
            return "(no annotation steps registered)"
        lines = []
        for name, cls in sorted(self._registry.items()):
            lines.append(f"{name}")
            if cls.description:
                lines.append(f"  {cls.description}")
            if cls.dependencies:
                lines.append(f"  depends on: {', '.join(cls.dependencies)}")
            if cls.params:
                lines.append("  parameters:")
                for pname, pdesc in cls.params.items():
                    lines.append(f"    {pname}: {pdesc}")
        return "\n".join(lines)

    def ensure(self, ds: DataStore, name: str, **params) -> Dict[str, Any]:
        """
        Ensure annotation `name` has been applied to `ds`, and return its metadata.

        Params select the instance: without params any instance matches, with
        params only those applied with these values. If exactly one matches it is
        returned. If none matches the step is applied, with its own defaults for
        params not given, after ensuring its dependencies (with no params).

        :raises DataStoreError: if several instances match (ask with more params),
            if the step is unknown or doesn't support another instance, or if an
            instance was applied by a version that didn't record these params.
        """
        instances = self.instances(ds, name)
        matches = [key for key, metadata in instances.items() if self._matches(name, metadata, params)]
        if len(matches) > 1:
            raise DataStoreError(f"Several instances of annotation '{name}' match {params}: {matches}")
        if matches:
            return instances[matches[0]]
        step_class = self._registry.get(name)
        if step_class is None:
            raise DataStoreError(f"Unknown annotation step '{name}'. Registered: {list(self._registry)}")
        if instances and not step_class.multi_instance:
            raise DataStoreError(
                f"Annotation '{name}' was already applied with other parameters, "
                f"and doesn't support several instances (requested: {params})"
            )
        for dep in step_class.dependencies:
            self.ensure(ds, dep)
        step = step_class()
        result = step.apply(ds, **params)
        metadata = {**(result if result is not None else {}), **params}
        ds.applied_annotations[self._full_name(step_class, metadata)] = metadata
        return metadata

    @staticmethod
    def instances(ds: DataStore, name: str) -> Dict[str, Dict[str, Any]]:
        """Return the applied instances of annotation `name`, by full name."""
        return {
            key: metadata for key, metadata in ds.applied_annotations.items()
            if key == name or key.startswith(f"{name}(")
        }

    @staticmethod
    def _full_name(step_class: Type[AnnotationStep], metadata: Dict[str, Any]) -> str:
        """Same spelling as the annotation argument of the command line tools."""
        args = [f"{k}={metadata[k]}" for k in sorted(step_class.params) if k in metadata]
        return f"{step_class.name}({','.join(args)})" if args else step_class.name

    @staticmethod
    def _matches(name: str, metadata: Dict[str, Any], params: Dict[str, Any]) -> bool:
        missing = [k for k in params if k not in metadata]
        if missing:
            raise DataStoreError(
                f"Annotation '{name}' was applied without recording {missing}, "
                f"so can't tell whether it matches {params}"
            )
        return all(str(metadata[k]) == str(v) for k, v in params.items())


engine = AnnotationEngine()


class ComponentRoleAnnotation(AnnotationStep):
    """
    Stamps component_role on every record using the component_map discovered
    by SessionNormalizer and stored in ds.session_metadata["component_map"].
    """
    name = "component_role"
    dependencies: List[str] = []
    description = "Assign component_role to every record (e.g. sender.pc.grabber) based on discovered pipeline topology."
    params: Dict[str, str] = {}

    def apply(self, ds: DataStore, **params) -> Dict[str, Any]:
        component_map: Dict[str, Dict[str, str]] = ds.session_metadata.get("component_map", {})
        if not component_map:
            raise DataStoreError("component_map not found in session_metadata — was SessionNormalizer run?")
        for record in ds.data:
            comp = record.get("component", "")
            role = record.get("role", "")
            record["component_role"] = component_map.get(role, {}).get(comp, "")
        roles = ds.session_metadata.get("roles", [])
        return {"roles": roles}


class LatencyAnnotation(AnnotationStep):
    """
    Records experiment metadata (sender/receiver roles, protocol, nTiles, etc.)
    into ds.applied_annotations["latency"].
    """
    name = "latency"
    dependencies = ["component_role"]
    description = "Record latency-experiment metadata: sender/receiver roles, protocol, nTiles, nQualities."
    params = {
        "sender": "Role name of the point-cloud sender (default: first role in session)",
        "receiver": "Role name of the point-cloud receiver (default: second role in session)",
    }
    multi_instance = True

    def apply(self, ds: DataStore, **params) -> Dict[str, Any]:
        roles = ds.session_metadata.get("roles", [])
        sender = params.get("sender", roles[0] if roles else "sender")
        receiver = params.get("receiver", roles[1] if len(roles) > 1 else "receiver")

        role_topology = ds.session_metadata.get("role_topology", {})
        sender_topo = role_topology.get(sender, {})

        user_names = ds.session_metadata.get("user_names", {})
        desyncs = ds.session_metadata.get("desyncs", {})
        uncertainties = ds.session_metadata.get("desync_uncertainties", {})

        sender_desync = desyncs.get(sender, 0)
        receiver_desync = desyncs.get(receiver, 0)
        combined_desync = sender_desync - receiver_desync
        combined_uncertainty = max(uncertainties.get(sender, 0), uncertainties.get(receiver, 0)) / 2

        return {
            "sender": sender,
            "receiver": receiver,
            "protocol": sender_topo.get("protocol"),
            "nTiles": sender_topo.get("nTiles", 1),
            "nQualities": sender_topo.get("nQualities", 1),
            "compressed": sender_topo.get("compressed", False),
            "sender_user": user_names.get(sender),
            "receiver_user": user_names.get(receiver),
            "desync": combined_desync,
            "desync_uncertainty": combined_uncertainty,
        }


engine.register(ComponentRoleAnnotation)
engine.register(LatencyAnnotation)


def describe(ds: DataStore) -> str:
    """Return a short human-readable description of the session and annotations."""
    sm = ds.session_metadata

    start = sm.get("session_start_time", 0)
    dt = datetime.datetime.fromtimestamp(start).strftime("%d-%b-%Y %H:%M") if start else "unknown"

    lines = [dt]

    # Several instances (e.g. both directions of a two-way session) have no single
    # sender and receiver to describe, so those fall through to listing the roles.
    latency_instances = list(engine.instances(ds, "latency").values())
    if len(latency_instances) == 1:
        lat = latency_instances[0]
        sender = lat.get("sender", "?")
        receiver = lat.get("receiver", "?")
        lines.append(f"{sender} → {receiver}")
        proto = lat.get("protocol")
        if proto:
            suffix = ""
            if lat.get("compressed"):
                suffix += ", compressed"
                nq = lat.get("nQualities", 1)
                if nq and nq > 1:
                    suffix += f" ({nq} levels)"
            nt = lat.get("nTiles", 1)
            if nt and nt > 1:
                suffix += f", {nt} tiles"
            lines.append(proto + suffix)
        desync = lat.get("desync", 0)
        uncertainty = lat.get("desync_uncertainty", 0)
        if uncertainty:
            lines.append(f"desync: {int(desync * 1000)} ms ± {int(uncertainty * 1000)}")
        else:
            lines.append(f"desync: {int(desync * 1000)} ms")
    else:
        roles = sm.get("roles", [])
        user_names = sm.get("user_names", {})
        for role in roles:
            user = user_names.get(role, role)
            lines.append(f"{role}: {user}")

    return "\n".join(lines)
