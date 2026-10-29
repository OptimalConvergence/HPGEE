"""Compatibility alias; implementation lives in hpgee.benchmarks.sampling."""
import importlib
import sys

if __name__ == "__main__":
    from hpgee.benchmarks.sampling import main
    raise SystemExit(main())

sys.modules[__name__] = importlib.import_module("hpgee.benchmarks.sampling")
