"""Compatibility exports for the modular clean-audio subsystem.

The implementation now lives in render_audio.py, gcax_render.py and gui_render.py.
No runtime monkey-patching is performed here.
"""
from .render_audio import *
from .gcax_render import GCAXRenderMixin
from .gui_render import GUIRenderMixin
