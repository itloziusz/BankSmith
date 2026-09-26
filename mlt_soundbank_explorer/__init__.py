"""BankSmith multi-format soundbank toolkit.\n\nThe historical mlt_soundbank_explorer package name is retained for compatibility.\n"""

from .branding import APP_NAME, APP_TITLE, PROJECT_TOOL_NAME, BANK_TREE_TOOL_NAME
from .core import *
from .dsp import *
from .audio import *
from .render_audio import *
from .bank import MLTBank
from .gui import MLTExplorerApp
from .formats import SoundbankProbe, ProbeEntry, inspect_soundbank, extract_mdt_blocks, format_summary_text
from .adapters import StandaloneGCAXMPBBank, open_editable_bank, wrap_gcax_mpb_as_mlt
from .dreamcast_bank import (
    DreamcastStandaloneMPBBank,
    DreamcastSMLTBank,
    DreamcastMDTBank,
)
from .multi_gui import BankSmithApp, MultiFormatExplorerApp
from .cli import main, print_cli_usage

__all__ = [
    "APP_NAME",
    "APP_TITLE",
    "PROJECT_TOOL_NAME",
    "BANK_TREE_TOOL_NAME",
    "MLTBank",
    "MLTExplorerApp",
    "MultiFormatExplorerApp",
    "BankSmithApp",
    "StandaloneGCAXMPBBank",
    "DreamcastStandaloneMPBBank",
    "DreamcastSMLTBank",
    "DreamcastMDTBank",
    "SoundbankProbe",
    "ProbeEntry",
    "open_editable_bank",
    "wrap_gcax_mpb_as_mlt",
    "inspect_soundbank",
    "extract_mdt_blocks",
    "format_summary_text",
    "main",
    "print_cli_usage",
]
