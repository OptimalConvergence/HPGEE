"""Compatibility alias; implementation lives in hpgee.recipes.gedi."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.recipes.gedi")
