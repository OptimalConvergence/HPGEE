"""Compatibility alias; implementation lives in hpgee.reporting.progress."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.reporting.progress")
