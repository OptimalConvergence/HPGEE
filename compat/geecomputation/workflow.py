"""Compatibility alias; implementation lives in hpgee.workflows.detection."""
import importlib
import sys

if __name__ == "__main__":
    from hpgee.workflows.detection import main
    raise SystemExit(main())

sys.modules[__name__] = importlib.import_module("hpgee.workflows.detection")
