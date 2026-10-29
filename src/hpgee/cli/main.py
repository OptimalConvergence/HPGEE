"""Single capability-based command entrypoint for HPGEE."""
import argparse
from importlib import import_module
import sys

# Sampling handlers keep their legacy subcommand, so their usage already names it.
_COMMANDS = {
    "sample": ("hpgee.cli.sampling", "run", "hpgee"),
    "catalog": ("hpgee.cli.sampling", "catalog", "hpgee"),
    "metadata": ("hpgee.cli.sampling", "metadata", "hpgee"),
    "progress": ("hpgee.cli.sampling", "progress", "hpgee"),
    "compute": ("hpgee.cli.computation", None, "hpgee compute"),
    "workflow": ("hpgee.workflows.detection", None, "hpgee workflow"),
    "benchmark": ("hpgee.benchmarks.computation", None, "hpgee benchmark"),
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
    module, prefix, prog = _COMMANDS[args.command]
    remaining = ([prefix] if prefix else []) + argv[1:]
    return import_module(module).main(remaining, prog=prog)
