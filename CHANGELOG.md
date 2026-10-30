# Changelog

All notable changes to this project will be documented here.

Version numbers follow VR2Gather major.minor, with an independent patch number for
fixes and improvements within a VR2Gather release cycle. Versions are derived from
git tags (see `FILEVERSION` / `OLDEST_COMPATIBLE_VERSION` in `datastore.py` for the
combined.json format version scheme).

## [unreleased]

- `DataStore.roles()`: the session's roles, from session metadata or (failing that) the records; raises if there are none (#39)
- `DataStore.master_role()`: the role that created the orchestrator session (#39)
- `DataStore.orchtime_to_sessiontime()`: convert orchestrator-clock timestamps in record fields to session time (#39)
- Several instances of an annotation (`latency`), each stored under its full name `name(key=value,...)`; `engine.ensure()` returns the metadata, and raises where it used to silently do nothing or the short name is ambiguous (#34)

## [1.4.1] — 2026-07-07

- `VRTstatistics-ingest` gains `--module MODULE` (previously only on `VRTstatistics-annotate`), so ingest-time annotation steps defined outside VRTstatistics can register themselves in one step: `VRTstatistics-ingest -a <name> --module <module>`
- `--module` (both `ingest` and `annotate`) now falls back to searching upward from the current directory for a matching `<module>.py`/`<module>/` package when a plain import fails — lets a per-experiment analysis module (e.g. `vrts_trolley.py`) live at an experiment repo's root and still be importable from a session subdirectory several levels below, with no packaging/install step
- New internal `scripts/_modimport.py` shared by `ingest.py` and `annotate.py`
- readme: document the emerging per-experiment `vrts_<experiment>.py` pattern (custom `View`s + `AnnotationStep`s + `--module`); flag `VRTstatistics-plot`'s `--import` as inconsistent with the other two and due for unification later

## [1.4.0] — 2026-06-14

- Plot refactor (closes #21): extract / render / publish three-step pipeline
    - Introduce `PlotStyle` dataclass: `figsize`, `ylim_top`, `plot_kwargs`, `label_kwargs`, `tick_kwargs`, `legend_kwargs`, `legend_row_major` — construct once, reuse across all plots in an experiment
    - All `render_*` functions now take `style: PlotStyle`; `plot_*` wrappers remain backward-compatible
    - View registry: `View` subclasses self-register via `__init_subclass__`; `register_extractor` / `register_renderer` wire in functions (both overridable at runtime)
    - `VRTstatistics-plot --type NAME` produces a standard plot; `--list-types` enumerates registered types; `--import MODULE` loads external View subclasses
    - `extract_latencies` gains `show_framedrops` / `show_tileswitches` flags (replaces `show_disruptions` on `render_latencies`)
    - Add voice latency to `plot_latencies`; fix x-axis auto-scaling; role names in description
- Parser: fail fast on malformed stats lines (locale-induced comma decimal separators, cwi-dis/VR2Gather#318)
- Add `examples/latency-tiled-voice`; update `examples/latency` config
- readme: document NTP sync tools and Windows decimal separator workaround
- Refactor annotator into `SessionNormalizer` + declarative `AnnotationEngine` (closes #25)
    - New `VRTstatistics-annotate` CLI for re-annotating existing combined.json files
    - Add `--list` to `VRTstatistics-annotate` to show available annotation steps
    - Fix `combine()` to handle 1..N roles instead of exactly 2 (closes #23)
    - Fix `TileCombiner` double-applying `previous_filter` in chain (closes #19)
- Fix component_map collision when same component name appears on multiple machines
- Add `fileversion` to combined.json for schema compatibility checks
- Derive package versions from git tags via setuptools-scm
- Add `--version` to all CLI entry points

## [1.3.0] — last release before automated versioning

This is the last release that predates setuptools-scm version derivation. No
CHANGELOG was maintained before this point.
