"""Compatibility alias; implementation lives in hpgee.reporting.resources."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.reporting.resources")
