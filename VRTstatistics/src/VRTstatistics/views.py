from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Dict, Optional, Type
import pandas as pd

from .datastore import DataStore
from .annotation import engine
from .analyze import TileCombiner, SessionTimeFilter

__all__ = [
    "View",
    "LatencyView",
    "LatencyPerTileView",
    "ResourceView",
    "FramerateView",
    "PointcountView",
    "ProgressView",
    "extract_latencies",
    "extract_latencies_per_tile",
    "extract_resources",
    "extract_framerates",
    "extract_pointcounts",
    "extract_progress",
]


@dataclass
class View:
    """Base class for extracted plot data. Carries named DataFrames ready for rendering or CSV export."""
    _registry: ClassVar[Dict[str, Type[View]]] = {}
    _aliases: ClassVar[Dict[str, str]] = {"latencies": "latency-contributions", "latencies-per-tile": "latency-contributions-per-tile"}
    name: ClassVar[str] = ""
    default_filename: ClassVar[str] = ""
    required_annotation: ClassVar[Optional[str]] = None
    description: str

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.name:
            View._registry[cls.name] = cls

    @classmethod
    def find(cls, name: str) -> Optional[Type[View]]:
        """Return the View type registered under `name` or an old name for it, or None."""
        return View._registry.get(View._aliases.get(name, name))

    @classmethod
    def register_extractor(cls, extractor: Callable) -> None:
        """Register a function as the extractor for this View type. Can be called again to override."""
        cls._extractor = extractor

    @classmethod
    def register_renderer(cls, renderer: Callable) -> None:
        """Register a function as the renderer for this View type. Can be called again to override."""
        cls._renderer = renderer

    @classmethod
    def extract(cls, ds: DataStore, **kwargs: Any) -> View:
        """Extract a View from a DataStore using the registered extractor."""
        extractor = getattr(cls, '_extractor', None)
        if extractor is None:
            raise NotImplementedError(f"No extractor registered for {cls.__name__}")
        return extractor(ds, **kwargs)

    def render(self, **kwargs: Any) -> list:
        """Render this View using the registered renderer."""
        renderer = getattr(type(self), '_renderer', None)
        if renderer is None:
            raise NotImplementedError(f"No renderer registered for {type(self).__name__}")
        return renderer(self, **kwargs)


@dataclass
class LatencyView(View):
    """Latency contributions: pipeline stage durations and end-to-end latency."""
    name: ClassVar[str] = "latency-contributions"
    default_filename: ClassVar[str] = "latency-contributions.pdf"
    required_annotation: ClassVar[str] = "media_path"
    area: pd.DataFrame
    end2end: pd.DataFrame
    framedrops: Optional[pd.DataFrame] = None
    tileswitches: Optional[pd.DataFrame] = None


@dataclass
class LatencyPerTileView(View):
    """Per-tile latency breakdown for multi-tile sessions."""
    name: ClassVar[str] = "latency-contributions-per-tile"
    default_filename: ClassVar[str] = "latency-contributions-per-tile.pdf"
    required_annotation: ClassVar[str] = "media_path"
    per_tile: pd.DataFrame
    nTiles: int
    sender: str
    receiver: str


@dataclass
class ResourceView(View):
    """Resource usage: CPU, memory, and bandwidth per role."""
    name: ClassVar[str] = "resources"
    default_filename: ClassVar[str] = "resources.pdf"
    resources: pd.DataFrame


@dataclass
class FramerateView(View):
    """Frame rates and dropped frames per pipeline stage."""
    name: ClassVar[str] = "framerates"
    default_filename: ClassVar[str] = "framerates.pdf"
    required_annotation: ClassVar[str] = "media_path"
    fps: pd.DataFrame
    fps_dropped: pd.DataFrame


@dataclass
class PointcountView(View):
    """Receiver point counts over session time."""
    name: ClassVar[str] = "pointcounts"
    default_filename: ClassVar[str] = "pointcounts.pdf"
    required_annotation: ClassVar[str] = "media_path"
    pointcounts: pd.DataFrame


@dataclass
class ProgressView(View):
    """Point cloud pipeline progress: aggregate packet sequence numbers per stage."""
    name: ClassVar[str] = "progress"
    default_filename: ClassVar[str] = "progress.pdf"
    required_annotation: ClassVar[str] = "media_path"
    progress: pd.DataFrame


def extract_latencies(ds: DataStore, *, show_framedrops: bool = False, show_tileswitches: bool = False) -> LatencyView:
    """Extract latency contribution data from a DataStore."""
    media_path = engine.ensure(ds, "media_path")
    sender = media_path["sender"]
    receiver = media_path["receiver"]

    area_filter = (
        TileCombiner(f"{sender}.pc.grabber.encoder_queue_ms", "encoder queue", "mean", combined=True) +
        TileCombiner(f"{sender}.pc.encoder.encoder_ms", "encoder", "mean", combined=True) +
        TileCombiner(f"{sender}.pc.encoder.transmitter_queue_ms", "transmitter queue", "mean", combined=True) +
        TileCombiner(f"{receiver}.pc.decoder.*.decoder_queue_ms", "decoder queues", "mean", combined=True) +
        TileCombiner(f"{receiver}.pc.decoder.*.decoder_ms", "decoders", "max", combined=True) +
        TileCombiner(f"{receiver}.pc.renderer.*.renderer_queue_ms", "renderer queues", "mean", combined=True)
    )
    area = ds.get_dataframe(
        predicate=f'"{sender}.pc.grabber" in component_role or "{sender}.pc.encoder" in component_role or "{receiver}.pc.decoder" in component_role or "{receiver}.pc.renderer" in component_role or component_role == "{receiver}.voice.renderer"',
        fields=[
            'sessiontime',
            'component_role.=encoder_queue_ms',
            'component_role.=encoder_ms',
            'component_role.=transmitter_queue_ms',
            'component_role.=decoder_queue_ms',
            'component_role.=decoder_ms',
            'component_role.=renderer_queue_ms',
        ]
    )
    area = area_filter(area)

    end2end_filter = (
        TileCombiner(f"{receiver}.synchronizer.latency_ms", "synchronizer latency", "max", combined=True) +
        TileCombiner(f"{receiver}.pc.renderer.*.latency_ms", "renderer latency", "min", combined=True) +
        TileCombiner(f"{receiver}.pc.renderer.*.latency_max_ms", "max renderer latency", "max", combined=True) +
        TileCombiner(f"{receiver}.voice.renderer.latency_ms", "voice latency", "mean", combined=True, optional=True)
    )
    end2end = ds.get_dataframe(
        predicate=f'"{receiver}.pc.renderer" in component_role or "{receiver}.synchronizer" in component_role or component_role == "{receiver}.voice.renderer"',
        fields=[
            'sessiontime',
            'component_role.=latency_ms',
            'component_role.=latency_max_ms',
        ]
    )
    end2end = end2end_filter(end2end)

    framedrops: Optional[pd.DataFrame] = None
    if show_framedrops:
        framedrops_df = ds.get_dataframe(fields=['component', 'sessiontime', 'fps_dropped'])
        framedrops_df = framedrops_df[framedrops_df['fps_dropped'] > 0].copy()
        if framedrops_df.shape[0] > 0:
            framedrops_df.loc[:, 'fps_dropped'] = 1
            framedrops_df.rename(columns={'fps_dropped': 'PC Drop event'}, inplace=True)
            framedrops = framedrops_df

    tileswitches: Optional[pd.DataFrame] = None
    if show_tileswitches:
        tileswitches_df = ds.get_dataframe(
            predicate='"component_role" in record and component_role == "receiver.pc.tileselector" and "tile0" in record',
            fields=['sessiontime', 'component']
        )
        if tileswitches_df.shape[0] > 0:
            tileswitches_df = tileswitches_df.assign(tile_switch=0)
            tileswitches_df.rename(columns={'tile_switch': 'Tile switch event'}, inplace=True)
            tileswitches = tileswitches_df

    return LatencyView(
        description=ds.describe(),
        area=area,
        end2end=end2end,
        framedrops=framedrops,
        tileswitches=tileswitches,
    )


def extract_latencies_per_tile(ds: DataStore) -> LatencyPerTileView:
    """Extract per-tile latency data from a DataStore. Requires nTiles > 1."""
    media_path = engine.ensure(ds, "media_path")
    sender = media_path["sender"]
    receiver = media_path["receiver"]
    nTiles = media_path.get("nTiles", 1)
    assert nTiles > 1
    per_tile = ds.get_dataframe(
        predicate=f'".pc." in component_role or component_role == "{receiver}.voice.renderer" or component_role == "{receiver}.synchronizer"',
        fields=[
            'sessiontime',
            'component_role.=downsample_ms',
            'component_role.=encoder_queue_ms',
            'component_role.=encoder_ms',
            'component_role.=transmitter_queue_ms',
            'component_role.=receive_ms',
            'component_role.=decoder_queue_ms',
            'component_role.=decoder_ms',
            'component_role.=renderer_queue_ms',
            'component_role.=latency_ms',
            'component_role.=latency_max_ms',
        ]
    )
    return LatencyPerTileView(
        description=ds.describe(),
        per_tile=per_tile,
        nTiles=nTiles,
        sender=sender,
        receiver=receiver,
    )


def extract_resources(ds: DataStore) -> ResourceView:
    """Extract resource usage data (CPU, memory, bandwidth) from a DataStore."""
    dataFilter = SessionTimeFilter()
    resources = ds.get_dataframe(
        predicate='component == "ResourceConsumption"',
        fields=['sessiontime', 'role.=cpu', 'role.=mem', 'role.=recv_bandwidth', 'role.=sent_bandwidth']
    )
    resources = dataFilter(resources)
    return ResourceView(description=ds.describe(), resources=resources)


def extract_framerates(ds: DataStore) -> FramerateView:
    """Extract framerate data (fps and dropped frames per pipeline stage) from a DataStore."""
    media_path = engine.ensure(ds, "media_path")
    sender = media_path["sender"]
    receiver = media_path["receiver"]

    fps_filter = (
        TileCombiner(f"{sender}.voice.grabber.fps", "voice capturer", "min", combined=True, optional=True) +
        TileCombiner(f"{sender}.voice.encoder.fps", "voice encoder", "min", combined=True, optional=True) +
        TileCombiner(f"{sender}.voice.writer.fps", "voice transmitter", "min", combined=True, optional=True) +
        TileCombiner(f"{receiver}.voice.reader.fps", "voice receiver", "min", combined=True, optional=True) +
        TileCombiner(f"{receiver}.voice.preparer.fps", "voice preparer", "min", combined=True, optional=True) +
        TileCombiner(f"{sender}.pc.grabber.fps", "capturer", "min", combined=True) +
        TileCombiner(f"{sender}.pc.encoder.fps", "encoders", "min", combined=True) +
        TileCombiner(f"{sender}.pc.writer.*.fps", "transmitters", "min", combined=True) +
        TileCombiner(f"{receiver}.pc.reader.*.fps", "receivers", "min", combined=True) +
        TileCombiner(f"{receiver}.pc.decoder.*.fps", "decoders", "min", combined=True) +
        TileCombiner(f"{receiver}.synchronizer.fps", "synchronizer", "min", combined=True) +
        TileCombiner(f"{receiver}.pc.preparer.*.fps", "preparers", "min", combined=True) +
        TileCombiner(f"{receiver}.pc.renderer.*.fps", "renderers", "min", combined=True)
    )
    fps = ds.get_dataframe(
        predicate='component_role and "fps" in record',
        fields=['sessiontime', 'component_role.=fps']
    )
    fps = fps_filter(fps)

    fps_dropped_filter = (
        TileCombiner(f"{sender}.voice.grabber.fps_dropped", "voice capturer dropped", "min", combined=True, optional=True) +
        TileCombiner(f"{sender}.voice.encoder.fps_dropped", "voice encoder dropped", "min", combined=True, optional=True) +
        TileCombiner(f"{receiver}.voice.reader.fps_dropped", "voice receiver dropped", "min", combined=True, optional=True) +
        TileCombiner(f"{receiver}.voice.preparer.fps_dropped", "voice preparer dropped", "min", combined=True, optional=True) +
        TileCombiner(f"{sender}.pc.grabber.fps_dropped", "capturer dropped", "sum", combined=True) +
        TileCombiner(f"{sender}.pc.encoder.fps_dropped", "encoders dropped", "sum", combined=True) +
        TileCombiner(f"{receiver}.pc.reader.*.fps_dropped", "receivers dropped", "sum", combined=True) +
        TileCombiner(f"{receiver}.pc.decoder.*.fps_dropped", "decoders dropped", "sum", combined=True) +
        TileCombiner(f"{receiver}.pc.preparer.*.fps_dropped", "preparers dropped", "sum", combined=True)
    )
    fps_dropped = ds.get_dataframe(
        predicate='component_role and "fps_dropped" in record',
        fields=['sessiontime', 'component_role.=fps_dropped']
    )
    fps_dropped = fps_dropped_filter(fps_dropped)

    return FramerateView(description=ds.describe(), fps=fps, fps_dropped=fps_dropped)


def extract_pointcounts(ds: DataStore) -> PointcountView:
    """Extract receiver point count data from a DataStore."""
    media_path = engine.ensure(ds, "media_path")
    receiver = media_path["receiver"]
    dataFilter = TileCombiner(f"{receiver}.pc.renderer.*.points_per_cloud", "points per cloud", "sum", combined=True, keep=True)
    pointcounts = ds.get_dataframe(
        predicate=f'"{receiver}.pc.renderer" in component_role',
        fields=['sessiontime', 'component_role.=points_per_cloud']
    )
    pointcounts = dataFilter(pointcounts)
    return PointcountView(description=ds.describe(), pointcounts=pointcounts)


def extract_progress(ds: DataStore) -> ProgressView:
    """Extract point cloud pipeline progress data from a DataStore."""
    media_path = engine.ensure(ds, "media_path")
    sender = media_path["sender"]
    nTiles = media_path.get("nTiles", 1)
    nQualities = media_path.get("nQualities", 1)

    df = ds.get_dataframe(
        predicate='"aggregate_packets" in record and component_role',
        fields=['sessiontime', 'component_role=aggregate_packets']
    )
    grabber_col = f'{sender}.pc.grabber'
    if grabber_col in df.columns:
        df = df.drop(columns=[grabber_col])
    if nTiles > 1:
        df = df.copy()
        for col in df.columns:
            if col == 'sessiontime':
                continue
            if '.all' in col:
                df[col] = df[col] / nTiles
            elif col == f"{sender}.pc.encoder":
                df[col] = df[col] / (nTiles * nQualities)

    return ProgressView(description=ds.describe(), progress=df)


# ── Register extractors ────────────────────────────────────────────────────────

LatencyView.register_extractor(extract_latencies)
LatencyPerTileView.register_extractor(extract_latencies_per_tile)
ResourceView.register_extractor(extract_resources)
FramerateView.register_extractor(extract_framerates)
PointcountView.register_extractor(extract_pointcounts)
ProgressView.register_extractor(extract_progress)
