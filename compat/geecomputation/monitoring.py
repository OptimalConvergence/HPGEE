"""Compatibility alias; implementation lives in hpgee.accounting.usage."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.accounting.usage")
