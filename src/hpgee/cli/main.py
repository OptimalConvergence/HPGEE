"""Single capability-based command entrypoint for HPGEE."""
import argparse
from importlib import import_module
import sys

_COMMANDS = {
    "sample": ("hpgee.cli.sampling", "run"),
    "catalog": ("hpgee.cli.sampling", "catalog"),
    "metadata": ("hpgee.cli.sampling", "metadata"),
    "progress": ("hpgee.cli.sampling", "progress"),
    "compute": ("hpgee.cli.computation", None),
    "workflow": ("hpgee.workflows.detection", None),
    "benchmark": ("hpgee.benchmarks.computation", None),
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="hpgee", description=__doc__)
    parser.add_argument("command", choices=_COMMANDS,
                        help="Choose sampling, catalog, computation or product execution")
    if not argv or argv[0] in ("-h", "--help"):
        parser.print_help()
        return 0 if argv else 2
    args = parser.parse_args(argv[:1])
    module, prefix = _COMMANDS[args.command]
    remaining = ([prefix] if prefix else []) + argv[1:]
    return import_module(module).main(remaining)
