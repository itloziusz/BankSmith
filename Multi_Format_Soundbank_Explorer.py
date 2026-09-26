#!/usr/bin/env python3
"""Legacy compatibility launcher for the BankSmith multi-format GUI."""
from __future__ import annotations

import sys

from mlt_soundbank_explorer.multi_gui import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
