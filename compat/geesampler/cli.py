"""Compatibility alias; implementation lives in hpgee.cli.sampling."""
import importlib
import sys

if __name__ == "__main__":
    from hpgee.cli.sampling import main
    raise SystemExit(main())

sys.modules[__name__] = importlib.import_module("hpgee.cli.sampling")
