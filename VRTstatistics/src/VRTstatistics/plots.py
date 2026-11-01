import os
import warnings
from dataclasses import dataclass, field
from typing import Callable, Tuple, Optional, List, Dict, Any, cast, Literal
from matplotlib.axes import Axes
from matplotlib.figure import Figure
import matplotlib.colors as mcolors
import matplotlib.patches as mpatches
import pandas as pd
import matplotlib.pyplot as pyplot
from matplotlib.backends.backend_pdf import PdfPages

from .datastore import DataStore, DataStoreError, Predicate
from .analyze import DataFrameFilter, TileCombiner, SessionTimeFilter
from .annotation import engine
from .views import (
    LatencyView, LatencyPerTileView, ResourceView, FramerateView, PointcountView, ProgressView, VoiceView, GazeView,
    EndToEndTemporalQualityView, CONTINUITY_MEASURES, LATENCY_STATISTICS,
    extract_latencies, extract_latencies_per_tile, extract_resources,
    extract_framerates, extract_pointcounts, extract_progress,
    extract_voice, extract_gaze,
)


@dataclass
class PlotStyle:
    """Visual styling for render_* functions. Construct once, reuse across plots in an experiment."""
    figsize: Optional[Tuple[int, int]] = None
    ylim_top: float = 0                              # 0 = auto-scale
    plot_kwargs: Dict[str, Any] = field(default_factory=dict)    # → pandas .plot()
    label_kwargs: Dict[str, Any] = field(default_factory=dict)   # → xlabel()/ylabel()
    tick_kwargs: Dict[str, Any] = field(default_factory=dict)    # → xticks()/yticks()
    legend_kwargs: Dict[str, Any] = field(default_factory=dict)  # → ax.legend()
    legend_row_major: bool = False


__all__ = [
    "PlotStyle",
    "plot_simple",
    "plot_pointcounts", "plot_framerates_and_dropped", "plot_framerates_dropped",
    "plot_framerates", "plot_progress", "plot_resources",
    "plot_resource_cpu", "plot_resource_mem", "plot_resource_bandwidth",
    "plot_latencies", "plot_latencies_per_tile",
    "render_latencies", "render_latencies_per_tile",
    "render_resources", "render_resource_cpu", "render_resource_mem", "render_resource_bandwidth",
    "render_framerates", "render_framerates_dropped", "render_framerates_and_dropped",
    "render_pointcounts", "render_progress",
    "publish_plots", "extract_legend",
]


_SCENE_SHADES = ['#f0f0f0', '#ffffff']


def apply_scene_shading(ax: Axes, scenes: Optional[pd.DataFrame], labels: Optional[Callable[[str], str]] = None) -> None:
    """
    Add alternating background shading and scene names to an axes whose x axis is session time.

    :param scenes: A table from DataStore.scenes(). Nothing happens if it is None or empty.
    :param labels: Optionally, a function from scene name to the label to show.
    """
    if scenes is None:
        return
    shade_idx = 0
    for _, row in scenes.iterrows():
        if pd.isna(row['t_end']):
            continue
        ax.axvspan(row['t_start'], row['t_end'], color=_SCENE_SHADES[shade_idx % 2], zorder=0)
        shade_idx += 1
        ax.text(
            (row['t_start'] + row['t_end']) / 2, 0.98, labels(row['name']) if labels else row['name'],
            ha='center', va='top', fontsize=7, color='#888888',
            transform=ax.get_xaxis_transform(),
        )


def plot_simple(datastore: DataStore, *, predicate: Optional[Predicate]=None, title: Optional[str]=None, noshow: bool=False, x: str="sessiontime", fields: List[str]=[], datafilter: Optional[DataFrameFilter]=None, plotargs: Dict[str, Any]={}, show_desc: bool=True) -> Axes:
    """
    Plot data (optionally after converting to pandas.DataFrame).
    output is optional output file (default: show in a window)
    x is name of x-axis field
    fields is list of fields to plot (default: all, except x)
    """
    fields_to_retrieve: Optional[List[str]] = list(fields)
    if fields_to_retrieve and x and x not in fields_to_retrieve:
        fields_to_retrieve.append(x)
    if not fields_to_retrieve:
        fields_to_retrieve = None
    dataframe = datastore.get_dataframe(predicate=predicate, fields=fields_to_retrieve)
    if datafilter:
        dataframe = datafilter(dataframe)
    descr = datastore.describe() if show_desc else None
    return _plot_dataframe(dataframe, title=title, noshow=noshow, x=x, fields=None, descr=descr, plotargs=plotargs)


def _plot_dataframe(dataframe: pd.DataFrame, *, title: Optional[str]=None, noshow: bool=False, x: Any=None, fields: Optional[List[str]]=None, descr: Optional[str]=None, plotargs: Dict[str, Any]={}, interpolate: str='linear') -> Axes:
    if dataframe.empty:
        raise DataStoreError("dataframe is empty, nothing to plot")
    df_tmp = dataframe.interpolate(method=interpolate)  # type: ignore
    if fields:
        plot: Axes = cast(Axes, df_tmp.plot(x=x, y=fields, **plotargs))
    else:
        plot: Axes = cast(Axes, df_tmp.plot(x=x, **plotargs))
    assert plot
    if descr:
        props = dict(boxstyle='round', facecolor='wheat', alpha=0.5)
        plot.text(0.98, 0.98, descr, transform=plot.transAxes, verticalalignment='top', horizontalalignment='right', fontsize='x-small', bbox=props)  # type: ignore
    if title:
        pyplot.title(title)  # type: ignore
    plot.legend(loc='upper left', fontsize='small')  # type: ignore
    if not noshow:
        pyplot.show()  # type: ignore
    return plot


def _handle_color(handle) -> Optional[tuple]:
    """Extract normalised RGBA colour from a legend handle for comparison."""
    try:
        if hasattr(handle, 'get_facecolor'):
            color = handle.get_facecolor()
            if hasattr(color, 'ndim') and color.ndim == 2:
                color = color[0]
            return tuple(round(float(c), 3) for c in color[:4])
        if hasattr(handle, 'get_color'):
            return mcolors.to_rgba(handle.get_color())
    except Exception:
        pass
    return None


def publish_plots(
    axes: List[Axes],
    *,
    dirname: Optional[str] = None,
    file_name: Optional[str] = None,
    format: str = "pdf",
    dpi: float | Literal["figure"] = "figure",
    showplot: bool = True,
    saveplot: bool = False,
) -> None:
    """Save and/or show a list of axes (and their figures).

    Recovers figures from axes preserving order. PDF saves all figures as pages.
    Other formats require a single figure; raises DataStoreError if more than one.
    """
    if saveplot:
        if not dirname:
            raise DataStoreError("publish_plots: saveplot=True requires dirname")
        if not file_name:
            raise DataStoreError("publish_plots: saveplot=True requires file_name")
        path = os.path.join(dirname, file_name)
        figs = list(dict.fromkeys(ax.get_figure() for ax in axes))
        if format == "pdf":
            pp = PdfPages(path)
            for fig in figs:
                fig.savefig(pp, bbox_inches='tight', format="pdf", pad_inches=0.05)  # type: ignore
            pp.close()
        else:
            if len(figs) > 1:
                raise DataStoreError(
                    f"publish_plots: cannot save {len(figs)} figures to a single {format} file; "
                    "use format='pdf' or call publish_plots once per figure"
                )
            figs[0].savefig(path, bbox_inches='tight', dpi=dpi, format=format, pad_inches=0.01)  # type: ignore
    if showplot:
        pyplot.show()  # type: ignore


def extract_legend(axes: List[Axes], **legend_kwargs) -> List[Axes]:
    """Remove per-axes legends and create a shared standalone legend axes.

    Collects all legend handles/labels from the given axes; warns if label
    sets are asymmetric across axes (subset is fine, superset is taken);
    raises ValueError if the same label maps to different colours on different
    axes (that would produce a silently wrong shared legend).

    Returns the original axes list with the new legend-only axes appended.
    """
    combined: Dict[str, Any] = {}  # label -> first handle seen

    for ax in axes:
        legend = ax.get_legend()
        if legend is None:
            continue
        for handle, text in zip(legend.legend_handles, legend.get_texts()):
            label = text.get_text()
            if label not in combined:
                combined[label] = handle
            else:
                existing_color = _handle_color(combined[label])
                new_color = _handle_color(handle)
                if existing_color is not None and new_color is not None and existing_color != new_color:
                    raise ValueError(
                        f"extract_legend: label '{label}' has inconsistent colours across axes: "
                        f"{existing_color} vs {new_color}"
                    )

    if combined:
        all_labels = set(combined.keys())
        warned = False
        for ax in axes:
            legend = ax.get_legend()
            ax_labels = {t.get_text() for t in legend.get_texts()} if legend is not None else set()
            if ax_labels and all_labels - ax_labels and not warned:
                warnings.warn(f"extract_legend: some axes are missing labels {all_labels - ax_labels}; using superset")
                warned = True

    for ax in axes:
        legend = ax.get_legend()
        if legend is not None:
            legend.remove()

    fig_legend = pyplot.figure()
    ax_legend: Axes = fig_legend.add_subplot(111)
    ax_legend.axis('off')
    ax_legend.legend(list(combined.values()), list(combined.keys()), loc='center', **legend_kwargs)

    return list(axes) + [ax_legend]


def _apply_style(ax: Axes, style: PlotStyle, *, reverse_legend: bool = False, capitalize_labels: bool = False) -> None:
    """Apply PlotStyle to an axes after rendering: ylim, axis labels, ticks, legend."""
    if style.ylim_top:
        ax.set_ylim(top=style.ylim_top)
    if style.label_kwargs:
        pyplot.xlabel(ax.get_xlabel(), **style.label_kwargs)
        pyplot.ylabel(ax.get_ylabel(), **style.label_kwargs)
    if style.tick_kwargs:
        pyplot.xticks(**style.tick_kwargs)
        pyplot.yticks(**style.tick_kwargs)
    handles, labels = ax.get_legend_handles_labels()
    if capitalize_labels:
        labels = [label.capitalize() for label in labels]
    ncols = style.legend_kwargs.get('ncols', style.legend_kwargs.get('ncol', 1))
    if style.legend_row_major and ncols > 1:
        nrows = -(-len(labels) // ncols)
        reordered_handles: List[Any] = []
        reordered_labels: List[str] = []
        for i in range(ncols):
            for j in range(nrows):
                idx = i + j * ncols
                if idx < len(labels):
                    reordered_handles.append(handles[idx])
                    reordered_labels.append(labels[idx])
        handles, labels = reordered_handles, reordered_labels
    if reverse_legend:
        handles, labels = handles[::-1], labels[::-1]
    legend_kw: Dict[str, Any] = {'loc': 'upper left', 'fontsize': 'small'} | style.legend_kwargs
    ax.legend(handles, labels, **legend_kw)


# ── render_* functions: View → List[Axes] ─────────────────────────────────────
# Pure plot creation: always renders everything (legend, description box, etc.).
# No output control — use publish_plots() for saving/showing.

def render_resource_cpu(view: ResourceView, *, title: str="CPU usage", style: PlotStyle=PlotStyle()) -> List[Axes]:
    """Render CPU usage from a ResourceView."""
    cpu_cols = [c for c in view.resources.columns if c != 'sessiontime' and c.endswith('.cpu')]
    actual_plotargs = ({} if style.figsize is None else {"figsize": style.figsize}) | style.plot_kwargs
    ax = _plot_dataframe(view.resources, noshow=True, title=title, x="sessiontime", fields=cpu_cols, descr=view.description, plotargs=actual_plotargs)
    _, top = ax.get_ylim()
    ax.set_ylim(0, top * 1.5)
    _apply_style(ax, style)
    apply_scene_shading(ax, view.scenes)
    return [ax]


def render_resource_mem(view: ResourceView, *, title: str="Memory usage", style: PlotStyle=PlotStyle()) -> List[Axes]:
    """Render memory usage from a ResourceView."""
    mem_cols = [c for c in view.resources.columns if c != 'sessiontime' and c.endswith('.mem')]
    actual_plotargs = ({} if style.figsize is None else {"figsize": style.figsize}) | style.plot_kwargs
    ax = _plot_dataframe(view.resources, noshow=True, title=title, x="sessiontime", fields=mem_cols, descr=view.description, plotargs=actual_plotargs)
    _, top = ax.get_ylim()
    ax.set_ylim(0, top * 1.5)
    _apply_style(ax, style)
    apply_scene_shading(ax, view.scenes)
    return [ax]


def render_resource_bandwidth(view: ResourceView, *, title: str="Bandwidth usage", style: PlotStyle=PlotStyle()) -> List[Axes]:
    """Render bandwidth usage from a ResourceView."""
    bw_cols = [c for c in view.resources.columns if c != 'sessiontime' and (c.endswith('.recv_bandwidth') or c.endswith('.sent_bandwidth'))]
    actual_plotargs = ({} if style.figsize is None else {"figsize": style.figsize}) | style.plot_kwargs
    ax = _plot_dataframe(view.resources, noshow=True, title=title, x="sessiontime", fields=bw_cols, descr=view.description, plotargs=actual_plotargs)
    _, top = ax.get_ylim()
    ax.set_ylim(0, top * 1.5)
    _apply_style(ax, style)
    apply_scene_shading(ax, view.scenes)
    return [ax]


def render_resources(view: ResourceView, *, style: PlotStyle=PlotStyle()) -> List[Axes]:
    """Render CPU, memory, and bandwidth subplots from a ResourceView."""
    return (render_resource_cpu(view, style=style) +
            render_resource_mem(view, style=style) +
            render_resource_bandwidth(view, style=style))


def _plot_latencies_for_tile(df: pd.DataFrame, tilenum: int, ax: Axes, sender: str="sender", receiver: str="receiver", plotargs: Dict[str, Any]={}) -> Axes:
    fields = [
        f"{sender}.pc.grabber.downsample_ms",
        f"{sender}.pc.grabber.encoder_queue_ms",
        f"{sender}.pc.encoder.encoder_ms",
        f"{sender}.pc.encoder.transmitter_queue_ms",
        f"{receiver}.pc.reader.{tilenum}.receive_ms",
        f"{receiver}.pc.decoder.{tilenum}.decoder_queue_ms",
        f"{receiver}.pc.decoder.{tilenum}.decoder_ms",
        f"{receiver}.pc.renderer.{tilenum}.renderer_queue_ms"
    ]
    todelete: List[str] = []
    for i in range(len(fields)):
        if fields[i] not in df.columns:
            alt = fields[i].replace(f'.{tilenum}.', '.all.')
            if alt in df.columns:
                fields[i] = alt
            else:
                print(f'Warning: missing field {fields[i]} in dataframe')
                todelete.append(fields[i])
    for f in todelete:
        fields.remove(f)
    latency_fields = [
        f"{receiver}.synchronizer.latency_ms",
        f"{receiver}.pc.renderer.{tilenum}.latency_ms",
        f"{receiver}.pc.renderer.{tilenum}.latency_max_ms",
    ]
    ax = df.ffill().plot(x="sessiontime", y=fields, kind="area", colormap="Paired", ax=ax, legend=False, **plotargs)
    df.ffill().plot(x="sessiontime", y=latency_fields, ax=ax, color=["blue", "red", "yellow"], legend=False)
    ax.set_title(f"Per-tile Latency contributions, tile={tilenum}")  # type: ignore
    return ax


def render_latencies_per_tile(view: LatencyPerTileView, *, style: PlotStyle=PlotStyle()) -> List[Axes]:
    """Render per-tile latency subplots from a LatencyPerTileView."""
    fig: Figure
    fig, axs = pyplot.subplots(view.nTiles, 1, sharex=True, sharey=True)  # type: ignore
    if style.figsize is not None:
        fig.set_size_inches(style.figsize)
    else:
        fig.set_figheight(fig.get_figheight() * (view.nTiles - 1))
        fig.set_figwidth(fig.get_figwidth() * 1.5)
    for i in range(view.nTiles):
        _plot_latencies_for_tile(view.per_tile, i, axs[i], sender=view.sender, receiver=view.receiver, plotargs=style.plot_kwargs)
    handles, labels = axs[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc='center right')  # type: ignore
    pyplot.subplots_adjust(right=0.66)
    for ax in axs:
        apply_scene_shading(ax, view.scenes)
    return list(axs)


def render_latencies(view: LatencyView, *, title: str="Latency contributions (ms)", style: PlotStyle=PlotStyle()) -> List[Axes]:
    """
    Render latency contributions from a LatencyView.

    Produces a stacked area plot of pipeline stage durations with end-to-end latency
    line overlays. Optionally overlays disruption events (frame drops, tile switches).
    """
    figsize = style.figsize if style.figsize is not None else (6, 4)
    ax = _plot_dataframe(view.area,
        noshow=True,
        title=title,
        x="sessiontime",
        descr=view.description,
        plotargs=dict(kind="area", colormap="Paired", figsize=figsize) | style.plot_kwargs,
    )
    latency_cols = ["synchronizer latency", "renderer latency", "max renderer latency"]
    latency_colors = ["blue", "red", "yellow"]
    if "voice latency" in view.end2end.columns:
        latency_cols.append("voice latency")
        latency_colors.append("green")
    view.end2end.interpolate().plot(x="sessiontime", y=latency_cols, ax=ax, color=latency_colors)
    if view.framedrops is not None:
        view.framedrops.plot(x='sessiontime', y=['PC Drop event'], marker='|', linestyle='None', color='red', ax=ax, zorder=4)
    if view.tileswitches is not None:
        view.tileswitches.plot(x='sessiontime', y=['Tile switch event'], marker='x', linestyle='None', color='blue', ax=ax, zorder=5)
    max_latency = view.end2end["renderer latency"].max()
    max_max_latency = view.end2end["max renderer latency"].max()
    max_sync_latency = view.end2end["synchronizer latency"].max()
    max_latency = max(max_latency, max_max_latency, max_sync_latency)
    if "voice latency" in view.end2end.columns:
        max_voice_latency = view.end2end["voice latency"].max()
        if not pd.isna(max_voice_latency):
            max_latency = max(max_latency, max_voice_latency)
    ax.set_ylim(0, max_latency * 1.1)  # auto-scale; _apply_style overrides top if ylim_top is set
    ax.set_xlim(left=0)
    pyplot.xlabel("Session time (s)")
    pyplot.ylabel("Latency (ms)")
    _apply_style(ax, style, reverse_legend=True, capitalize_labels=True)
    apply_scene_shading(ax, view.scenes)
    return [ax]


def render_framerates(view: FramerateView, *, title: str="Frames per second", style: PlotStyle=PlotStyle()) -> List[Axes]:
    """Render frames-per-second from a FramerateView."""
    actual_plotargs = ({} if style.figsize is None else {"figsize": style.figsize}) | style.plot_kwargs
    ax = _plot_dataframe(view.fps, noshow=True, title=title, x="sessiontime", descr=view.description, plotargs=actual_plotargs)
    _apply_style(ax, style)
    apply_scene_shading(ax, view.scenes)
    return [ax]


def render_framerates_dropped(view: FramerateView, *, title: str="FPS dropped", style: PlotStyle=PlotStyle()) -> List[Axes]:
    """Render dropped-frames-per-second from a FramerateView."""
    actual_plotargs = ({} if style.figsize is None else {"figsize": style.figsize}) | style.plot_kwargs
    ax = _plot_dataframe(view.fps_dropped, noshow=True, title=title, x="sessiontime", descr=view.description, plotargs=actual_plotargs)
    _apply_style(ax, style)
    apply_scene_shading(ax, view.scenes)
    return [ax]


def render_framerates_and_dropped(view: FramerateView, *, style: PlotStyle=PlotStyle()) -> List[Axes]:
    """Render fps and dropped-fps subplots from a FramerateView."""
    return render_framerates(view, style=style) + render_framerates_dropped(view, style=style)


def render_pointcounts(view: PointcountView, *, title: str="Receiver point counts", style: PlotStyle=PlotStyle()) -> List[Axes]:
    """Render receiver point counts from a PointcountView."""
    actual_plotargs = ({} if style.figsize is None else {"figsize": style.figsize}) | style.plot_kwargs
    ax = _plot_dataframe(view.pointcounts, noshow=True, title=title, x="sessiontime", descr=view.description, plotargs=actual_plotargs)
    _, top = ax.get_ylim()
    ax.set_ylim(0, top * 1.5)
    _apply_style(ax, style)
    apply_scene_shading(ax, view.scenes)
    return [ax]


def render_progress(view: ProgressView, *, title: str="Pointcloud Progress", style: PlotStyle=PlotStyle()) -> List[Axes]:
    """Render point cloud pipeline progress from a ProgressView."""
    df = view.progress
    columns = [c for c in df.columns if c != 'sessiontime']
    ax = None
    for y in columns:
        color = 'black'
        marker = None
        markevery = 100
        markoffset = 0
        if '.0' in y:
            color = 'red'
            markoffset = 20
        elif '.1' in y:
            color = 'blue'
            markoffset = 40
        elif '.2' in y:
            color = 'green'
            markoffset = 60
        elif '.3' in y:
            color = 'purple'
            markoffset = 80
        if '.encoder' in y:
            marker = '+'
            markoffset += 3
        elif '.writer' in y:
            marker = '>'
            markoffset += 6
        elif '.decoder' in y:
            marker = 'o'
            markoffset += 9
        elif '.preparer' in y:
            marker = '<'
            markoffset += 12
        elif '.reader' in y:
            marker = '*'
            markoffset += 15
        series = df.loc[:, ["sessiontime", y]]
        ax = series.ffill().plot(x="sessiontime", marker=marker, markevery=(markoffset, markevery), color=color, alpha=0.5, ax=ax, **style.plot_kwargs)
    assert ax
    if style.figsize is not None:
        ax.get_figure().set_size_inches(style.figsize)  # type: ignore
    props = dict(boxstyle='round', facecolor='wheat', alpha=0.5)
    ax.text(0.98, 0.98, view.description, transform=ax.transAxes, verticalalignment='top', horizontalalignment='right', fontsize='x-small', bbox=props)  # type: ignore
    ax.set_title(title)  # type: ignore
    _apply_style(ax, style)
    apply_scene_shading(ax, view.scenes)
    return [ax]


# ── plot_* convenience wrappers: DataStore → List[Axes] ───────────────────────
# Each calls extract_*() + render_*() + publish_plots().
# Signatures are kept backward-compatible; styling params are bridged to PlotStyle.

def plot_pointcounts(ds: DataStore, dirname: Optional[str]=None, showplot: bool=True, saveplot: bool=False) -> List[Axes]:
    axes = render_pointcounts(extract_pointcounts(ds))
    publish_plots(axes, dirname=dirname, file_name="pointcounts.pdf", showplot=showplot, saveplot=saveplot)
    return axes

def plot_resource_cpu(ds: DataStore) -> List[Axes]:
    return render_resource_cpu(extract_resources(ds))

def plot_resource_mem(ds: DataStore) -> List[Axes]:
    return render_resource_mem(extract_resources(ds))

def plot_resource_bandwidth(ds: DataStore) -> List[Axes]:
    return render_resource_bandwidth(extract_resources(ds))

def plot_resources(ds: DataStore, dirname: Optional[str]=None, showplot: bool=True, saveplot: bool=False) -> List[Axes]:
    axes = render_resources(extract_resources(ds))
    publish_plots(axes, dirname=dirname, file_name="resources.pdf", showplot=showplot, saveplot=saveplot)
    return axes

def plot_latencies_per_tile(ds: DataStore, dirname: Optional[str]=None, showplot: bool=True, saveplot: bool=False) -> List[Axes]:
    axes = render_latencies_per_tile(extract_latencies_per_tile(ds))
    publish_plots(axes, dirname=dirname, file_name="latencies-per-tile.pdf", showplot=showplot, saveplot=saveplot)
    return axes

def plot_latencies(ds: DataStore, dpi: float|Literal["figure"]="figure", format: str="pdf", file_name: str="latencies.pdf", title: str="Latency contributions (ms)", label_dict: Dict[str, Any]={}, tick_dict: Dict[str, Any]={}, legend_dict: Dict[str, Any]={}, labelspacing: float=0.5, ncols: int=1, use_row_major: bool=False, dirname: Optional[str]=None, showplot: bool=True, saveplot: bool=False, max_y: float=0, figsize: Tuple[int, int]=(6, 4), show_disruptions: bool=False, plotargs: Dict[str, Any]={}) -> List[Axes]:
    legend_kwargs: Dict[str, Any] = {'labelspacing': labelspacing, 'ncols': ncols}
    if legend_dict:
        legend_kwargs['prop'] = legend_dict
    style = PlotStyle(
        figsize=figsize,
        ylim_top=max_y,
        plot_kwargs=plotargs,
        label_kwargs=label_dict,
        tick_kwargs=tick_dict,
        legend_kwargs=legend_kwargs,
        legend_row_major=use_row_major,
    )
    axes = render_latencies(extract_latencies(ds, show_framedrops=show_disruptions, show_tileswitches=show_disruptions), title=title, style=style)
    publish_plots(axes, dirname=dirname, file_name=file_name, format=format, dpi=dpi, showplot=showplot, saveplot=saveplot)
    return axes

def plot_framerates(ds: DataStore, plotargs: Dict[str, Any]={}) -> List[Axes]:
    return render_framerates(extract_framerates(ds), style=PlotStyle(plot_kwargs=plotargs))

def plot_framerates_dropped(ds: DataStore, plotargs: Dict[str, Any]={}) -> List[Axes]:
    return render_framerates_dropped(extract_framerates(ds), style=PlotStyle(plot_kwargs=plotargs))

def plot_framerates_and_dropped(ds: DataStore, dirname: Optional[str]=None, showplot: bool=True, saveplot: bool=False, plotargs: Dict[str, Any]={}) -> List[Axes]:
    axes = render_framerates_and_dropped(extract_framerates(ds), style=PlotStyle(plot_kwargs=plotargs))
    publish_plots(axes, dirname=dirname, file_name="framerates.pdf", showplot=showplot, saveplot=saveplot)
    return axes

def plot_progress(ds: DataStore, dirname: Optional[str]=None, showplot: bool=True, saveplot: bool=False, plotargs: Dict[str, Any]={}) -> List[Axes]:
    axes = render_progress(extract_progress(ds), style=PlotStyle(plot_kwargs=plotargs))
    publish_plots(axes, dirname=dirname, file_name="progress.pdf", showplot=showplot, saveplot=saveplot)
    return axes


# ── Register renderers ─────────────────────────────────────────────────────────

def render_voice(view: VoiceView, *, title: str="Voice activity", style: PlotStyle=PlotStyle()) -> List[Axes]:
    """Render voice levels per role from a VoiceView."""
    figsize = style.figsize or (14, 4)
    fig, ax = pyplot.subplots(figsize=figsize)

    apply_scene_shading(ax, view.scenes)

    level_cols = [c for c in view.voice_levels.columns if c != 'sessiontime']
    for col in level_cols:
        role = col.removesuffix('.level')
        sub = view.voice_levels[['sessiontime', col]].dropna()
        ax.plot(sub['sessiontime'], sub[col],
                color=view.role_colors[role], linewidth=0.8, label=view.labels[role])

    ax.set_xlabel("Session time (s)")
    ax.set_ylabel("Voice level")
    ax.set_ylim(0, 1)
    ax.set_title(f"{title} — {view.description}")
    if len(level_cols) > 1:
        ax.legend()
    return [ax]


def render_gaze(view: GazeView, *, title: str="Gaze activity", style: PlotStyle=PlotStyle()) -> List[Axes]:
    """Render a timeline of gaze segments per target from a GazeView."""
    all_targets = sorted(view.segments['target'].unique()) if not view.segments.empty else []
    target_idx = {t: i for i, t in enumerate(all_targets)}
    n_targets = len(all_targets)
    n_roles = len(view.roles)

    bar_height = 0.8 / max(n_roles, 1)
    offsets = [i * bar_height - (n_roles - 1) * bar_height / 2 for i in range(n_roles)]

    figsize = style.figsize or (14, max(3, n_targets * 0.6 + 1))
    fig, ax = pyplot.subplots(figsize=figsize)

    apply_scene_shading(ax, view.scenes)

    for _, seg in view.segments.iterrows():
        y = target_idx[seg['target']] + offsets[view.roles.index(seg['role'])]
        ax.barh(y, seg['t_end'] - seg['t_start'], left=seg['t_start'],
                height=bar_height * 0.85,
                color=view.role_colors[seg['role']], align='center', zorder=2)

    ax.set_yticks(range(n_targets))
    ax.set_yticklabels([t.replace('GazeTarget_', '') for t in all_targets], fontsize=8)
    ax.set_xlabel("Session time (s)")
    ax.set_ylim(-0.5, n_targets - 0.5)
    ax.set_title(f"{title} — {view.description}")

    if n_roles > 1:
        legend_patches = [mpatches.Patch(color=view.role_colors[r], label=view.labels[r]) for r in view.roles]
        ax.legend(handles=legend_patches, loc='lower right')

    return [ax]


def render_end_to_end_temporal_quality(view: EndToEndTemporalQualityView, *, title: str="End-to-end temporal quality", style: PlotStyle=PlotStyle()) -> List[Axes]:
    """Render one latency panel and one continuity panel per medium from an EndToEndTemporalQualityView."""
    media = [m for m in CONTINUITY_MEASURES if m in set(view.continuity["medium"])]
    n_panels = 1 + len(media)
    figsize = style.figsize or (14, 3 * n_panels)
    fig, axes = pyplot.subplots(n_panels, 1, figsize=figsize, sharex=True)
    axes = list(axes)
    linestyles = {"voice": "-", "point cloud": "--"}

    for ax in axes:
        apply_scene_shading(ax, view.scenes)

    def plot_lines(ax: Axes, table: pd.DataFrame, with_medium: bool) -> None:
        for (sender, receiver, medium), line in table.groupby(["sender", "receiver", "medium"], sort=False):
            label = f"{view.labels[sender]} → {view.labels[receiver]}"
            if with_medium:
                label += f" {medium} ({LATENCY_STATISTICS[medium]})"
            ax.plot(line["sessiontime"], line["value"], color=view.role_colors[sender],
                    linestyle=linestyles[medium], linewidth=0.8, label=label)
        ax.legend()

    plot_lines(axes[0], view.latency, True)
    axes[0].set_ylabel("End-to-end latency (ms)")
    heading = f"{title} — {view.description}"
    if view.clock_desync:
        heading += f"\n{view.clock_desync}"
    axes[0].set_title(heading)
    for ax, medium in zip(axes[1:], media):
        plot_lines(ax, view.continuity[view.continuity["medium"] == medium], False)
        ax.set_ylabel(CONTINUITY_MEASURES[medium])
    axes[-1].set_xlabel("Session time (s)")
    return axes


LatencyView.register_renderer(render_latencies)
LatencyPerTileView.register_renderer(render_latencies_per_tile)
ResourceView.register_renderer(render_resources)
FramerateView.register_renderer(render_framerates_and_dropped)
PointcountView.register_renderer(render_pointcounts)
ProgressView.register_renderer(render_progress)
VoiceView.register_renderer(render_voice)
GazeView.register_renderer(render_gaze)
EndToEndTemporalQualityView.register_renderer(render_end_to_end_temporal_quality)
