"""Compatibility alias; implementation lives in hpgee.workflows.config."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.workflows.config")
