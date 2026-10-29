"""Compatibility alias; implementation lives in hpgee.recipes.mtbs."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.recipes.mtbs")
