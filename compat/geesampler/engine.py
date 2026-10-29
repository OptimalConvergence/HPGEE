"""Compatibility alias; implementation lives in hpgee.transfer.engine."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.transfer.engine")
