from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Dict

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .core import *
from .audio import *
from .render_audio import *


class GUIRenderMixin:
    def __init__(self, root: tk.Tk) -> None:
        self.clean_audition_var = tk.BooleanVar(master=root, value=True)
        self.fixed_render_rate_var = tk.BooleanVar(master=root, value=True)
        self.render_rate_var = tk.IntVar(master=root, value=DEFAULT_CLEAN_RENDER_RATE)
        self.dc_filter_var = tk.BooleanVar(master=root, value=True)
        self.decrackle_filter_var = tk.BooleanVar(master=root, value=True)
        self.decrackle_strength_var = tk.StringVar(master=root, value="Light")
        self.limiter_var = tk.BooleanVar(master=root, value=True)
        self.edge_fade_var = tk.BooleanVar(master=root, value=True)
        super().__init__(root)
        self.root.geometry("1220x760")

    def _build_ui(self) -> None:
        super()._build_ui()
        # Add the audio cleanup menu.
        try:
            menubar = self.root.nametowidget(self.root.cget("menu"))
            clean_menu = tk.Menu(menubar, tearoff=False)
            clean_menu.add_checkbutton(label="Clean audition/render WAVs", variable=self.clean_audition_var)
            clean_menu.add_checkbutton(label="Render to fixed 44.1/48 kHz", variable=self.fixed_render_rate_var)
            clean_menu.add_separator()
            for rate in CLEAN_RENDER_RATES:
                clean_menu.add_radiobutton(label=f"Render rate: {rate} Hz", variable=self.render_rate_var, value=rate)
            clean_menu.add_separator()
            clean_menu.add_checkbutton(label="Remove DC offset", variable=self.dc_filter_var)
            clean_menu.add_checkbutton(label="De-crackle isolated spikes", variable=self.decrackle_filter_var)
            for value in ("Light", "Medium", "Strong"):
                clean_menu.add_radiobutton(label=f"De-crackle strength: {value}", variable=self.decrackle_strength_var, value=value)
            clean_menu.add_checkbutton(label="Safety limiter / headroom", variable=self.limiter_var)
            clean_menu.add_checkbutton(label="Tiny edge fade", variable=self.edge_fade_var)
            clean_menu.add_separator()
            clean_menu.add_command(label="Export All Clean WAV...", command=self.export_all_clean)
            clean_menu.add_command(label="Save Audio Quality Report CSV...", command=self.save_audio_quality_report_csv)
            menubar.add_cascade(label="Audio Clean", menu=clean_menu)
        except Exception:
            pass
        # Add quick cleanup controls above the status line.
        try:
            strip = ttk.Frame(self.root, padding=(8, 2))
            strip.pack(side=tk.BOTTOM, fill=tk.X, before=self.root.children.get('!label'))
        except Exception:
            strip = ttk.Frame(self.root, padding=(8, 2))
            strip.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Checkbutton(strip, text="Clean preview/export", variable=self.clean_audition_var).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Checkbutton(strip, text="Fixed render Hz", variable=self.fixed_render_rate_var).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Combobox(strip, textvariable=self.render_rate_var, state="readonly", width=8, values=CLEAN_RENDER_RATES).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(strip, text="Limiter", variable=self.limiter_var).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Checkbutton(strip, text="De-crackle", variable=self.decrackle_filter_var).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Combobox(strip, textvariable=self.decrackle_strength_var, state="readonly", width=8, values=("Light", "Medium", "Strong")).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Button(strip, text="Audio Quality Report", command=self.save_audio_quality_report_csv).pack(side=tk.LEFT, padx=(0, 6))
    
    
    def self._clean_render_options() -> Dict[str, object]:
        return {
            "clean": bool(self.clean_audition_var.get()),
            "fixed_output_rate": bool(self.fixed_render_rate_var.get()),
            "output_rate": int(self.render_rate_var.get() or DEFAULT_CLEAN_RENDER_RATE),
            "dc_filter": bool(self.dc_filter_var.get()),
            "decrackle": bool(self.decrackle_filter_var.get()),
            "decrackle_strength": str(self.decrackle_strength_var.get() or "Light"),
            "limiter": bool(self.limiter_var.get()),
            "edge_fade": bool(self.edge_fade_var.get()),
        }
    
    
    def preview_selected(self) -> None:
        if not self.bank:
            return
        idx = self.selected_index()
        if idx is None:
            return
        try:
            self.stop_preview(silent=True)
            s = self.bank.samples[idx]
            pcm, rate, diag = self.bank.render_sample_for_wav(
                idx,
                pitch_correct=self.pitch_correct_var.get(),
                trigger_note=int(self.trigger_note_var.get()),
                **self._clean_render_options(),
            )
            tmp = Path(tempfile.gettempdir()) / f"mlt_preview_{os.getpid()}_{idx:03d}.wav"
            write_wav(tmp, pcm, rate)
            self.current_temp_wav = tmp
            self._play_wav(tmp)
            self.status_var.set(f"Previewing sample {idx}: {s.alias} | {rate} Hz | peak {diag.get('post_peak', diag.get('peak', 0))}")
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
            if not self.bank.loop_points_samples(idx):
                messagebox.showinfo("Loop preview", "This sample has no loop flag / loop points.")
                return
            self.stop_preview(silent=True)
            seconds = int(self.loop_preview_seconds_var.get() or 20)
            pcm, rate, diag = self.bank.render_loop_preview_for_wav(
                idx,
                preview_seconds=seconds,
                pitch_correct=self.pitch_correct_var.get(),
                trigger_note=int(self.trigger_note_var.get()),
                loop_declick=self.loop_declick_var.get(),
                crossfade_ms=float(self.loop_crossfade_ms_var.get() or 0.0),
                zero_cross=self.loop_zero_cross_var.get(),
                trim_silence=self.loop_trim_silence_var.get(),
                **self._clean_render_options(),
            )
            tmp = Path(tempfile.gettempdir()) / f"mlt_preview_{os.getpid()}_{idx:03d}_loop_clean_{seconds}s.wav"
            write_wav(tmp, pcm, rate)
            self.current_temp_wav = tmp
            self._play_wav(tmp)
            self.status_var.set(f"Loop preview sample {idx}: {s.alias} | {rate} Hz | clean={bool(self.clean_audition_var.get())} | {seconds}s")
        except Exception as exc:
            messagebox.showerror("Loop preview failed", str(exc))
    
    
    def export_selected(self) -> None:
        if not self.bank:
            return
        idx = self.selected_index()
        if idx is None:
            return
        s = self.bank.samples[idx]
        # Include the final WAV rate in the filename.
        _pcm, rate, _diag = self.bank.render_sample_for_wav(
            idx,
            pitch_correct=self.pitch_correct_var.get(),
            trigger_note=int(self.trigger_note_var.get()),
            **self._clean_render_options(),
        )
        default = f"{idx:03d}_{self.bank.safe_alias(idx)}_{rate}Hz_clean.wav"
        path = filedialog.asksaveasfilename(title="Export selected WAV", initialfile=default, defaultextension=".wav", filetypes=[("WAV", "*.wav")])
        if not path:
            return
        try:
            self.bank.export_sample_rendered(
                idx,
                Path(path),
                pitch_correct=self.pitch_correct_var.get(),
                trigger_note=int(self.trigger_note_var.get()),
                **self._clean_render_options(),
            )
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
        seconds = int(self.loop_preview_seconds_var.get() or 20)
        _pcm, rate, _diag = self.bank.render_loop_preview_for_wav(
            idx,
            preview_seconds=seconds,
            pitch_correct=self.pitch_correct_var.get(),
            trigger_note=int(self.trigger_note_var.get()),
            loop_declick=self.loop_declick_var.get(),
            crossfade_ms=float(self.loop_crossfade_ms_var.get() or 0.0),
            zero_cross=self.loop_zero_cross_var.get(),
            trim_silence=self.loop_trim_silence_var.get(),
            **self._clean_render_options(),
        )
        default = f"{idx:03d}_{self.bank.safe_alias(idx)}_{rate}Hz_loopPreview_{seconds}s_clean.wav"
        path = filedialog.asksaveasfilename(title="Export loop preview WAV", initialfile=default, defaultextension=".wav", filetypes=[("WAV", "*.wav")])
        if not path:
            return
        try:
            self.bank.export_loop_preview_rendered(
                idx,
                Path(path),
                preview_seconds=seconds,
                pitch_correct=self.pitch_correct_var.get(),
                trigger_note=int(self.trigger_note_var.get()),
                loop_declick=self.loop_declick_var.get(),
                crossfade_ms=float(self.loop_crossfade_ms_var.get() or 0.0),
                zero_cross=self.loop_zero_cross_var.get(),
                trim_silence=self.loop_trim_silence_var.get(),
                **self._clean_render_options(),
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
        clean_kwargs = self._clean_render_options()
        self._run_background("Export all clean", lambda: bank.export_all_rendered(
            Path(out),
            pitch_correct=pitch_correct,
            trigger_note=trigger_note,
            **clean_kwargs,
        ))
    
    
    def export_all_clean(self) -> None:
        self.export_all()
    
    
    def save_audio_quality_report_csv(self) -> None:
        if not self.bank:
            return
        default = self.bank.path.with_name(self.bank.path.stem + "_audio_quality.csv").name
        path = filedialog.asksaveasfilename(title="Save audio quality report CSV", initialfile=default, defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            self.bank.write_audio_quality_report_csv(
                Path(path),
                pitch_correct=self.pitch_correct_var.get(),
                trigger_note=int(self.trigger_note_var.get()),
                **self._clean_render_options(),
            )
            self.status_var.set(f"Saved audio quality report to {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Audio quality report failed", str(exc))
