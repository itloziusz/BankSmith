#!/usr/bin/env python3
from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .gui import MLTExplorerApp
from .adapters import open_editable_bank
from .formats import extract_mdt_blocks, format_summary_text, inspect_soundbank
from .core import note_name


class MultiFormatExplorerApp(MLTExplorerApp):

    def _is_dreamcast_bank(self) -> bool:
        return bool(self.bank and getattr(self.bank, "family", "") in (
            "Dreamcast SMPB", "Dreamcast SMLT", "Sonic Shuffle MDT"
        ))

    def update_details(self) -> None:
        if not self._is_dreamcast_bank():
            return super().update_details()
        idx = self.selected_index()
        if idx is None:
            self.detail_var.set("No sample selected.")
            return
        s = self.bank.samples[idx]
        trigger_note = int(self.trigger_note_var.get())
        rate, semis, rate_note = self.bank.rate_correction(idx, trigger_note=trigger_note)
        audition_rate = self.bank.audition_sample_rate(
            idx,
            pitch_correct=self.pitch_correct_var.get(),
            trigger_note=trigger_note,
        )
        loop_report = self.bank.loop_point_report(idx, trigger_note=trigger_note)
        roots = ", ".join(f"{r}/{note_name(r)}" for r in s.root_keys) if s.root_keys else "-"
        lines = [
            f"Tone #{s.index}",
            f"Alias: {s.alias}",
            f"Container: {getattr(self.bank, 'family', 'Dreamcast')}",
            f"AICA format: {s.format.upper()}",
            f"Base-note playback rate: {s.current_sample_rate} Hz",
            f"Preview/export rate: {audition_rate} Hz",
            f"Root/base note(s): {roots}",
            f"Trigger note: {trigger_note}/{note_name(trigger_note)}",
            f"Pitch shift: {semis:+d} semitone(s) ({rate_note})",
            f"Duration: {s.current_sample_count / audition_rate:.6f} s" if audition_rate else "Duration: 0 s",
            f"Samples: {s.current_sample_count}",
            f"Loop: {'yes' if s.loop_flag else 'no'}",
            f"Loop start: {loop_report['loop_start_sample']}" if loop_report else "Loop start: -",
            f"Loop end [exclusive]: {loop_report['loop_end_sample_exclusive']}" if loop_report else "Loop end: -",
            f"Tone data offset: 0x{s.data_offset:06X}",
            f"Encoded extent: {s.original_extent} bytes",
            f"Usage: {', '.join(s.usage) if s.usage else 'not mapped'}",
        ]
        if s.replacement:
            lines += [
                "",
                f"Replacement: {s.replacement.wav_path.name}",
                f"Source WAV rate: {s.replacement.source_wav_rate} Hz",
                f"Stored content rate: {s.replacement.sample_rate} Hz",
                f"Encoded bytes: {len(s.replacement.encoded_payload)}",
            ]
        self.detail_var.set("\n".join(lines))

    def _dreamcast_diag_notice(self, title: str) -> None:
        messagebox.showinfo(
            title,
            "This command is specific to the gcax MPBP/MPBW layout. "
            "Dreamcast SMLT/SMPB/MDT banks use the AICA tone backend instead. "
            "Use the sample list, preview/export/replace controls, validation, or the structure view.",
        )

    def show_reverse_summary(self) -> None:
        if self._is_dreamcast_bank():
            return self._show_structural_probe(self.bank.path)
        return super().show_reverse_summary()

    def show_program_map(self) -> None:
        if self._is_dreamcast_bank():
            return self._dreamcast_diag_notice("Program map")
        return super().show_program_map()

    def show_selected_layer_fields(self) -> None:
        if self._is_dreamcast_bank():
            return self._dreamcast_diag_notice("Layer fields")
        return super().show_selected_layer_fields()

    def save_program_map_csv(self) -> None:
        if self._is_dreamcast_bank():
            return self._dreamcast_diag_notice("Program map")
        return super().save_program_map_csv()

    def save_bank_tree_json(self) -> None:
        if self._is_dreamcast_bank():
            path = filedialog.asksaveasfilename(
                title="Save Dreamcast structure JSON",
                initialfile=self.bank.path.stem + "_structure.json",
                defaultextension=".json",
                filetypes=[("JSON", "*.json")],
            )
            if path:
                Path(path).write_text(inspect_soundbank(self.bank.path).to_json(indent=2) + "\n", encoding="utf-8")
            return
        return super().save_bank_tree_json()

    def save_deep_audit(self) -> None:
        if self._is_dreamcast_bank():
            return self._dreamcast_diag_notice("Deep audit")
        return super().save_deep_audit()

    def save_samplerate_forensics(self) -> None:
        if self._is_dreamcast_bank():
            return self._dreamcast_diag_notice("Sample-rate forensics")
        return super().save_samplerate_forensics()

    def save_parameter_forensics(self) -> None:
        if self._is_dreamcast_bank():
            return self._dreamcast_diag_notice("Parameter forensics")
        return super().save_parameter_forensics()

    def _show_structural_probe(self, path: Path) -> None:
        probe = inspect_soundbank(path)

        win = tk.Toplevel(self.root)
        win.title(f"Soundbank structure - {path.name}")
        win.geometry("980x680")

        top = ttk.Frame(win, padding=8)
        top.pack(fill=tk.X)
        ttk.Label(
            top,
            text=f"{path.name} | {probe.family} | {probe.endian}-endian | "
                 f"{probe.file_size:,} bytes",
        ).pack(side=tk.LEFT)

        if probe.family == "Sonic Shuffle MDT":
            def extract() -> None:
                out = filedialog.askdirectory(
                    parent=win,
                    title="Extract MDT blocks to folder",
                )
                if not out:
                    return
                try:
                    files = extract_mdt_blocks(path, Path(out))
                    messagebox.showinfo(
                        "MDT extraction",
                        f"Extracted {len(files)} blocks to:\n{out}",
                        parent=win,
                    )
                except Exception as exc:
                    messagebox.showerror("MDT extraction failed", str(exc), parent=win)

            ttk.Button(top, text="Extract MDT blocks", command=extract).pack(
                side=tk.RIGHT
            )

        columns = ("index", "kind", "offset", "size", "bank", "aux", "notes")
        tree = ttk.Treeview(win, columns=columns, show="headings")
        widths = {
            "index": 60,
            "kind": 110,
            "offset": 110,
            "size": 110,
            "bank": 70,
            "aux": 180,
            "notes": 320,
        }
        for col in columns:
            tree.heading(col, text=col.title())
            tree.column(col, width=widths[col], stretch=(col == "notes"))

        ybar = ttk.Scrollbar(win, orient=tk.VERTICAL, command=tree.yview)
        xbar = ttk.Scrollbar(win, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)

        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0), pady=(0, 8))
        ybar.pack(side=tk.RIGHT, fill=tk.Y, pady=(0, 8))

        for entry in probe.entries:
            aux = ""
            if entry.aux_offset is not None:
                aux = f"0x{entry.aux_offset:X}/0x{(entry.aux_size or 0):X}"
            tree.insert(
                "",
                tk.END,
                values=(
                    entry.index,
                    entry.kind,
                    f"0x{entry.offset:X}",
                    f"0x{entry.size:X}",
                    "" if entry.bank_id is None else entry.bank_id,
                    aux,
                    entry.notes,
                ),
            )

        self.status_var.set(
            f"Inspected {path.name}: {probe.family}, {len(probe.entries)} entries"
        )

    def open_mlt(self) -> None:
        path_str = filedialog.askopenfilename(
            title="Open soundbank / container",
            filetypes=[
                ("Supported soundbanks", "*.mlt *.mpb *.mdt"),
                ("MLT files", "*.mlt"),
                ("MPB files", "*.mpb"),
                ("MDT files", "*.mdt"),
                ("All files", "*.*"),
            ],
        )
        if not path_str:
            return

        path = Path(path_str)
        try:
            probe = inspect_soundbank(path)
            if probe.editable_audio:
                bank = open_editable_bank(path)
                for candidate in [
                    path.with_suffix(".aliases.csv"),
                    path.with_name(path.stem + "_aliases.csv"),
                ]:
                    if candidate.exists():
                        bank.load_alias_csv(candidate)
                        break
                self.bank = bank
                self.update_program_filter_values()
                self.refresh_tree()
                self.status_var.set(
                    f"Opened {path.name}: {len(bank.samples)} samples | {probe.family}"
                )
            else:
                self._show_structural_probe(path)
        except Exception as exc:
            messagebox.showerror("Open failed", str(exc))


def main(argv: list[str]) -> int:
    root = tk.Tk()
    app = MultiFormatExplorerApp(root)

    if len(argv) >= 2 and Path(argv[1]).exists():
        path = Path(argv[1])
        try:
            probe = inspect_soundbank(path)
            if probe.editable_audio:
                app.bank = open_editable_bank(path)
                app.update_program_filter_values()
                app.refresh_tree()
                app.status_var.set(
                    f"Opened {path.name}: {len(app.bank.samples)} samples | {probe.family}"
                )
            else:
                root.after(50, lambda: app._show_structural_probe(path))
        except Exception as exc:
            root.after(50, lambda: messagebox.showerror("Open failed", str(exc)))

    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
