"""Compatibility alias; implementation lives in hpgee.benchmarks.computation."""
import importlib
import sys

if __name__ == "__main__":
    from hpgee.benchmarks.computation import main
    raise SystemExit(main())

sys.modules[__name__] = importlib.import_module("hpgee.benchmarks.computation")
