"""Compatibility alias; implementation lives in hpgee.runtime.transport."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.runtime.transport")
