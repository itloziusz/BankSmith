from __future__ import annotations

import csv
import json
import math
import os
import platform
import queue
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


class MLTExplorerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("MLT Soundbank Explorer")
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
        self.status_var = tk.StringVar(value="Open an MLT file to begin.")
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
        file_menu.add_command(label="Open MLT...", command=self.open_mlt, accelerator="Ctrl+O")
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
        export_menu.add_command(label="Export Selected Raw DSP Payload...", command=self.export_selected_raw_payload)
        export_menu.add_command(label="Export All Raw DSP Payloads...", command=self.export_all_raw_payloads)
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

        ttk.Button(top, text="Open MLT", command=self.open_mlt).pack(side=tk.LEFT, padx=(0, 6))
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
            "Workflow:\n"
            "1. Open .mlt.\n"
            "2. Export/preview samples.\n"
            "3. Replace entries with PCM WAV files.\n"
            "4. Save Repacked As.\n\n"
            "Always keep the original MLT as backup. The tool rebuilds MPBW and updates MPBP offsets automatically. Menus at the top expose extra export/audit/report actions. Loop preview is one gapless file with optional zero-cross/crossfade de-clicking. Game-pitch export uses stored sample-rate / 2 + split root key + trigger note. The default pitch preset is Base stored/2 rate."
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

    def _program_rows_for_gui(self) -> List[Dict[str, object]]:
        if not self.bank:
            return []
        bank = self.bank
        body = bank.data[bank.mpbp_body:bank.mpbp_body + bank.mpbp_size]
        program_count = bank.mpbp_program_count
        ptr_rel = bank.mpbp_program_pointer_rel
        rows: List[Dict[str, object]] = []
        for pi in range(program_count):
            if ptr_rel + pi * 4 + 4 > len(body):
                continue
            program_rel = ptr_rel + pi * 4
            layer_count = body[program_rel]
            program_unknown = body[program_rel + 1]
            layer_rel = u16be(body, program_rel + 2)
            for li in range(layer_count):
                descriptor_rel = layer_rel + li * MPBP_LAYER_DESCRIPTOR_SIZE
                if descriptor_rel + MPBP_LAYER_DESCRIPTOR_SIZE > len(body):
                    continue
                split_count = body[descriptor_rel]
                split_data_rel = u16be(body, descriptor_rel + 2)
                for si in range(split_count):
                    block_rel = split_data_rel + si * MPBP_SPLIT_SIZE
                    if block_rel + MPBP_SPLIT_SIZE > len(body):
                        continue
                    bl = body[block_rel:block_rel + MPBP_SPLIT_SIZE]
                    sample_idx = u32be(bl, 0)
                    root_key = bl[0x0C]
                    sample = bank.samples[sample_idx] if sample_idx < len(bank.samples) else None
                    game_rate_exact = effective_game_rate_exact(
                        sample.current_sample_rate, root_key, int(self.trigger_note_var.get())
                    )[0] if sample else ""
                    game_rate = effective_game_audition_rate(
                        sample.current_sample_rate, root_key, int(self.trigger_note_var.get())
                    )[0] if sample else ""
                    rows.append({
                        "program": pi,
                        "layer": li,
                        "split": si,
                        "program_unknown_byte_0x01": program_unknown,
                        "sample_index": sample_idx,
                        "alias": sample.alias if sample else "",
                        "root_key": root_key,
                        "root_note": note_name(root_key),
                        "stored_rate_word_x2": sample.current_sample_rate if sample else "",
                        "base_rate_exact_hz": f"{sample.current_base_sample_rate_exact:.6f}" if sample else "",
                        "effective_game_rate_exact_hz": f"{game_rate_exact:.6f}" if sample else "",
                        "wav_header_rate_hz": game_rate,
                        # Compatibility aliases used by the current tree view.
                        "bank_rate": sample.current_sample_rate if sample else "",
                        "game_rate": game_rate,
                        "key_min": bl[0x06],
                        "key_max": bl[0x07],
                        "vel_min": bl[0x08],
                        "vel_max": bl[0x09],
                        "output_level_candidate_0A": bl[0x0A],
                        "fx_route_candidate_0B": bl[0x0B],
                        "encoding_mode_u16_0E": u16be(bl, 0x0E),
                        "env_attack_0x10": bl[0x10],
                        "env_release_0x14": bl[0x14],
                        "layer_descriptor_rel": descriptor_rel,
                        "block_rel": block_rel,
                        "block_hex": bl.hex(" "),
                    })
        return rows

    def _bank_tree_dict(self) -> Dict[str, object]:
        if not self.bank:
            return {}
        bank = self.bank
        programs: Dict[int, Dict[str, object]] = {}
        for row in self._program_rows_for_gui():
            pi = int(row["program"])
            li = int(row["layer"])
            prog = programs.setdefault(pi, {"program": pi, "layers": {}})
            layers = prog["layers"]
            assert isinstance(layers, dict)
            layer = layers.setdefault(li, {"layer": li, "splits": []})
            layer["splits"].append(row)
        program_list = []
        for pi in sorted(programs):
            prog = programs[pi]
            layers = prog["layers"]
            prog["layers"] = [layers[li] for li in sorted(layers)]
            program_list.append(prog)
        samples = []
        for s in bank.samples:
            root = bank.sample_root_key(s.index)
            rate, semis, note = effective_game_audition_rate(s.current_sample_rate, root, int(self.trigger_note_var.get()))
            samples.append({
                "index": s.index,
                "alias": s.alias,
                "stored_rate_word_x2": s.current_sample_rate,
                "base_rate_exact_hz": f"{s.current_base_sample_rate_exact:.6f}",
                "game_preview_rate": rate,
                "root_key": root,
                "root_note": note_name(root) if root is not None else None,
                "pitch_shift_semitones": semis,
                "sample_count": s.current_sample_count,
                "loop_flag": bool(s.loop_flag),
                "loop_points_samples": self.bank.loop_points_samples(s.index),
                "mpbw_offset": s.data_offset,
                "usage": s.usage,
                "replacement": s.replacement_label,
            })
        return {
            "tool": "MLT Soundbank Explorer bank tree",
            "source": str(bank.path),
            "mltm_directory": [
                {
                    "index": entry.index,
                    "dummy": entry.is_dummy,
                    "type_id": entry.type_id,
                    "type_name": {1: "gcaxMPB", 4: "gcaxMSB"}.get(entry.type_id, "unknown") if not entry.is_dummy else "dummy",
                    "bank_id": entry.bank_id if not entry.is_dummy else None,
                    "pointer_rel_to_abs_0x20": entry.pointer_rel if not entry.is_dummy else None,
                    "pointer_abs": entry.pointer_abs if not entry.is_dummy else None,
                    "raw_hex": entry.raw.hex(" "),
                }
                for entry in bank.mlt_directory_entries
            ],
            "mpbp_directories": {
                "sample": {
                    "count": bank.mpbp_sample_count,
                    "unknown_byte": bank.mpbp_sample_directory_unknown,
                    "pointer_u16": bank.mpbp_sample_directory_rel,
                },
                "velocity_curve": {
                    "count": bank.mpbp_velocity_curve_count,
                    "unknown_byte": bank.mpbp_velocity_curve_directory_unknown,
                    "pointer_u16": bank.mpbp_velocity_curve_table_rel,
                },
                "program": {
                    "count": bank.mpbp_program_count,
                    "unknown_byte": bank.mpbp_program_directory_unknown,
                    "pointer_u16": bank.mpbp_program_pointer_rel,
                },
            },
            "trigger_note": int(self.trigger_note_var.get()),
            "trigger_note_name": note_name(int(self.trigger_note_var.get())),
            "sample_count": len(bank.samples),
            "programs_with_splits": len(program_list),
            "samples": samples,
            "programs": program_list,
        }

    def save_program_map_csv(self) -> None:
        if not self.bank:
            return
        default = self.bank.path.with_name(self.bank.path.stem + "_program_map.csv").name
        path = filedialog.asksaveasfilename(title="Save program map CSV", initialfile=default, defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        rows = self._program_rows_for_gui()
        fields = list(rows[0].keys()) if rows else ["empty"]
        try:
            with Path(path).open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader(); writer.writerows(rows)
            self.status_var.set(f"Saved program map to {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Save program map failed", str(exc))

    def save_bank_tree_json(self) -> None:
        if not self.bank:
            return
        default = self.bank.path.with_name(self.bank.path.stem + "_bank_tree.json").name
        path = filedialog.asksaveasfilename(title="Save bank tree JSON", initialfile=default, defaultextension=".json", filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(self._bank_tree_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
            self.status_var.set(f"Saved bank tree to {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Save bank tree failed", str(exc))

    def show_program_map(self) -> None:
        if not self.bank:
            return
        win = tk.Toplevel(self.root)
        win.title("Program / Layer / Split Map")
        win.geometry("980x560")
        cols = ("sample", "alias", "root", "rate", "zone", "fields")
        tree = ttk.Treeview(win, columns=cols, show="tree headings")
        tree.heading("#0", text="Program structure")
        tree.column("#0", width=210)
        labels = {"sample":"Sample", "alias":"Alias", "root":"Root", "rate":"Game Hz", "zone":"Key/Vel Zone", "fields":"Split fields"}
        widths = {"sample":65, "alias":285, "root":80, "rate":80, "zone":120, "fields":210}
        for c in cols:
            tree.heading(c, text=labels[c])
            tree.column(c, width=widths[c], anchor=tk.W if c in ("alias","fields") else tk.CENTER)
        y = ttk.Scrollbar(win, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=y.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        y.pack(side=tk.RIGHT, fill=tk.Y)
        current_prog = None
        current_layer = None
        prog_id = layer_id = ""
        for row in self._program_rows_for_gui():
            pi = row["program"]; li = row["layer"]
            if pi != current_prog:
                current_prog = pi
                prog_id = tree.insert("", tk.END, text=f"Program {pi:02d}", values=("", "", "", "", "", ""), open=False)
                current_layer = None
            if li != current_layer:
                current_layer = li
                layer_id = tree.insert(prog_id, tk.END, text=f"Layer {li}", values=("", "", "", "", "", ""), open=True)
            zone = f"K{row['key_min']}-{row['key_max']} / V{row['vel_min']}-{row['vel_max']}"
            fields = f"level?={row['output_level_candidate_0A']} fx?={row['fx_route_candidate_0B']} encoding={row['encoding_mode_u16_0E']} env={row['env_attack_0x10']}/{row['env_release_0x14']}"
            item = tree.insert(layer_id, tk.END, text=f"Split {row['split']}", values=(row["sample_index"], row["alias"], f"{row['root_key']}/{row['root_note']}", row["wav_header_rate_hz"], zone, fields))
            tree.item(item, open=True)

    def show_selected_layer_fields(self) -> None:
        if not self.bank:
            return
        idx = self.selected_index()
        if idx is None:
            return
        rows = [r for r in self._program_rows_for_gui() if int(r["sample_index"]) == idx]
        s = self.bank.samples[idx]
        entry = self.bank.data[s.entry_abs:s.entry_abs + 0x50].hex(" ")
        lines = [
            f"Sample #{idx}: {s.alias}",
            f"Sample entry rel=0x{s.entry_rel:04X}, abs=0x{s.entry_abs:08X}",
            f"MPBW offset=0x{s.data_offset:06X}, encoded bytes={s.byte_count}, extent+pad={s.original_extent}",
            "",
            "Layer references:",
        ]
        for r in rows:
            lines.append(
                f"  P{int(r['program']):02d}.L{int(r['layer'])}.S{int(r['split'])}: "
                f"root={r['root_key']}/{r['root_note']} exactHz={r['effective_game_rate_exact_hz']} WAVHz={r['wav_header_rate_hz']} "
                f"key={r['key_min']}-{r['key_max']} vel={r['vel_min']}-{r['vel_max']} "
                f"level?={r['output_level_candidate_0A']} fx?={r['fx_route_candidate_0B']} encoding={r['encoding_mode_u16_0E']} env={r['env_attack_0x10']}/{r['env_release_0x14']} "
                f"block_rel=0x{int(r['block_rel']):04X}"
            )
            lines.append(f"    block: {r['block_hex']}")
        if not rows:
            lines.append("  No instrument/layer mapping found.")
        lines += ["", "Raw 0x50 sample entry:", entry]
        win = tk.Toplevel(self.root)
        win.title(f"Sample {idx} layer / hex details")
        win.geometry("900x520")
        body = ttk.Frame(win)
        body.pack(fill=tk.BOTH, expand=True)
        text = tk.Text(body, wrap=tk.WORD)
        yscroll = ttk.Scrollbar(body, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=yscroll.set)
        text.insert("1.0", "\n".join(lines))
        text.configure(state=tk.DISABLED)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)

    def save_deep_audit(self) -> None:
        if not self.bank:
            return
        out = filedialog.askdirectory(title="Choose output folder for deep audit")
        if not out:
            return
        bank_path = self.bank.path
        trigger_note = int(self.trigger_note_var.get())
        def work():
            import mlt_deep_audit
            mlt_deep_audit.run_audit(bank_path, Path(out), trigger_note)
        self._run_background("Deep audit", work)

    def save_samplerate_forensics(self) -> None:
        if not self.bank:
            return
        out = filedialog.askdirectory(title="Choose output folder for sample-rate forensics")
        if not out:
            return
        bank_path = self.bank.path
        trigger_note = int(self.trigger_note_var.get())
        def work():
            import mlt_samplerate_forensics
            mlt_samplerate_forensics.run(bank_path, Path(out), trigger_note)
        self._run_background("Sample-rate forensics", work)

    def save_parameter_forensics(self) -> None:
        if not self.bank:
            return
        out = filedialog.askdirectory(title="Choose output folder for complete parameter forensics")
        if not out:
            return
        bank_path = self.bank.path
        trigger_note = int(self.trigger_note_var.get())
        def work():
            import mlt_parameter_forensics
            mlt_parameter_forensics.run(bank_path, Path(out), trigger_note)
        self._run_background("Complete parameter forensics", work)

    def show_reverse_summary(self) -> None:
        if not self.bank:
            return
        bank = self.bank
        rows = self._program_rows_for_gui()
        looped = sum(1 for s in bank.samples if s.loop_flag)
        empty_programs = bank.mpbp_program_count - len({int(r["program"]) for r in rows})
        active_mltm = [entry for entry in bank.mlt_directory_entries if not entry.is_dummy]
        text = (
            f"MLTM active banks: {len(active_mltm)}\n"
            f"Current MPB BankID: {bank.mlt_directory_entries[bank.mpb_directory_entry_index].bank_id}\n"
            f"Samples: {len(bank.samples)}\n"
            f"Looped samples: {looped}\n"
            f"Layer refs: {len(rows)}\n"
            f"Mapped programs: {len({int(r['program']) for r in rows})}\n"
            f"Approx. empty program slots: {empty_programs}\n\n"
            "Current format notes:\n"
            "- Sample entry 0x50 bytes: DSP header-style sample metadata.\n"
            "- Hierarchy: Program -> Layer descriptor -> Split.\n"
            "- Layer descriptor size 0x10; split size 0x30 bytes.\n"
            "- Stored rate word is twice the physical base rate (base Hz = word / 2.0).\n"
            "- Effective Hz = base Hz * 2^((trigger note - root key) / 12).\n"
            "- Split 0x00 = sample index; split 0x0C = root key.\n"
            "- Split 0x06/0x07 = key-zone candidate; 0x08/0x09 = velocity-zone candidate.\n"
            "- Split 0x0A/0x0B = direct/FX level candidates; 0x10..0x15 = envelope fields.\n"
            "- Unproven value scales remain evidence-ranked in the parameter-forensics report.\n"
            "- Human-readable original names were not found; aliases are generated/editable."
        )
        messagebox.showinfo("Reverse engineering summary", text)

    def open_mlt(self) -> None:
        path = filedialog.askopenfilename(title="Open MLT", filetypes=[("MLT files", "*.mlt"), ("All files", "*.*")])
        if not path:
            return
        try:
            bank = MLTBank(Path(path))
            # Load sidecar aliases when present.
            for candidate in [Path(path).with_suffix(".aliases.csv"), Path(path).with_name(Path(path).stem + "_aliases.csv")]:
                if candidate.exists():
                    bank.load_alias_csv(candidate)
                    break
            self.bank = bank
            self.update_program_filter_values()
            self.refresh_tree()
            self.status_var.set(f"Opened {Path(path).name}: {len(bank.samples)} samples. {bank.no_embedded_names_report()}")
        except Exception as exc:
            messagebox.showerror("Open failed", str(exc))

    def selected_index(self) -> Optional[int]:
        item = self.tree.focus()
        if not item:
            return None
        try:
            return int(self.tree.item(item, "values")[0])
        except Exception:
            return None

    def refresh_tree(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        if not self.bank:
            return
        f = self.filter_var.get().strip().lower()
        for s in self.bank.samples:
            usage = ";".join(s.usage[:5]) + ("…" if len(s.usage) > 5 else "")
            audition_rate = self.bank.audition_sample_rate(s.index, pitch_correct=self.pitch_correct_var.get(), trigger_note=int(self.trigger_note_var.get()))
            values = (
                s.index,
                s.alias,
                s.current_sample_rate,
                audition_rate,
                f"{s.current_sample_count / audition_rate:.3f}" if audition_rate else "0.000",
                "yes" if s.loop_flag else "no",
                s.current_sample_count,
                f"0x{s.data_offset:06X}",
                usage,
                s.replacement_label,
            )
            hay = " ".join(str(v).lower() for v in values)
            if f and f not in hay:
                continue
            view_mode = self.view_filter_var.get()
            if view_mode == "Looped only" and not s.loop_flag:
                continue
            if view_mode == "One-shot only" and s.loop_flag:
                continue
            if view_mode == "Mapped only" and not s.usage:
                continue
            if view_mode == "Unmapped only" and s.usage:
                continue
            if view_mode == "Replaced only" and not s.replacement:
                continue
            if view_mode == "No replacement" and s.replacement:
                continue
            rate_mode = self.rate_filter_var.get()
            if rate_mode != "Any stored rate word":
                try:
                    wanted_rate = int(rate_mode.split()[0])
                except Exception:
                    wanted_rate = None
                if wanted_rate is not None and s.current_sample_rate != wanted_rate:
                    continue
            program_mode = self.program_filter_var.get()
            if program_mode != "All programs":
                program_code = program_mode.split()[0]
                if not any(u.startswith(program_code) for u in s.usage):
                    continue
            self.tree.insert("", tk.END, values=values)
        self.update_details()

    def update_details(self) -> None:
        if not self.bank:
            self.detail_var.set("No MLT loaded.")
            return
        idx = self.selected_index()
        if idx is None:
            self.detail_var.set("No sample selected.")
            return
        s = self.bank.samples[idx]
        trigger_note = int(self.trigger_note_var.get())
        corrected_rate, semis, rate_note = self.bank.rate_correction(idx, trigger_note=trigger_note)
        audition_rate = self.bank.audition_sample_rate(idx, pitch_correct=self.pitch_correct_var.get(), trigger_note=trigger_note)
        loop_report = self.bank.loop_point_report(idx, trigger_note=trigger_note)
        lines = [
            f"Sample #{s.index}",
            f"Alias: {s.alias}",
            f"Stored MPBP rate word: {s.current_sample_rate}",
            f"Base sample rate (stored / 2): {s.current_base_sample_rate_exact:.3f} Hz",
            f"Preview/export rate: {audition_rate} Hz",
            f"Root key(s): {', '.join(str(r) + '/' + note_name(r) for r in s.root_keys) if s.root_keys else '-'}",
            f"Trigger note: {trigger_note}/{note_name(trigger_note)}",
            f"Game pitch shift: {semis:+d} semitone(s) ({rate_note})",
            f"Duration at preview/export rate: {s.current_sample_count / audition_rate:.6f} s" if audition_rate else "Duration: 0 s",
            f"Samples: {s.current_sample_count}",
            f"Loop: {'yes' if s.loop_flag else 'no'}",
            f"Loop start addr/sample: {loop_report['loop_start_addr_hex']} / {loop_report['loop_start_sample']}" if loop_report else "Loop start addr/sample: -",
            f"Loop end addr/sample inclusive: {loop_report['loop_end_addr_hex']} / {loop_report['loop_end_sample_inclusive']}" if loop_report else "Loop end addr/sample inclusive: -",
            f"Current loop region [start, end): {self.bank.loop_points_samples(idx) if s.loop_flag else '-'}",
            f"Original MPBW offset: 0x{s.data_offset:06X}",
            f"Format: fmt={s.fmt}, type=0x{s.type_byte:02X}",
            f"Usage: {', '.join(s.usage) if s.usage else 'not mapped'}",
        ]
        if s.replacement:
            lines += [
                "",
                f"Replacement: {s.replacement.wav_path.name}",
                f"Source WAV rate: {s.replacement.source_wav_rate} Hz",
                f"Encoded content rate: {s.replacement.content_sample_rate} Hz",
                f"Stored rate word written: {s.replacement.sample_rate} (base {s.replacement.sample_rate / MLT_RATE_WORD_DIVISOR:.3f} Hz)",
                f"Replacement loop start sample: {s.replacement.loop_start_sample if s.loop_flag else '-'}",
                f"Encoded bytes: {len(s.replacement.encoded_payload)}",
                f"DSP encode-back RMS / peak error: {s.replacement.encode_rms_error:.1f} / {s.replacement.encode_peak_error}",
            ]
        self.detail_var.set("\n".join(lines))

    def _run_background(self, title: str, func) -> None:
        """Run file and codec work off the UI thread."""
        if self._background_active:
            self.status_var.set("Another background task is still running.")
            return

        self._background_active = True
        self.status_var.set(f"{title}...")

        def finish_ok() -> None:
            self._background_active = False
            if not self._closing:
                self.status_var.set(f"{title} done.")

        def finish_error(message: str) -> None:
            self._background_active = False
            if not self._closing:
                self.status_var.set(f"{title} failed.")
                messagebox.showerror(title, message)

        def worker() -> None:
            try:
                func()
            except Exception as exc:
                self._post_ui(finish_error, str(exc))
                return
            self._post_ui(finish_ok)

        threading.Thread(target=worker, daemon=True, name=f"MLT-{title}").start()

    def preview_selected(self) -> None:
        if not self.bank:
            return
        idx = self.selected_index()
        if idx is None:
            return
        try:
            self.stop_preview(silent=True)
            s = self.bank.samples[idx]
            pcm = self.bank.decode_sample(idx)
            tmp = Path(tempfile.gettempdir()) / f"mlt_preview_{os.getpid()}_{idx:03d}.wav"
            write_wav(tmp, pcm, self.bank.audition_sample_rate(idx, pitch_correct=self.pitch_correct_var.get(), trigger_note=int(self.trigger_note_var.get())))
            self.current_temp_wav = tmp
            self._play_wav(tmp)
            self.status_var.set(f"Previewing sample {idx}: {s.alias}")
        except Exception as exc:
            messagebox.showerror("Preview failed", str(exc))

    def preview_loop_selected(self) -> None:
        if not self.bank:
            return
        idx = self.selected_index()
        if idx is None:
            return
        try:
            s = self.bank.samples[idx]
            points = self.bank.loop_points_samples(idx)
            if not points:
                messagebox.showinfo("Loop preview", "This sample has no loop flag / loop points.")
                return
            self.stop_preview(silent=True)
            start, end_excl = points
            pcm = self.bank.decode_sample(idx)
            preview_seconds = int(self.loop_preview_seconds_var.get() or 20)
            self._play_generation += 1
            generation = self._play_generation

            # Build one WAV so playback does not pause at the loop handoff.
            loop_pcm = self.bank.build_loop_preview_pcm(
                idx,
                preview_seconds,
                pitch_correct=self.pitch_correct_var.get(),
                trigger_note=int(self.trigger_note_var.get()),
                declick=self.loop_declick_var.get(),
                crossfade_ms=float(self.loop_crossfade_ms_var.get() or 0.0),
                zero_cross=self.loop_zero_cross_var.get(),
                trim_silence=self.loop_trim_silence_var.get(),
            )
            diag = self.bank.loop_preview_diagnostics(
                idx,
                preview_seconds,
                pitch_correct=self.pitch_correct_var.get(),
                trigger_note=int(self.trigger_note_var.get()),
                declick=self.loop_declick_var.get(),
                crossfade_ms=float(self.loop_crossfade_ms_var.get() or 0.0),
                zero_cross=self.loop_zero_cross_var.get(),
                trim_silence=self.loop_trim_silence_var.get(),
            )
            tmp = Path(tempfile.gettempdir()) / f"mlt_preview_{os.getpid()}_{idx:03d}_gapless_loop_{preview_seconds}s.wav"
            write_wav(tmp, loop_pcm, self.bank.audition_sample_rate(idx, pitch_correct=self.pitch_correct_var.get(), trigger_note=int(self.trigger_note_var.get())))
            self.current_temp_wav = tmp
            self._play_wav(tmp)
            self.status_var.set(
                f"Gapless loop preview sample {idx}: start {diag.get('preview_start', start)}, "
                f"loop {int(diag.get('preview_end_exclusive', end_excl)) - int(diag.get('preview_start', start))} samples, "
                f"crossfade {diag.get('crossfade_samples', 0)} samples, {preview_seconds}s."
            )
        except Exception as exc:
            messagebox.showerror("Loop preview failed", str(exc))

    def _play_wav(self, wav_path: Path) -> None:
        system = platform.system().lower()
        if system == "windows":
            import winsound
            winsound.PlaySound(str(wav_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
            return
        if system == "darwin":
            self._preview_process = subprocess.Popen(["afplay", str(wav_path)])
            return
        opener = shutil.which("aplay") or shutil.which("xdg-open")
        if opener:
            self._preview_process = subprocess.Popen([opener, str(wav_path)])
        else:
            messagebox.showinfo("Preview", f"WAV written to:\n{wav_path}")

    def stop_preview(self, silent: bool = False) -> None:
        self._play_generation += 1
        if platform.system().lower() == "windows":
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        process = self._preview_process
        self._preview_process = None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        if not silent:
            self.status_var.set("Preview stopped.")

    def export_selected(self) -> None:
        if not self.bank:
            return
        idx = self.selected_index()
        if idx is None:
            return
        s = self.bank.samples[idx]
        safe_alias = "".join(c if c.isalnum() or c in "._-" else "_" for c in s.alias)
        audition_rate = self.bank.audition_sample_rate(idx, pitch_correct=self.pitch_correct_var.get(), trigger_note=int(self.trigger_note_var.get()))
        default = f"{idx:03d}_{safe_alias}_{audition_rate}Hz.wav"
        path = filedialog.asksaveasfilename(title="Export selected WAV", initialfile=default, defaultextension=".wav", filetypes=[("WAV", "*.wav")])
        if not path:
            return
        try:
            self.bank.export_sample(idx, Path(path), pitch_correct=self.pitch_correct_var.get(), trigger_note=int(self.trigger_note_var.get()))
            self.status_var.set(f"Exported {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))

    def export_loop_preview_selected(self) -> None:
        if not self.bank:
            return
        idx = self.selected_index()
        if idx is None:
            return
        s = self.bank.samples[idx]
        if not self.bank.loop_points_samples(idx):
            messagebox.showinfo("Export loop preview", "This sample has no loop flag / loop points.")
            return
        safe_alias = "".join(c if c.isalnum() or c in "._-" else "_" for c in s.alias)
        seconds = int(self.loop_preview_seconds_var.get() or 20)
        audition_rate = self.bank.audition_sample_rate(idx, pitch_correct=self.pitch_correct_var.get(), trigger_note=int(self.trigger_note_var.get()))
        default = f"{idx:03d}_{safe_alias}_{audition_rate}Hz_loopPreview_{seconds}s.wav"
        path = filedialog.asksaveasfilename(
            title="Export loop preview WAV",
            initialfile=default,
            defaultextension=".wav",
            filetypes=[("WAV", "*.wav")],
        )
        if not path:
            return
        try:
            self.bank.export_loop_preview(
                idx, Path(path), seconds,
                pitch_correct=self.pitch_correct_var.get(),
                trigger_note=int(self.trigger_note_var.get()),
                declick=self.loop_declick_var.get(),
                crossfade_ms=float(self.loop_crossfade_ms_var.get() or 0.0),
                zero_cross=self.loop_zero_cross_var.get(),
                trim_silence=self.loop_trim_silence_var.get(),
            )
            self.status_var.set(f"Exported loop preview {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Export loop preview failed", str(exc))

    def export_all(self) -> None:
        if not self.bank:
            return
        out = filedialog.askdirectory(title="Export all WAV files")
        if not out:
            return
        bank = self.bank
        pitch_correct = bool(self.pitch_correct_var.get())
        trigger_note = int(self.trigger_note_var.get())
        self._run_background("Export all", lambda: bank.export_all(Path(out), pitch_correct=pitch_correct, trigger_note=trigger_note))

    def replace_selected(self) -> None:
        if not self.bank:
            return
        idx = self.selected_index()
        if idx is None:
            return
        path = filedialog.askopenfilename(title="Choose replacement WAV", filetypes=[("WAV", "*.wav"), ("All files", "*.*")])
        if not path:
            return

        bank = self.bank
        preserve_bank_rate = bool(self.preserve_bank_rate_var.get())
        pitch_correct = bool(self.pitch_correct_var.get())
        trigger_note = int(self.trigger_note_var.get())

        def work():
            bank.replace_from_wav(
                idx,
                Path(path),
                preserve_loop_ratio=True,
                preserve_bank_rate=preserve_bank_rate,
                auto_resample_to_audition=True,
                pitch_correct=pitch_correct,
                trigger_note=trigger_note,
            )
            self._post_ui(self.refresh_tree)
            self._post_ui(self.update_details)

        self._run_background(f"Encoding replacement for sample {idx}", work)

    def clear_replacement(self) -> None:
        if not self.bank:
            return
        idx = self.selected_index()
        if idx is None:
            return
        self.bank.clear_replacement(idx)
        self.refresh_tree()
        self.status_var.set(f"Cleared replacement for sample {idx}")

    def save_as(self) -> None:
        if not self.bank:
            return
        default = self.bank.path.with_name(self.bank.path.stem + "_repacked.mlt").name
        path = filedialog.asksaveasfilename(title="Save repacked MLT", initialfile=default, defaultextension=".mlt", filetypes=[("MLT", "*.mlt"), ("All files", "*.*")])
        if not path:
            return
        self._run_background("Save repacked MLT", lambda: self.bank.save_as(Path(path)))

    def save_aliases(self) -> None:
        if not self.bank:
            return
        default = self.bank.path.with_name(self.bank.path.stem + "_aliases.csv").name
        path = filedialog.asksaveasfilename(title="Save aliases CSV", initialfile=default, defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            self.bank.write_alias_csv(Path(path))
            self.status_var.set(f"Saved aliases to {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Save aliases failed", str(exc))

    def save_loop_report(self) -> None:
        if not self.bank:
            return
        default = self.bank.path.with_name(self.bank.path.stem + "_loop_points.csv").name
        path = filedialog.asksaveasfilename(title="Save loop-point report CSV", initialfile=default, defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            self.bank.write_loop_report_csv(Path(path))
            self.status_var.set(f"Saved loop-point report to {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Save loop report failed", str(exc))

    def rename_alias(self) -> None:
        if not self.bank:
            return
        idx = self.selected_index()
        if idx is None:
            return
        s = self.bank.samples[idx]
        win = tk.Toplevel(self.root)
        win.title(f"Rename sample {idx}")
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text="Alias:").pack(padx=10, pady=(10, 2), anchor=tk.W)
        var = tk.StringVar(value=s.alias)
        ent = ttk.Entry(win, textvariable=var, width=58)
        ent.pack(padx=10, pady=4)
        ent.focus_set()

        def ok():
            new_alias = var.get().strip()
            if new_alias:
                s.alias = new_alias
                self.refresh_tree()
            win.destroy()

        btns = ttk.Frame(win)
        btns.pack(padx=10, pady=10, fill=tk.X)
        ttk.Button(btns, text="OK", command=ok).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side=tk.RIGHT)
        win.bind("<Return>", lambda _e: ok())
        win.bind("<Escape>", lambda _e: win.destroy())



    def _build_context_menu(self) -> None:
        self.context_menu = tk.Menu(self.root, tearoff=False)
        self.context_menu.add_command(label="Preview", command=self.preview_selected)
        self.context_menu.add_command(label="Preview Loop Mode", command=self.preview_loop_selected)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Export WAV...", command=self.export_selected)
        self.context_menu.add_command(label="Export Loop Preview WAV...", command=self.export_loop_preview_selected)
        self.context_menu.add_command(label="Export Raw DSP Payload...", command=self.export_selected_raw_payload)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Replace From WAV...", command=self.replace_selected)
        self.context_menu.add_command(label="Clear Replacement", command=self.clear_replacement)
        self.context_menu.add_command(label="Rename Alias...", command=self.rename_alias)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Layer / Hex Details...", command=self.show_selected_layer_fields)

    def show_context_menu(self, event) -> None:
        row_id = self.tree.identify_row(event.y)
        if row_id:
            self.tree.selection_set(row_id)
            self.tree.focus(row_id)
            self.update_details()
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def update_program_filter_values(self) -> None:
        values = ["All programs"]
        if self.bank:
            programs = sorted({u.split(".")[0] for s in self.bank.samples for u in s.usage})
            values += [f"{p} only" for p in programs]
        try:
            self.program_combo.configure(values=tuple(values))
        except Exception:
            pass
        if self.program_filter_var.get() not in values:
            self.program_filter_var.set("All programs")

    def export_selected_raw_payload(self) -> None:
        if not self.bank:
            return
        idx = self.selected_index()
        if idx is None:
            return
        s = self.bank.samples[idx]
        default = f"{idx:03d}_{self.bank.safe_alias(idx)}_DSPADPCM.bin"
        path = filedialog.asksaveasfilename(title="Export raw DSP-ADPCM payload", initialfile=default, defaultextension=".bin", filetypes=[("Binary", "*.bin"), ("All files", "*.*")])
        if not path:
            return
        try:
            self.bank.export_raw_payload(idx, Path(path), include_padding=False)
            self.status_var.set(f"Exported raw payload for sample {idx} to {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Raw export failed", str(exc))

    def export_all_raw_payloads(self) -> None:
        if not self.bank:
            return
        out = filedialog.askdirectory(title="Export all raw DSP-ADPCM payloads")
        if not out:
            return
        self._run_background("Export raw payloads", lambda: self.bank.export_all_raw_payloads(Path(out), include_padding=False))

    def save_replacement_manifest_template(self) -> None:
        if not self.bank:
            return
        default = self.bank.path.with_name(self.bank.path.stem + "_replacement_manifest_template.csv").name
        path = filedialog.asksaveasfilename(title="Save replacement manifest template CSV", initialfile=default, defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            self.bank.write_replacement_manifest_template(Path(path), trigger_note=int(self.trigger_note_var.get()))
            self.status_var.set(f"Saved replacement manifest template to {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Manifest template failed", str(exc))

    def validate_repack_plan_gui(self) -> None:
        if not self.bank:
            return
        rows = self.bank.validate_repack_plan(trigger_note=int(self.trigger_note_var.get()))
        errors = sum(1 for r in rows if r.get("area") != "summary" and r.get("severity") == "error")
        warnings = sum(1 for r in rows if r.get("area") != "summary" and r.get("severity") == "warning")
        win = tk.Toplevel(self.root)
        win.title(f"Repack validation: {errors} error(s), {warnings} warning(s)")
        win.geometry("980x560")
        cols = ("severity", "area", "index", "message", "detail")
        tree = ttk.Treeview(win, columns=cols, show="headings")
        widths = {"severity": 80, "area": 110, "index": 70, "message": 260, "detail": 430}
        for c in cols:
            tree.heading(c, text=c)
            tree.column(c, width=widths[c], anchor=tk.W if c in ("message", "detail") else tk.CENTER)
        y = ttk.Scrollbar(win, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=y.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        y.pack(side=tk.RIGHT, fill=tk.Y)
        for r in rows:
            if r.get("severity") in ("error", "warning", "ok") or r.get("area") in ("summary", "repack", "file"):
                tree.insert("", tk.END, values=(r.get("severity"), r.get("area"), r.get("index"), r.get("message"), r.get("detail")))
        self.status_var.set(f"Validation done: {errors} error(s), {warnings} warning(s)")

    def save_validation_report_csv(self) -> None:
        if not self.bank:
            return
        default = self.bank.path.with_name(self.bank.path.stem + "_validation.csv").name
        path = filedialog.asksaveasfilename(title="Save validation report CSV", initialfile=default, defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            self.bank.write_validation_report_csv(Path(path), trigger_note=int(self.trigger_note_var.get()))
            self.status_var.set(f"Saved validation report to {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Validation report failed", str(exc))

    def batch_replace_from_folder_gui(self) -> None:
        if not self.bank:
            return
        folder = filedialog.askdirectory(title="Choose folder containing replacement WAVs")
        if not folder:
            return

        bank = self.bank
        preserve_bank_rate = bool(self.preserve_bank_rate_var.get())
        pitch_correct = bool(self.pitch_correct_var.get())
        trigger_note = int(self.trigger_note_var.get())

        def work():
            rows = bank.batch_replace_from_folder(
                Path(folder),
                preserve_bank_rate=preserve_bank_rate,
                pitch_correct=pitch_correct,
                trigger_note=trigger_note,
            )
            ok = sum(1 for r in rows if r.get("status") == "ok")
            err = sum(1 for r in rows if r.get("status") == "error")
            report_path = Path(folder) / "mlt_batch_replace_report.csv"
            fields = sorted({k for r in rows for k in r.keys()}) or ["empty"]
            with report_path.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader(); writer.writerows(rows)
            self._post_ui(self.refresh_tree)
            self._post_ui(self.update_details)
            self._post_ui(self.status_var.set, f"Batch replace finished: {ok} ok, {err} error(s). Report: {report_path.name}")
        self._run_background("Batch replace", work)

    def save_loop_preview_seam_report_csv(self) -> None:
        if not self.bank:
            return
        default = self.bank.path.with_name(self.bank.path.stem + "_loop_preview_seam.csv").name
        path = filedialog.asksaveasfilename(
            title="Save loop preview seam report CSV",
            initialfile=default,
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
        )
        if not path:
            return
        try:
            self.bank.write_loop_preview_seam_report_csv(
                Path(path),
                int(self.loop_preview_seconds_var.get() or 20),
                pitch_correct=self.pitch_correct_var.get(),
                trigger_note=int(self.trigger_note_var.get()),
                declick=self.loop_declick_var.get(),
                crossfade_ms=float(self.loop_crossfade_ms_var.get() or 0.0),
                zero_cross=self.loop_zero_cross_var.get(),
                trim_silence=self.loop_trim_silence_var.get(),
            )
            self.status_var.set(f"Saved loop seam report to {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Save loop seam report failed", str(exc))

    def save_project_json(self) -> None:
        if not self.bank:
            return
        default = self.bank.path.with_name(self.bank.path.stem + "_mlt_project.json").name
        path = filedialog.asksaveasfilename(title="Save editor project JSON", initialfile=default, defaultextension=".json", filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            data = {
                "tool": "MLT Soundbank Explorer project",
                "mlt_path": str(self.bank.path),
                "settings": {
                    "pitch_correct": bool(self.pitch_correct_var.get()),
                    "trigger_note": int(self.trigger_note_var.get()),
                    "preserve_bank_rate": bool(self.preserve_bank_rate_var.get()),
                    "loop_preview_seconds": int(self.loop_preview_seconds_var.get()),
                    "loop_declick": bool(self.loop_declick_var.get()),
                    "loop_zero_cross": bool(self.loop_zero_cross_var.get()),
                    "loop_trim_silence": bool(self.loop_trim_silence_var.get()),
                    "loop_crossfade_ms": float(self.loop_crossfade_ms_var.get() or 0.0),
                    "clean_audition": bool(self.clean_audition_var.get()),
                    "fixed_render_rate": bool(self.fixed_render_rate_var.get()),
                    "render_rate": int(self.render_rate_var.get()),
                    "dc_filter": bool(self.dc_filter_var.get()),
                    "decrackle_filter": bool(self.decrackle_filter_var.get()),
                    "decrackle_strength": str(self.decrackle_strength_var.get()),
                    "limiter": bool(self.limiter_var.get()),
                    "edge_fade": bool(self.edge_fade_var.get()),
                },
                "samples": [
                    {
                        "index": s.index,
                        "alias": s.alias,
                        "replacement_wav": str(s.replacement.wav_path) if s.replacement else "",
                    }
                    for s in self.bank.samples
                ],
            }
            Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            self.status_var.set(f"Saved editor project to {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Save project failed", str(exc))

    def load_project_json(self) -> None:
        path = filedialog.askopenfilename(title="Load editor project JSON", filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            mlt_path = Path(data.get("mlt_path", ""))
            if self.bank is None:
                if not mlt_path.exists():
                    chosen = filedialog.askopenfilename(title="Project MLT missing; choose MLT", filetypes=[("MLT", "*.mlt"), ("All files", "*.*")])
                    if not chosen:
                        return
                    mlt_path = Path(chosen)
                self.bank = MLTBank(mlt_path)
            settings = data.get("settings", {})
            if "pitch_correct" in settings:
                self.pitch_correct_var.set(bool(settings["pitch_correct"]))
            if "trigger_note" in settings:
                self.trigger_note_var.set(int(settings["trigger_note"]))
            if "preserve_bank_rate" in settings:
                self.preserve_bank_rate_var.set(bool(settings["preserve_bank_rate"]))
            if "loop_preview_seconds" in settings:
                self.loop_preview_seconds_var.set(int(settings["loop_preview_seconds"]))
            if "loop_declick" in settings:
                self.loop_declick_var.set(bool(settings["loop_declick"]))
            if "loop_zero_cross" in settings:
                self.loop_zero_cross_var.set(bool(settings["loop_zero_cross"]))
            if "loop_trim_silence" in settings:
                self.loop_trim_silence_var.set(bool(settings["loop_trim_silence"]))
            if "loop_crossfade_ms" in settings:
                self.loop_crossfade_ms_var.set(float(settings["loop_crossfade_ms"]))
            if "clean_audition" in settings:
                self.clean_audition_var.set(bool(settings["clean_audition"]))
            if "fixed_render_rate" in settings:
                self.fixed_render_rate_var.set(bool(settings["fixed_render_rate"]))
            if "render_rate" in settings:
                self.render_rate_var.set(int(settings["render_rate"]))
            if "dc_filter" in settings:
                self.dc_filter_var.set(bool(settings["dc_filter"]))
            if "decrackle_filter" in settings:
                self.decrackle_filter_var.set(bool(settings["decrackle_filter"]))
            if "decrackle_strength" in settings:
                self.decrackle_strength_var.set(str(settings["decrackle_strength"]))
            if "limiter" in settings:
                self.limiter_var.set(bool(settings["limiter"]))
            if "edge_fade" in settings:
                self.edge_fade_var.set(bool(settings["edge_fade"]))
            by_idx = {s.index: s for s in self.bank.samples}
            samples = data.get("samples", [])
            for row in samples:
                try:
                    idx = int(row.get("index"))
                except Exception:
                    continue
                if idx in by_idx and row.get("alias"):
                    by_idx[idx].alias = str(row.get("alias"))

            bank = self.bank
            preserve_bank_rate = bool(self.preserve_bank_rate_var.get())
            pitch_correct = bool(self.pitch_correct_var.get())
            trigger_note = int(self.trigger_note_var.get())
            if not pitch_correct:
                self.pitch_preset_var.set("Base stored/2 rate")
            elif trigger_note == 60:
                self.pitch_preset_var.set("Game C4 / 60")
            elif trigger_note == 48:
                self.pitch_preset_var.set("Game C3 / 48")
            else:
                self.pitch_preset_var.set("Custom")

            def work():
                applied = 0; missing = 0; failed = 0
                for row in samples:
                    try:
                        idx = int(row.get("index"))
                    except Exception:
                        continue
                    rep = str(row.get("replacement_wav") or "").strip()
                    if not rep:
                        continue
                    rp = Path(rep)
                    if not rp.exists():
                        missing += 1; continue
                    try:
                        bank.replace_from_wav(
                            idx,
                            rp,
                            preserve_loop_ratio=True,
                            preserve_bank_rate=preserve_bank_rate,
                            auto_resample_to_audition=True,
                            pitch_correct=pitch_correct,
                            trigger_note=trigger_note,
                        )
                        applied += 1
                    except Exception:
                        failed += 1
                self._post_ui(self.update_program_filter_values)
                self._post_ui(self.refresh_tree)
                self._post_ui(self.status_var.set, f"Loaded project: aliases applied, replacements {applied} applied, {missing} missing, {failed} failed.")
            self._run_background("Load project", work)
        except Exception as exc:
            messagebox.showerror("Load project failed", str(exc))


