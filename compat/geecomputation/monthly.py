"""Compatibility alias; implementation lives in hpgee.compute.monthly."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.compute.monthly")
