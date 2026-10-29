"""Compatibility alias; implementation lives in hpgee.catalog.metadata."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.catalog.metadata")
