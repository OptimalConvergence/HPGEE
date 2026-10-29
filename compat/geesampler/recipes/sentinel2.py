"""Compatibility alias; implementation lives in hpgee.recipes.sentinel2."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.recipes.sentinel2")
