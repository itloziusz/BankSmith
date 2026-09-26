from __future__ import annotations

from .gui_base import MLTExplorerBase
from .gui_inspection import GUIInspectionMixin
from .gui_actions import GUIActionsMixin
from .gui_project import GUIProjectMixin


class MLTExplorerApp(
    GUIProjectMixin,
    GUIActionsMixin,
    GUIInspectionMixin,
    MLTExplorerBase,
):
    """Public Tkinter application composed from focused GUI layers."""

    pass


__all__ = ["MLTExplorerApp"]
