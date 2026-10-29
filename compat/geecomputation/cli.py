"""Compatibility alias; implementation lives in hpgee.cli.computation."""
import importlib
import sys

if __name__ == "__main__":
    from hpgee.cli.computation import main
    raise SystemExit(main())

sys.modules[__name__] = importlib.import_module("hpgee.cli.computation")
