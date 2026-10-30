import argparse
import importlib
import sys
import os
import matplotlib.pyplot as pyplot
from importlib.metadata import version as _pkg_version
from ..datastore import DataStore
from ..views import View
from ..plots import plot_simple, PlotStyle, publish_plots

def main():
    parser = argparse.ArgumentParser(description="Plot datastore file")
    parser.add_argument("--version", action="version", version=f"%(prog)s {_pkg_version('VRTstatistics')}")
    parser.add_argument("--import", dest="imports", metavar="MODULE", action="append", default=[],
                        help="Import MODULE before running (use to register external plot types)")
    parser.add_argument("--list-types", action="store_true",
                        help="List available standard plot types and exit")
    parser.add_argument("--type", metavar="TYPE",
                        help="Produce a standard plot of the given type (see --list-types)")
    parser.add_argument("-d", "--datastore", metavar="FILE",
                        help="DataStore file to plot")
    parser.add_argument(
        "-o", "--output", metavar="FILE", help="Output plot image file (default: show interactively)"
    )
    parser.add_argument(
        "-p", "--predicate", metavar="EXPR", default=None,
        help="Plot only data matching EXPR predicate (ad-hoc mode)"
    )
    parser.add_argument(
        "-x", "--x", metavar="FIELD", default="sessiontime",
        help="Field to use as x-axis (ad-hoc mode)"
    )
    parser.add_argument(
        "-t", "--title", metavar="TITLE", default=None,
        help="Set plot title"
    )
    parser.add_argument(
        "fields", default=None, nargs="*", metavar="FIELD",
        help="Field mappings to plot (ad-hoc mode; default: all)"
    )
    parser.add_argument("--pausefordebug", action="store_true",
                        help="Wait for a newline after start (so you can attach a debugger)")
    args = parser.parse_args()

    if args.pausefordebug:
        sys.stderr.write(f"Attach debugger to pid={os.getpid()}. Press return to continue - ")
        sys.stderr.flush()
        sys.stdin.readline()

    for mod in args.imports:
        importlib.import_module(mod)

    if args.list_types:
        print("Available plot types:")
        for name, cls in sorted(View._registry.items()):
            doc = (cls.__doc__ or "").strip().splitlines()[0]
            print(f"  {name:20s}  {doc}")
        return

    if args.type:
        if not args.datastore:
            parser.error("--type requires --datastore")
        view_cls = View.find(args.type)
        if view_cls is None:
            parser.error(f"Unknown type {args.type!r}. Use --list-types to see available types.")
        ds = DataStore(args.datastore)
        ds.load()
        view = view_cls.extract(ds)
        kwargs: dict = {'style': PlotStyle()}
        if args.title:
            kwargs['title'] = args.title
        axes = view.render(**kwargs)
        if args.output:
            dirname = os.path.dirname(os.path.abspath(args.output))
            file_name = os.path.basename(args.output)
            publish_plots(axes, showplot=False, saveplot=True, dirname=dirname, file_name=file_name)
        else:
            publish_plots(axes, showplot=True, saveplot=False)
        return

    # Ad-hoc mode: plot_simple with predicate and field selection
    if not args.datastore:
        parser.error("--datastore is required")
    datastore = DataStore(args.datastore)
    datastore.load()
    plt = plot_simple(datastore, title=args.title, noshow=not not args.output,
                      predicate=args.predicate, x=args.x, fields=args.fields)
    if args.output:
        pyplot.savefig(args.output)


if __name__ == "__main__":
    main()
