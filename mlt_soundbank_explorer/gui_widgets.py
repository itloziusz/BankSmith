from __future__ import annotations

import csv
import json
import math
import os
import platform
import queue
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .core import *
from .dsp import *
from .audio import *
from .bank import *

class VerticalScrolledFrame(ttk.Frame):
    """Scrollable control panel whose vertical bar appears only when required."""

    def __init__(self, master, *, padding=0):
        super().__init__(master)
        background = ttk.Style(self).lookup("TFrame", "background")
        if not background:
            background = self.winfo_toplevel().cget("background")

        self.canvas = tk.Canvas(
            self,
            highlightthickness=0,
            borderwidth=0,
            background=background,
        )
        # A classic Tk scrollbar remains clearly visible with Windows themes.
        self.scrollbar = tk.Scrollbar(
            self,
            orient=tk.VERTICAL,
            command=self.canvas.yview,
            width=16,
        )
        self.content = ttk.Frame(self.canvas, padding=padding)
        self._content_window = self.canvas.create_window(
            (0, 0), window=self.content, anchor=tk.NW
        )
        self._scrollbar_visible = False
        self._sync_job: Optional[str] = None

        self.canvas.configure(yscrollcommand=self._on_canvas_yview)
        self.canvas.grid(row=0, column=0, sticky=tk.NSEW)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.content.bind("<Configure>", self._schedule_scroll_sync, add="+")
        self.canvas.bind("<Configure>", self._on_canvas_configure, add="+")

        # Mouse-wheel events are global in Tk, but scrolling is restricted to
        # widgets that belong to this panel.
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self.canvas.bind_all("<Button-4>", self._on_mousewheel, add="+")
        self.canvas.bind_all("<Button-5>", self._on_mousewheel, add="+")
        self.after_idle(self._sync_scroll_state)

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self._content_window, width=max(1, event.width))
        self._schedule_scroll_sync()

    def _schedule_scroll_sync(self, _event=None) -> None:
        if self._sync_job is not None:
            try:
                self.after_cancel(self._sync_job)
            except tk.TclError:
                pass
        self._sync_job = self.after_idle(self._sync_scroll_state)

    def _sync_scroll_state(self) -> None:
        self._sync_job = None
        bbox = self.canvas.bbox("all")
        self.canvas.configure(scrollregion=bbox or (0, 0, 0, 0))

        content_height = 0 if bbox is None else max(0, int(bbox[3] - bbox[1]))
        viewport_height = max(1, int(self.canvas.winfo_height()))
        needs_scrollbar = content_height > viewport_height + 1

        if needs_scrollbar and not self._scrollbar_visible:
            self.scrollbar.grid(row=0, column=1, sticky=tk.NS)
            self._scrollbar_visible = True
        elif not needs_scrollbar and self._scrollbar_visible:
            self.scrollbar.grid_remove()
            self._scrollbar_visible = False
            self.canvas.yview_moveto(0.0)

    def _on_canvas_yview(self, first: str, last: str) -> None:
        self.scrollbar.set(first, last)

    def _contains_widget(self, widget) -> bool:
        current = widget
        while current is not None:
            if current in (self, self.canvas, self.content):
                return True
            current = getattr(current, "master", None)
        return False

    def _on_mousewheel(self, event):
        if not self._contains_widget(getattr(event, "widget", None)):
            return None
        if not self._scrollbar_visible:
            return None

        if getattr(event, "num", None) == 4:
            units = -1
        elif getattr(event, "num", None) == 5:
            units = 1
        else:
            delta = int(getattr(event, "delta", 0))
            if delta == 0:
                return None
            units = -max(1, abs(delta) // 120) if delta > 0 else max(1, abs(delta) // 120)

        self.canvas.yview_scroll(units, "units")
        return "break"


