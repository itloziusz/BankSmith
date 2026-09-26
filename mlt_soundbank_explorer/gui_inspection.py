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
from .branding import BANK_TREE_TOOL_NAME

class GUIInspectionMixin:
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
            "tool": BANK_TREE_TOOL_NAME,
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

