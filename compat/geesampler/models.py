"""Compatibility alias; implementation lives in hpgee.core.models."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.core.models")
