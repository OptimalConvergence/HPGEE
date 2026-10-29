"""Compatibility alias; implementation lives in hpgee.runtime.retry."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.runtime.retry")
