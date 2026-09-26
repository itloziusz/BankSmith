#!/usr/bin/env python3
"""BankSmith — Multi-Format Soundbank Editor.

Primary launcher for GameCube gcax, Dreamcast AICA, and Sonic Shuffle soundbanks.
"""

from __future__ import annotations

import sys

from banksmith import *
from banksmith import main, print_cli_usage


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
