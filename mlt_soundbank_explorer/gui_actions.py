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

class GUIActionsMixin:
    def open_mlt(self) -> None:
        path = filedialog.askopenfilename(title="Open Soundbank", filetypes=[("Supported soundbanks", "*.mlt *.mpb *.mdt"), ("All files", "*.*")])
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
        source_suffix = self.bank.path.suffix.lower() or ".mlt"
        default_ext = source_suffix
        format_label = {
            ".mpb": "MPB",
            ".mdt": "MDT",
            ".mlt": "MLT",
        }.get(source_suffix, source_suffix.lstrip(".").upper() or "Soundbank")
        default = self.bank.path.with_name(self.bank.path.stem + "_repacked" + default_ext).name
        path = filedialog.asksaveasfilename(
            title=f"Save repacked {format_label}",
            initialfile=default,
            defaultextension=default_ext,
            filetypes=[(format_label, f"*{default_ext}"), ("All files", "*.*")],
        )
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

