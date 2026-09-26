from __future__ import annotations

import sys
from pathlib import Path
from typing import List

import tkinter as tk
from tkinter import messagebox

from .core import *
from .bank import *
from .gui import *
from .clean_audio import *  # installs rendered-audio extensions on bank/gui classes

def print_cli_usage() -> None:
    print("""MLT Soundbank Explorer

Usage:
  python MLT_Soundbank_Explorer.py [bank.mlt]                         Open the GUI
  python MLT_Soundbank_Explorer.py --validate bank.mlt [report.csv] [trigger_note]
  python MLT_Soundbank_Explorer.py --repack-copy bank.mlt [out.mlt]
  python MLT_Soundbank_Explorer.py --export-all bank.mlt [out_dir] [trigger_note]
  python MLT_Soundbank_Explorer.py --export-all-clean bank.mlt [out_dir] [trigger_note] [render_hz]
  python MLT_Soundbank_Explorer.py --audio-quality-report bank.mlt [report.csv] [trigger_note] [render_hz]
  python MLT_Soundbank_Explorer.py --export-loop-preview bank.mlt index out.wav [seconds] [trigger_note]
  python MLT_Soundbank_Explorer.py --loop-report bank.mlt [report.csv]
  python MLT_Soundbank_Explorer.py --loop-seam-report bank.mlt [report.csv] [seconds] [trigger_note]
  python MLT_Soundbank_Explorer.py --save-aliases bank.mlt [aliases.csv]
  python MLT_Soundbank_Explorer.py --parameter-forensics bank.mlt [out_dir] [trigger_note]

Replacement WAV import accepts PCM 8/16/24/32-bit and IEEE-float 32/64-bit,
including WAVE_FORMAT_EXTENSIBLE. The source MLT is never modified in place.
""")


def main(argv: List[str]) -> int:
    # CLI commands.
    if len(argv) >= 2 and argv[1] in ("-h", "--help"):
        print_cli_usage()
        return 0
    if len(argv) >= 3 and argv[1] == "--validate":
        bank = MLTBank(Path(argv[2]))
        out_csv = Path(argv[3]) if len(argv) >= 4 else None
        trigger_note = int(argv[4]) if len(argv) >= 5 else DEFAULT_TRIGGER_NOTE
        rows = bank.validate_repack_plan(trigger_note=trigger_note)
        if out_csv is not None:
            bank.write_validation_report_csv(out_csv, trigger_note=trigger_note)
        errors = sum(1 for row in rows if row.get("area") != "summary" and row["severity"] == "error")
        warnings = sum(1 for row in rows if row.get("area") != "summary" and row["severity"] == "warning")
        print(f"Validated {len(bank.samples)} samples: errors={errors}, warnings={warnings}")
        if out_csv is not None:
            print(f"Saved validation report to {out_csv}")
        return 1 if errors else 0
    if len(argv) >= 3 and argv[1] == "--parameter-forensics":
        import mlt_parameter_forensics
        input_path = Path(argv[2])
        out_dir = Path(argv[3]) if len(argv) >= 4 else input_path.with_name(input_path.stem + "_parameter_forensics")
        trigger_note = int(argv[4]) if len(argv) >= 5 else DEFAULT_TRIGGER_NOTE
        summary = mlt_parameter_forensics.run(input_path, out_dir, trigger_note)
        print(
            f"Parameter forensics complete: samples={summary['sample_count']}, "
            f"layers={summary['layer_count']}, splits={summary['split_count']}, "
            f"errors={summary['validation_errors']}"
        )
        print(f"Reports written to {out_dir}")
        return 1 if summary["validation_errors"] else 0
    if len(argv) >= 3 and argv[1] == "--export-all-clean":
        bank = MLTBank(Path(argv[2]))
        out_dir = Path(argv[3]) if len(argv) >= 4 else Path(argv[2]).with_name(Path(argv[2]).stem + "_clean_wavs")
        trigger_note = int(argv[4]) if len(argv) >= 5 else DEFAULT_TRIGGER_NOTE
        render_rate = int(argv[5]) if len(argv) >= 6 else DEFAULT_CLEAN_RENDER_RATE
        bank.export_all_rendered(out_dir, pitch_correct=True, trigger_note=trigger_note, clean=True, fixed_output_rate=True, output_rate=render_rate)
        print(f"Exported {len(bank.samples)} clean WAV files to {out_dir} at {render_rate} Hz using trigger note {trigger_note}/{note_name(trigger_note)}")
        return 0
    if len(argv) >= 3 and argv[1] == "--audio-quality-report":
        bank = MLTBank(Path(argv[2]))
        out_csv = Path(argv[3]) if len(argv) >= 4 else Path(argv[2]).with_name(Path(argv[2]).stem + "_audio_quality.csv")
        trigger_note = int(argv[4]) if len(argv) >= 5 else DEFAULT_TRIGGER_NOTE
        render_rate = int(argv[5]) if len(argv) >= 6 else DEFAULT_CLEAN_RENDER_RATE
        bank.write_audio_quality_report_csv(out_csv, pitch_correct=True, trigger_note=trigger_note, clean=True, fixed_output_rate=True, output_rate=render_rate)
        print(f"Saved audio quality report to {out_csv}")
        return 0
    if len(argv) >= 3 and argv[1] == "--export-all":
        bank = MLTBank(Path(argv[2]))
        out_dir = Path(argv[3]) if len(argv) >= 4 else Path(argv[2]).with_suffix("")
        trigger_note = int(argv[4]) if len(argv) >= 5 else DEFAULT_TRIGGER_NOTE
        bank.export_all(out_dir, trigger_note=trigger_note)
        print(f"Exported {len(bank.samples)} WAV files to {out_dir} using trigger note {trigger_note}/{note_name(trigger_note)}")
        return 0
    if len(argv) >= 3 and argv[1] == "--save-aliases":
        bank = MLTBank(Path(argv[2]))
        out_csv = Path(argv[3]) if len(argv) >= 4 else Path(argv[2]).with_name(Path(argv[2]).stem + "_aliases.csv")
        bank.write_alias_csv(out_csv)
        print(f"Saved aliases to {out_csv}")
        print(bank.no_embedded_names_report())
        return 0
    if len(argv) >= 3 and argv[1] == "--loop-report":
        bank = MLTBank(Path(argv[2]))
        out_csv = Path(argv[3]) if len(argv) >= 4 else Path(argv[2]).with_name(Path(argv[2]).stem + "_loop_points.csv")
        bank.write_loop_report_csv(out_csv)
        print(f"Saved loop-point report to {out_csv}")
        return 0
    if len(argv) >= 3 and argv[1] == "--loop-seam-report":
        bank = MLTBank(Path(argv[2]))
        out_csv = Path(argv[3]) if len(argv) >= 4 else Path(argv[2]).with_name(Path(argv[2]).stem + "_loop_preview_seam.csv")
        seconds = int(argv[4]) if len(argv) >= 5 else 20
        trigger_note = int(argv[5]) if len(argv) >= 6 else DEFAULT_TRIGGER_NOTE
        bank.write_loop_preview_seam_report_csv(out_csv, preview_seconds=seconds, trigger_note=trigger_note)
        print(f"Saved loop preview seam report to {out_csv}")
        return 0
    if len(argv) >= 5 and argv[1] == "--export-loop-preview":
        bank = MLTBank(Path(argv[2]))
        index = int(argv[3])
        out_wav = Path(argv[4])
        seconds = int(argv[5]) if len(argv) >= 6 else 20
        if not bank.loop_points_samples(index):
            raise SystemExit(f"Sample {index} is not looped or has no valid loop points")
        trigger_note = int(argv[6]) if len(argv) >= 7 else DEFAULT_TRIGGER_NOTE
        bank.export_loop_preview(index, out_wav, seconds, trigger_note=trigger_note)
        print(f"Exported loop preview for sample {index} to {out_wav} using trigger note {trigger_note}/{note_name(trigger_note)}")
        return 0
    if len(argv) >= 3 and argv[1] == "--repack-copy":
        bank = MLTBank(Path(argv[2]))
        out = Path(argv[3]) if len(argv) >= 4 else Path(argv[2]).with_name(Path(argv[2]).stem + "_repacked_copy.mlt")
        bank.save_as(out)
        print(f"Saved repacked copy to {out}")
        return 0

    root = tk.Tk()
    app = MLTExplorerApp(root)
    if len(argv) >= 2 and Path(argv[1]).exists():
        try:
            app.bank = MLTBank(Path(argv[1]))
            app.update_program_filter_values()
            app.refresh_tree()
            app.status_var.set(f"Opened {Path(argv[1]).name}: {len(app.bank.samples)} samples. {app.bank.no_embedded_names_report()}")
        except Exception as exc:
            messagebox.showerror("Open failed", str(exc))
    root.mainloop()
    return 0
