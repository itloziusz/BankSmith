#!/usr/bin/env python3
from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from MLT_Soundbank_Explorer import MLTExplorerApp
from multi_format_adapter import open_editable_bank
from soundbank_formats import extract_mdt_blocks, format_summary_text, inspect_soundbank


class MultiFormatExplorerApp(MLTExplorerApp):
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
