"""Compatibility alias; implementation lives in hpgee.sampling.distributed."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.sampling.distributed")
