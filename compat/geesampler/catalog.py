"""Compatibility alias; implementation lives in hpgee.catalog.catalog."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.catalog.catalog")
