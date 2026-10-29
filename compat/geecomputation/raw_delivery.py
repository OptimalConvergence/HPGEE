"""Compatibility alias; implementation lives in hpgee.transfer.raw_delivery."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.transfer.raw_delivery")
