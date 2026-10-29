"""Compatibility alias; implementation lives in hpgee.sampling.sources."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.sampling.sources")
