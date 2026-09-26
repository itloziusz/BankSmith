"""BankSmith public package.

This compatibility-facing package exposes the BankSmith API while the historical
mlt_soundbank_explorer package remains available for existing integrations.
"""

from mlt_soundbank_explorer import *
from mlt_soundbank_explorer import __all__ as _legacy_all

__all__ = list(_legacy_all)
