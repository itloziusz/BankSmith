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
from .adapters import open_editable_bank
from .branding import PROJECT_TOOL_NAME

class GUIProjectMixin:
    def save_project_json(self) -> None:
        if not self.bank:
            return
        default = self.bank.path.with_name(self.bank.path.stem + "_banksmith_project.json").name
        path = filedialog.asksaveasfilename(title="Save editor project JSON", initialfile=default, defaultextension=".json", filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            data = {
                "tool": PROJECT_TOOL_NAME,
                "format_version": 2,
                "soundbank_path": str(self.bank.path),
                "mlt_path": str(self.bank.path),  # legacy compatibility
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
            soundbank_path = Path(data.get("soundbank_path") or data.get("mlt_path", ""))
            if self.bank is None:
                if not soundbank_path.exists():
                    chosen = filedialog.askopenfilename(
                        title="Project soundbank missing; choose source file",
                        filetypes=[
                            ("Supported soundbanks", "*.mlt *.mpb *.mdt"),
                            ("All files", "*.*"),
                        ],
                    )
                    if not chosen:
                        return
                    soundbank_path = Path(chosen)
                self.bank = open_editable_bank(soundbank_path)
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


