"""Compatibility alias; implementation lives in hpgee.transfer.delivery."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.transfer.delivery")
