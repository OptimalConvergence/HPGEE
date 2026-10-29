"""Compatibility alias; implementation lives in hpgee.planning.candidates."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.planning.candidates")
