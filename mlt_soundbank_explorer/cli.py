from __future__ import annotations

import sys
from pathlib import Path
from typing import List

from .core import DEFAULT_TRIGGER_NOTE, note_name
from .adapters import open_editable_bank
from .formats import extract_mdt_blocks, inspect_soundbank
from .render_audio import DEFAULT_CLEAN_RENDER_RATE
from .branding import APP_TITLE


def print_cli_usage() -> None:
    print("""BankSmith — Multi-Format Soundbank Editor

Usage:
  python BankSmith.py [soundbank]                         Open the multi-format GUI
  python BankSmith.py --inspect file [report.json]
  python BankSmith.py --extract-mdt file.mdt [out_dir]
  python BankSmith.py --validate bank [report.csv] [trigger_note]
  python BankSmith.py --repack-copy bank [out_file]
  python BankSmith.py --export-all bank [out_dir] [trigger_note]
  python BankSmith.py --export-all-clean bank [out_dir] [trigger_note] [render_hz]
  python BankSmith.py --audio-quality-report bank [report.csv] [trigger_note] [render_hz]
  python BankSmith.py --export-loop-preview bank index out.wav [seconds] [trigger_note]
  python BankSmith.py --loop-report bank [report.csv]
  python BankSmith.py --loop-seam-report bank [report.csv] [seconds] [trigger_note]
  python BankSmith.py --save-aliases bank [aliases.csv]
  python BankSmith.py --parameter-forensics bank.mlt [out_dir] [trigger_note]

Editable bank formats:
  - gcaxMLT archives
  - standalone gcaxMPB banks
  - Dreamcast SMLT archives
  - standalone Dreamcast SMPB/SMDB banks
  - Sonic Shuffle MDT containers with SMPB/SMDB/SOSB audio blocks

Replacement WAV import accepts PCM 8/16/24/32-bit and IEEE-float 32/64-bit,
including WAVE_FORMAT_EXTENSIBLE. Source files are never overwritten in place.
""")


def _default_repack_path(input_path: Path) -> Path:
    suffix = input_path.suffix or ".mlt"
    return input_path.with_name(input_path.stem + "_repacked_copy" + suffix)


def main(argv: List[str]) -> int:
    if len(argv) >= 2 and argv[1] in ("-h", "--help"):
        print_cli_usage()
        return 0

    if len(argv) >= 3 and argv[1] == "--inspect":
        input_path = Path(argv[2])
        probe = inspect_soundbank(input_path)
        text = probe.to_json(indent=2)
        if len(argv) >= 4:
            out_json = Path(argv[3])
            out_json.parent.mkdir(parents=True, exist_ok=True)
            out_json.write_text(text + "\n", encoding="utf-8")
            print(f"Saved structural report to {out_json}")
        else:
            print(text)
        return 0

    if len(argv) >= 3 and argv[1] == "--extract-mdt":
        input_path = Path(argv[2])
        out_dir = Path(argv[3]) if len(argv) >= 4 else input_path.with_name(input_path.stem + "_blocks")
        files = extract_mdt_blocks(input_path, out_dir)
        print(f"Extracted {len(files)} MDT blocks to {out_dir}")
        return 0

    if len(argv) >= 3 and argv[1] == "--validate":
        bank = open_editable_bank(Path(argv[2]))
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
        input_path = Path(argv[2])
        probe = inspect_soundbank(input_path)
        if not probe.family.startswith("gcax"):
            raise SystemExit("--parameter-forensics is specific to gcax MPBP/MPBW banks")
        import mlt_parameter_forensics
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
        bank = open_editable_bank(Path(argv[2]))
        out_dir = Path(argv[3]) if len(argv) >= 4 else Path(argv[2]).with_name(Path(argv[2]).stem + "_clean_wavs")
        trigger_note = int(argv[4]) if len(argv) >= 5 else DEFAULT_TRIGGER_NOTE
        render_rate = int(argv[5]) if len(argv) >= 6 else DEFAULT_CLEAN_RENDER_RATE
        bank.export_all_rendered(
            out_dir,
            pitch_correct=True,
            trigger_note=trigger_note,
            clean=True,
            fixed_output_rate=True,
            output_rate=render_rate,
        )
        print(
            f"Exported {len(bank.samples)} clean WAV files to {out_dir} at "
            f"{render_rate} Hz using trigger note {trigger_note}/{note_name(trigger_note)}"
        )
        return 0

    if len(argv) >= 3 and argv[1] == "--audio-quality-report":
        bank = open_editable_bank(Path(argv[2]))
        out_csv = Path(argv[3]) if len(argv) >= 4 else Path(argv[2]).with_name(Path(argv[2]).stem + "_audio_quality.csv")
        trigger_note = int(argv[4]) if len(argv) >= 5 else DEFAULT_TRIGGER_NOTE
        render_rate = int(argv[5]) if len(argv) >= 6 else DEFAULT_CLEAN_RENDER_RATE
        bank.write_audio_quality_report_csv(
            out_csv,
            pitch_correct=True,
            trigger_note=trigger_note,
            clean=True,
            fixed_output_rate=True,
            output_rate=render_rate,
        )
        print(f"Saved audio quality report to {out_csv}")
        return 0

    if len(argv) >= 3 and argv[1] == "--export-all":
        bank = open_editable_bank(Path(argv[2]))
        out_dir = Path(argv[3]) if len(argv) >= 4 else Path(argv[2]).with_suffix("")
        trigger_note = int(argv[4]) if len(argv) >= 5 else DEFAULT_TRIGGER_NOTE
        bank.export_all(out_dir, trigger_note=trigger_note)
        print(
            f"Exported {len(bank.samples)} WAV files to {out_dir} using trigger note "
            f"{trigger_note}/{note_name(trigger_note)}"
        )
        return 0

    if len(argv) >= 3 and argv[1] == "--save-aliases":
        bank = open_editable_bank(Path(argv[2]))
        out_csv = Path(argv[3]) if len(argv) >= 4 else Path(argv[2]).with_name(Path(argv[2]).stem + "_aliases.csv")
        bank.write_alias_csv(out_csv)
        print(f"Saved aliases to {out_csv}")
        print(bank.no_embedded_names_report())
        return 0

    if len(argv) >= 3 and argv[1] == "--loop-report":
        bank = open_editable_bank(Path(argv[2]))
        out_csv = Path(argv[3]) if len(argv) >= 4 else Path(argv[2]).with_name(Path(argv[2]).stem + "_loop_points.csv")
        bank.write_loop_report_csv(out_csv)
        print(f"Saved loop-point report to {out_csv}")
        return 0

    if len(argv) >= 3 and argv[1] == "--loop-seam-report":
        bank = open_editable_bank(Path(argv[2]))
        out_csv = Path(argv[3]) if len(argv) >= 4 else Path(argv[2]).with_name(Path(argv[2]).stem + "_loop_preview_seam.csv")
        seconds = int(argv[4]) if len(argv) >= 5 else 20
        trigger_note = int(argv[5]) if len(argv) >= 6 else DEFAULT_TRIGGER_NOTE
        bank.write_loop_preview_seam_report_csv(out_csv, preview_seconds=seconds, trigger_note=trigger_note)
        print(f"Saved loop preview seam report to {out_csv}")
        return 0

    if len(argv) >= 5 and argv[1] == "--export-loop-preview":
        bank = open_editable_bank(Path(argv[2]))
        index = int(argv[3])
        out_wav = Path(argv[4])
        seconds = int(argv[5]) if len(argv) >= 6 else 20
        if not bank.loop_points_samples(index):
            raise SystemExit(f"Sample {index} is not looped or has no valid loop points")
        trigger_note = int(argv[6]) if len(argv) >= 7 else DEFAULT_TRIGGER_NOTE
        bank.export_loop_preview(index, out_wav, seconds, trigger_note=trigger_note)
        print(
            f"Exported loop preview for sample {index} to {out_wav} using trigger note "
            f"{trigger_note}/{note_name(trigger_note)}"
        )
        return 0

    if len(argv) >= 3 and argv[1] == "--repack-copy":
        input_path = Path(argv[2])
        bank = open_editable_bank(input_path)
        out = Path(argv[3]) if len(argv) >= 4 else _default_repack_path(input_path)
        bank.save_as(out)
        print(f"Saved repacked copy to {out}")
        return 0

    from .multi_gui import main as multi_gui_main
    return multi_gui_main(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
