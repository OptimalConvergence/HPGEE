"""Compatibility alias; implementation lives in hpgee.accounting.quota."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.accounting.quota")
