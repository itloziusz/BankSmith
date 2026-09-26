#!/usr/bin/env python3
"""Compatibility launcher for the modular MLT Soundbank Explorer.

The implementation now lives under the mlt_soundbank_explorer package.
Existing imports and the historical script entry point remain supported.
"""
from __future__ import annotations

import sys

from mlt_soundbank_explorer.core import *
from mlt_soundbank_explorer.dsp import *
from mlt_soundbank_explorer.audio import *
from mlt_soundbank_explorer.bank import *
from mlt_soundbank_explorer.gui import *
from mlt_soundbank_explorer.clean_audio import *
from mlt_soundbank_explorer.cli import main, print_cli_usage

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
