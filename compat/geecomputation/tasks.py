"""Compatibility alias; implementation lives in hpgee.accounting.tasks."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.accounting.tasks")
