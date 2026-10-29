"""Compatibility alias; implementation lives in hpgee.planning.cube."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.planning.cube")
