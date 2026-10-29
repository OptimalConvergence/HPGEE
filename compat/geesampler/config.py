"""Compatibility alias; implementation lives in hpgee.sampling.config."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.sampling.config")
