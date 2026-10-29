"""Compatibility alias; implementation lives in hpgee.transfer.raw_download."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("hpgee.transfer.raw_download")
