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
from .branding import APP_TITLE

from .gui_widgets import *

class MLTExplorerBase:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1180x720")
        self.bank: Optional[MLTBank] = None
        self.current_temp_wav: Optional[Path] = None
        self._play_generation = 0
        self._preview_process: Optional[subprocess.Popen] = None
        self._background_active = False
        self._closing = False
        self._settings_refresh_job: Optional[str] = None
        self._ui_queue: queue.Queue = queue.Queue()
        self._ui_queue_job: Optional[str] = None
        self.status_var = tk.StringVar(value="Open a soundbank to begin.")
        self.filter_var = tk.StringVar()
        self.loop_preview_seconds_var = tk.IntVar(value=20)
        self.loop_declick_var = tk.BooleanVar(value=True)
        self.loop_zero_cross_var = tk.BooleanVar(value=True)
        self.loop_trim_silence_var = tk.BooleanVar(value=True)
        self.loop_crossfade_ms_var = tk.DoubleVar(value=3.0)
        self.pitch_correct_var = tk.BooleanVar(value=False)
        self.trigger_note_var = tk.IntVar(value=DEFAULT_TRIGGER_NOTE)
        self.preserve_bank_rate_var = tk.BooleanVar(value=True)
        self.detail_var = tk.StringVar(value="No sample selected.")
        self.view_filter_var = tk.StringVar(value="All samples")
        self.rate_filter_var = tk.StringVar(value="Any stored rate word")
        self.program_filter_var = tk.StringVar(value="All programs")
        self.pitch_preset_var = tk.StringVar(value="Base stored/2 rate")
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._start_ui_queue_pump()

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Open Soundbank...", command=self.open_mlt, accelerator="Ctrl+O")
        file_menu.add_command(label="Save Repacked As...", command=self.save_as, accelerator="Ctrl+S")
        file_menu.add_separator()
        file_menu.add_command(label="Load Editor Project JSON...", command=self.load_project_json)
        file_menu.add_command(label="Save Editor Project JSON...", command=self.save_project_json)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.destroy)
        menubar.add_cascade(label="File", menu=file_menu)

        preview_menu = tk.Menu(menubar, tearoff=False)
        preview_menu.add_command(label="Preview Selected", command=self.preview_selected, accelerator="Space")
        preview_menu.add_command(label="Preview Selected in Loop Mode", command=self.preview_loop_selected)
        preview_menu.add_command(label="Stop Preview", command=self.stop_preview, accelerator="Esc")
        preview_menu.add_separator()
        preview_menu.add_checkbutton(label="Gapless/de-click loop preview", variable=self.loop_declick_var)
        preview_menu.add_checkbutton(label="Snap preview loop to nearby zero-crossing", variable=self.loop_zero_cross_var)
        preview_menu.add_checkbutton(label="Trim silent head inside loop preview", variable=self.loop_trim_silence_var)
        preview_menu.add_separator()
        preview_menu.add_radiobutton(label="Base sample-rate (stored / 2)", variable=self.pitch_preset_var, value="Base stored/2 rate", command=self.apply_pitch_preset)
        preview_menu.add_radiobutton(label="Game pitch: C4 / note 60", variable=self.pitch_preset_var, value="Game C4 / 60", command=self.apply_pitch_preset)
        preview_menu.add_radiobutton(label="Game pitch: C3 / note 48", variable=self.pitch_preset_var, value="Game C3 / 48", command=self.apply_pitch_preset)
        preview_menu.add_radiobutton(label="Custom trigger note", variable=self.pitch_preset_var, value="Custom", command=self.apply_pitch_preset)
        menubar.add_cascade(label="Preview", menu=preview_menu)

        export_menu = tk.Menu(menubar, tearoff=False)
        export_menu.add_command(label="Export Selected WAV...", command=self.export_selected)
        export_menu.add_command(label="Export Selected Loop Preview WAV...", command=self.export_loop_preview_selected)
        export_menu.add_command(label="Export All WAV...", command=self.export_all)
        export_menu.add_separator()
        export_menu.add_command(label="Export Selected Raw Encoded Payload...", command=self.export_selected_raw_payload)
        export_menu.add_command(label="Export All Raw Encoded Payloads...", command=self.export_all_raw_payloads)
        export_menu.add_separator()
        export_menu.add_command(label="Save Aliases CSV...", command=self.save_aliases)
        export_menu.add_command(label="Save Loop Report CSV...", command=self.save_loop_report)
        export_menu.add_command(label="Save Program Map CSV...", command=self.save_program_map_csv)
        export_menu.add_command(label="Save Bank Tree JSON...", command=self.save_bank_tree_json)
        export_menu.add_command(label="Save Replacement Manifest Template CSV...", command=self.save_replacement_manifest_template)
        menubar.add_cascade(label="Export", menu=export_menu)

        edit_menu = tk.Menu(menubar, tearoff=False)
        edit_menu.add_command(label="Replace Selected From WAV...", command=self.replace_selected)
        edit_menu.add_command(label="Batch Replace From Folder...", command=self.batch_replace_from_folder_gui)
        edit_menu.add_command(label="Clear Selected Replacement", command=self.clear_replacement)
        edit_menu.add_separator()
        edit_menu.add_command(label="Rename Alias...", command=self.rename_alias)
        menubar.add_cascade(label="Edit", menu=edit_menu)

        view_menu = tk.Menu(menubar, tearoff=False)
        for label in ["All samples", "Looped only", "One-shot only", "Mapped only", "Unmapped only", "Replaced only", "No replacement"]:
            view_menu.add_radiobutton(label=label, variable=self.view_filter_var, value=label, command=self.refresh_tree)
        view_menu.add_separator()
        view_menu.add_command(label="Show Program Map...", command=self.show_program_map)
        view_menu.add_command(label="Show Selected Layer / Hex Details...", command=self.show_selected_layer_fields)
        menubar.add_cascade(label="View", menu=view_menu)

        reports_menu = tk.Menu(menubar, tearoff=False)
        reports_menu.add_command(label="Run Complete Parameter Forensics...", command=self.save_parameter_forensics)
        reports_menu.add_separator()
        reports_menu.add_command(label="Run Deep Audit...", command=self.save_deep_audit)
        reports_menu.add_command(label="Run Sample-Rate Forensics...", command=self.save_samplerate_forensics)
        reports_menu.add_command(label="Validate Repack Plan...", command=self.validate_repack_plan_gui)
        reports_menu.add_command(label="Save Validation Report CSV...", command=self.save_validation_report_csv)
        reports_menu.add_command(label="Save Loop Preview Seam Report CSV...", command=self.save_loop_preview_seam_report_csv)
        reports_menu.add_command(label="Show Reverse Engineering Summary...", command=self.show_reverse_summary)
        menubar.add_cascade(label="Reports", menu=reports_menu)

        self.root.config(menu=menubar)
        self.root.bind("<Control-o>", lambda _e: self.open_mlt())
        self.root.bind("<Control-s>", lambda _e: self.save_as())
        self.root.bind("<space>", lambda _e: self.preview_selected())
        self.root.bind("<Escape>", lambda _e: self.stop_preview())

    def _build_ui(self) -> None:
        self._build_menu()
        top = ttk.Frame(self.root, padding=8)
        top.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(top, text="Open Soundbank", command=self.open_mlt).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(top, text="Save Repacked As", command=self.save_as).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(top, text="Export All WAV", command=self.export_all).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(top, text="Batch Replace", command=self.batch_replace_from_folder_gui).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(top, text="Validate", command=self.validate_repack_plan_gui).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(top, text="Save Aliases CSV", command=self.save_aliases).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(top, text="Save Loop Report CSV", command=self.save_loop_report).pack(side=tk.LEFT, padx=(0, 16))

        ttk.Label(top, text="Filter:").pack(side=tk.LEFT)
        filter_entry = ttk.Entry(top, textvariable=self.filter_var, width=32)
        filter_entry.pack(side=tk.LEFT, padx=(4, 6))
        filter_entry.bind("<KeyRelease>", lambda _e: self.refresh_tree())
        ttk.Button(top, text="Clear", command=lambda: (self.filter_var.set(""), self.refresh_tree())).pack(side=tk.LEFT, padx=(0, 12))

        ttk.Label(top, text="View:").pack(side=tk.LEFT)
        view_combo = ttk.Combobox(
            top,
            textvariable=self.view_filter_var,
            state="readonly",
            width=15,
            values=("All samples", "Looped only", "One-shot only", "Mapped only", "Unmapped only", "Replaced only", "No replacement"),
        )
        view_combo.pack(side=tk.LEFT, padx=(4, 8))
        view_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_tree())

        ttk.Label(top, text="Rate word:").pack(side=tk.LEFT)
        rate_combo = ttk.Combobox(
            top,
            textvariable=self.rate_filter_var,
            state="readonly",
            width=14,
            values=(
                "Any stored rate word",
                "15592 word (7796 Hz base)",
                "31183 word (15591.5 Hz base)",
                "33038 word (16519 Hz base)",
                "44100 word (22050 Hz base)",
                "62367 word (31183.5 Hz base)",
            ),
        )
        rate_combo.pack(side=tk.LEFT, padx=(4, 8))
        rate_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_tree())

        ttk.Label(top, text="Program filter:").pack(side=tk.LEFT)
        self.program_combo = ttk.Combobox(
            top,
            textvariable=self.program_filter_var,
            state="readonly",
            width=14,
            values=("All programs",),
        )
        self.program_combo.pack(side=tk.LEFT, padx=(4, 0))
        self.program_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_tree())

        mid = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        mid.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        left = ttk.Frame(mid)
        right_host = ttk.Frame(mid)
        self.details_scroller = VerticalScrolledFrame(right_host, padding=8)
        self.details_scroller.pack(fill=tk.BOTH, expand=True)
        right = self.details_scroller.content
        mid.add(left, weight=4)
        mid.add(right_host, weight=2)

        columns = ("idx", "alias", "bank_rate", "wav_rate", "dur", "loop", "samples", "offset", "usage", "replacement")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        headers = {
            "idx": ("#", 50),
            "alias": ("Alias / recovered name", 285),
            "bank_rate": ("Rate word", 90),
            "wav_rate": ("WAV Hz", 75),
            "dur": ("Sec", 70),
            "loop": ("Loop", 55),
            "samples": ("Samples", 85),
            "offset": ("Offset", 90),
            "usage": ("Usage", 165),
            "replacement": ("Replacement WAV", 180),
        }
        for key, (label, width) in headers.items():
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor=tk.W if key in ("alias", "usage", "replacement") else tk.CENTER)
        yscroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.update_details())
        self.tree.bind("<Double-1>", lambda _e: self.preview_selected())
        self.tree.bind("<Button-3>", self.show_context_menu)
        self._build_context_menu()

        detail_label = ttk.Label(right, textvariable=self.detail_var, justify=tk.LEFT, wraplength=390)
        detail_label.pack(anchor=tk.NW, fill=tk.X, pady=(0, 12))

        ttk.Button(right, text="Preview", command=self.preview_selected).pack(fill=tk.X, pady=3)
        ttk.Button(right, text="Preview Loop Mode", command=self.preview_loop_selected).pack(fill=tk.X, pady=3)

        loop_row = ttk.Frame(right)
        loop_row.pack(fill=tk.X, pady=3)
        ttk.Label(loop_row, text="Loop preview sec:").pack(side=tk.LEFT)
        ttk.Spinbox(loop_row, from_=3, to=120, width=6, textvariable=self.loop_preview_seconds_var).pack(side=tk.RIGHT)

        fade_row = ttk.Frame(right)
        fade_row.pack(fill=tk.X, pady=3)
        ttk.Label(fade_row, text="Loop crossfade ms:").pack(side=tk.LEFT)
        ttk.Spinbox(fade_row, from_=0.0, to=20.0, increment=0.5, width=6, textvariable=self.loop_crossfade_ms_var).pack(side=tk.RIGHT)

        ttk.Checkbutton(right, text="Gapless/de-click loop preview", variable=self.loop_declick_var).pack(fill=tk.X, pady=2)
        ttk.Checkbutton(right, text="Zero-cross loop preview bounds", variable=self.loop_zero_cross_var).pack(fill=tk.X, pady=2)
        ttk.Checkbutton(right, text="Trim silent loop head in preview", variable=self.loop_trim_silence_var).pack(fill=tk.X, pady=2)

        ttk.Checkbutton(
            right,
            text="Game-pitch preview/export WAV",
            variable=self.pitch_correct_var,
            command=self.refresh_tree,
        ).pack(fill=tk.X, pady=3)

        note_row = ttk.Frame(right)
        note_row.pack(fill=tk.X, pady=3)
        ttk.Label(note_row, text="Trigger note:").pack(side=tk.LEFT)
        ttk.Spinbox(note_row, from_=0, to=127, width=6, textvariable=self.trigger_note_var, command=self.refresh_tree).pack(side=tk.LEFT, padx=4)
        ttk.Label(note_row, text="default C4 / 60").pack(side=tk.LEFT)

        preset_row = ttk.Frame(right)
        preset_row.pack(fill=tk.X, pady=3)
        ttk.Label(preset_row, text="Pitch preset:").pack(side=tk.LEFT)
        preset_combo = ttk.Combobox(
            preset_row,
            textvariable=self.pitch_preset_var,
            state="readonly",
            width=18,
            values=("Base stored/2 rate", "Game C4 / 60", "Game C3 / 48", "Custom"),
        )
        preset_combo.pack(side=tk.RIGHT)
        preset_combo.bind("<<ComboboxSelected>>", lambda _e: self.apply_pitch_preset())
        ttk.Checkbutton(
            right,
            text="Preserve original bank pitch on replace",
            variable=self.preserve_bank_rate_var,
        ).pack(fill=tk.X, pady=3)

        ttk.Button(right, text="Stop Preview", command=self.stop_preview).pack(fill=tk.X, pady=3)
        ttk.Separator(right).pack(fill=tk.X, pady=8)
        ttk.Button(right, text="Export Selected WAV", command=self.export_selected).pack(fill=tk.X, pady=3)
        ttk.Button(right, text="Export Loop Preview WAV", command=self.export_loop_preview_selected).pack(fill=tk.X, pady=3)
        ttk.Button(right, text="Replace Selected From WAV", command=self.replace_selected).pack(fill=tk.X, pady=3)
        ttk.Button(right, text="Clear Selected Replacement", command=self.clear_replacement).pack(fill=tk.X, pady=3)
        ttk.Button(right, text="Batch Replace From Folder", command=self.batch_replace_from_folder_gui).pack(fill=tk.X, pady=3)
        ttk.Button(right, text="Validate Repack Plan", command=self.validate_repack_plan_gui).pack(fill=tk.X, pady=3)
        ttk.Separator(right).pack(fill=tk.X, pady=8)
        ttk.Button(right, text="Rename Alias", command=self.rename_alias).pack(fill=tk.X, pady=3)
        ttk.Button(right, text="Show Program Map", command=self.show_program_map).pack(fill=tk.X, pady=3)
        ttk.Button(right, text="Selected Layer / Hex Details", command=self.show_selected_layer_fields).pack(fill=tk.X, pady=3)
        ttk.Button(right, text="Run Deep Audit", command=self.save_deep_audit).pack(fill=tk.X, pady=3)

        help_text = (
            "BankSmith workflow:\n"
            "1. Open a supported soundbank (.mlt, .mpb, .mdt).\n"
            "2. Export or preview samples/tones.\n"
            "3. Replace entries with PCM WAV files.\n"
            "4. Validate and Save Repacked As.\n\n"
            "Always keep the original source as a backup. BankSmith uses separate "
            "GameCube gcax and Dreamcast AICA backends, rebuilds affected offsets "
            "and containers, and provides gapless loop preview plus validation."
        )
        help_label = ttk.Label(right, text=help_text, justify=tk.LEFT, wraplength=390)
        help_label.pack(anchor=tk.SW, fill=tk.X, pady=(16, 0))

        def update_right_panel_wrap(event) -> None:
            wrap_width = max(180, int(event.width) - 36)
            detail_label.configure(wraplength=wrap_width)
            help_label.configure(wraplength=wrap_width)

        self.details_scroller.canvas.bind("<Configure>", update_right_panel_wrap, add="+")
        self.details_scroller.canvas.yview_moveto(0.0)

        bottom = ttk.Frame(self.root, padding=(8, 4))
        bottom.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Label(bottom, textvariable=self.status_var).pack(side=tk.LEFT, fill=tk.X, expand=True)

    def apply_pitch_preset(self) -> None:
        preset = self.pitch_preset_var.get()
        if preset == "Game C3 / 48":
            self.pitch_correct_var.set(True)
            self.trigger_note_var.set(48)
        elif preset == "Game C4 / 60":
            self.pitch_correct_var.set(True)
            self.trigger_note_var.set(60)
        elif preset == "Base stored/2 rate":
            self.pitch_correct_var.set(False)
        self.stop_preview(silent=True)
        self._schedule_settings_refresh()

    def _schedule_settings_refresh(self) -> None:
        """Coalesce quick setting changes into one tree/detail refresh."""
        if self._closing:
            return
        if self._settings_refresh_job is not None:
            try:
                self.root.after_cancel(self._settings_refresh_job)
            except tk.TclError:
                pass
        self._settings_refresh_job = self.root.after_idle(self._refresh_after_settings_change)

    def _refresh_after_settings_change(self) -> None:
        self._settings_refresh_job = None
        if self._closing:
            return
        self.refresh_tree()
        self.update_details()

    def _start_ui_queue_pump(self) -> None:
        if self._closing or self._ui_queue_job is not None:
            return
        self._ui_queue_job = self.root.after(30, self._drain_ui_queue)

    def _post_ui(self, callback, *args) -> None:
        self._ui_queue.put((callback, args))

    def _drain_ui_queue(self) -> None:
        self._ui_queue_job = None
        if self._closing:
            return
        while True:
            try:
                callback, args = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            callback(*args)
        self._ui_queue_job = self.root.after(30, self._drain_ui_queue)

    def _on_close(self) -> None:
        self._closing = True
        if self._ui_queue_job is not None:
            try:
                self.root.after_cancel(self._ui_queue_job)
            except tk.TclError:
                pass
            self._ui_queue_job = None
        try:
            self.stop_preview(silent=True)
        finally:
            self.root.destroy()

