import argparse
import sys
import os
import matplotlib.pyplot as pyplot
from importlib.metadata import version as _pkg_version
from ..datastore import DataStore, DataStoreError
from ..views import View
from ..plots import plot_simple, PlotStyle, publish_plots
from ._modimport import import_external_module

def main():
    parser = argparse.ArgumentParser(description="Plot datastore file")
    parser.add_argument("--version", action="version", version=f"%(prog)s {_pkg_version('VRTstatistics')}")
    parser.add_argument("--module", "--import", dest="modules", metavar="MODULE", action="append", default=[],
                        help="Import MODULE before running (allows external plot types to register themselves). If MODULE isn't importable as-is, searches upward from the current directory for a matching MODULE.py or MODULE/ package.")
    parser.add_argument("--list-types", action="store_true",
                        help="List available standard plot types and exit")
    parser.add_argument("--type", dest="types", metavar="TYPE", action="append", default=[],
                        help="Produce a standard plot of the given type (see --list-types). Repeat for several plots.")
    parser.add_argument("--save-dir", metavar="DIR", default=None,
                        help="With --type: save each plot in DIR under its default filename, instead of showing it")
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

    for mod in args.modules:
        import_external_module(mod)

    if args.list_types:
        print("Available plot types:")
        for name, cls in sorted(View._registry.items()):
            doc = (cls.__doc__ or "").strip().splitlines()[0]
            print(f"  {name:20s}  {doc}")
        return

    if args.types:
        if not args.datastore:
            parser.error("--type requires --datastore")
        if args.output and args.save_dir:
            parser.error("--output and --save-dir can't be combined")
        if args.output and len(args.types) > 1:
            parser.error("--output needs a single --type; use --save-dir for several")
        view_classes = {}
        for name in args.types:
            view_cls = View.find(name)
            if view_cls is None:
                parser.error(f"Unknown type {name!r}. Use --list-types to see available types.")
            if args.save_dir and not view_cls.default_filename:
                parser.error(f"Type {name!r} has no default filename, so can't be used with --save-dir")
            view_classes[name] = view_cls
        ds = DataStore(args.datastore)
        ds.load()
        kwargs: dict = {'style': PlotStyle()}
        if args.title:
            kwargs['title'] = args.title
        failed = False
        for name, view_cls in view_classes.items():
            try:
                axes = view_cls.extract(ds).render(**kwargs)
            except DataStoreError as e:
                print(f"{parser.prog}: {name}: {e}", file=sys.stderr)
                failed = True
                continue
            if args.save_dir:
                publish_plots(axes, showplot=False, saveplot=True, dirname=args.save_dir, file_name=view_cls.default_filename)
                print(f"Saved {os.path.join(args.save_dir, view_cls.default_filename)}")
                pyplot.close('all')
            elif args.output:
                dirname = os.path.dirname(os.path.abspath(args.output))
                file_name = os.path.basename(args.output)
                publish_plots(axes, showplot=False, saveplot=True, dirname=dirname, file_name=file_name)
        if not args.save_dir and not args.output:
            pyplot.show()
        sys.exit(1 if failed else 0)

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
