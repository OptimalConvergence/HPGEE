"""Compatibility alias; implementation lives in hpgee.catalog.resolver."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.catalog.resolver")
