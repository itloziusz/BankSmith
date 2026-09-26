from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import wave
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .core import *
from .dsp import *
from .audio import *

class GCAXValidationMixin:
    def replacement_count(self) -> int:
        return sum(1 for s in self.samples if s.replacement is not None)

    def safe_alias(self, index: int) -> str:
        s = self.samples[index]
        return "".join(c if c.isalnum() or c in "._-" else "_" for c in s.alias)

    def export_raw_payload(self, index: int, path: Path, include_padding: bool = False) -> None:
        s = self.samples[index]
        payload = self.sample_payload(s)
        if not include_padding:
            # Export complete eight-byte DSP frames, including the last partial frame.
            payload = payload[:s.storage_frame_byte_count]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    def export_all_raw_payloads(self, out_dir: Path, include_padding: bool = False) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        for s in self.samples:
            name = f"{s.index:03d}_{self.safe_alias(s.index)}_DSPADPCM.bin"
            self.export_raw_payload(s.index, out_dir / name, include_padding=include_padding)

    def validate_repack_plan(self, trigger_note: int = DEFAULT_TRIGGER_NOTE) -> List[Dict[str, object]]:
        rows: List[Dict[str, object]] = []

        def add(severity: str, area: str, index: object, message: str, detail: str = "") -> None:
            rows.append({
                "severity": severity,
                "area": area,
                "index": index,
                "message": message,
                "detail": detail,
            })

        add("info", "file", "-", "source", str(self.path))
        add("info", "file", "-", "samples", str(len(self.samples)))
        add(
            "info", "mpbp", "-", "directories",
            f"samples={self.mpbp_sample_count}@0x{self.mpbp_sample_directory_rel:X}; "
            f"velocity_curves={self.mpbp_velocity_curve_count}@0x{self.mpbp_velocity_curve_table_rel:X}; "
            f"programs={self.mpbp_program_count}@0x{self.mpbp_program_pointer_rel:X}",
        )
        for entry in self.mlt_directory_entries:
            if entry.is_dummy:
                add("info", "mltm", entry.index, "dummy directory record", entry.raw.hex(" "))
            else:
                type_name = {1: "gcaxMPB", 4: "gcaxMSB"}.get(entry.type_id, f"unknown type {entry.type_id}")
                add(
                    "ok", "mltm", entry.index, "active bank directory record",
                    f"type={type_name}; bank_id={entry.bank_id}; pointer_rel=0x{entry.pointer_rel:X}; pointer_abs=0x{entry.pointer_abs:X}",
                )
        identity_source = getattr(self, "_validation_source_data", self.data)
        add("info", "file", "-", "replacements", str(self.replacement_count()))
        add("info", "file", "-", "original_size", str(len(identity_source)))
        add("info", "file", "-", "original_sha1", hashlib.sha1(identity_source).hexdigest())

        try:
            repacked = self.build_repacked()
            add("info", "repack", "-", "repacked_size", str(len(repacked)))
            add("info", "repack", "-", "repacked_sha1", hashlib.sha1(repacked).hexdigest())
            if self.replacement_count() == 0:
                add(
                    "ok" if repacked == identity_source else "error",
                    "repack",
                    "-",
                    "no-edit identity check",
                    "byte-identical" if repacked == identity_source else "changed without replacements",
                )
            else:
                add("ok", "repack", "-", "build_repacked completed", "offsets and MPBW rebuilt in memory")
        except Exception as exc:
            add("error", "repack", "-", "build_repacked failed", str(exc))
            return rows

        for issue in self.instrument_layout_issues:
            add("error", "instrument", "-", "invalid program/layer/split reference", issue)

        for s in self.samples:
            root = self.sample_root_key(s.index)
            aud_rate, semis, note = effective_game_audition_rate(s.current_sample_rate, root, trigger_note)
            if s.entry_abs < 0 or s.entry_abs + 0x50 > len(self.data):
                add("error", "sample_entry", s.index, "sample entry outside file", f"abs=0x{s.entry_abs:X}")
            if s.data_offset < 0 or s.data_offset + max(0, s.byte_count) > self.mpbw_size:
                add("error", "payload", s.index, "sample payload outside MPBW", f"off=0x{s.data_offset:X} bytes={s.byte_count} mpbw={self.mpbw_size}")
            if s.byte_count <= 0:
                add("warning", "payload", s.index, "empty or non-positive encoded byte count", str(s.byte_count))
            replacement_info = self.replacement_decode_info(s) if s.replacement else None
            active_type = replacement_info.type_byte if replacement_info else s.type_byte
            active_fmt = replacement_info.fmt if replacement_info else s.fmt

            if active_type == 0x0A:
                info = replacement_info if replacement_info else s
                expected_bytes = info.sample_count * 2
                actual_bytes = (
                    len(s.replacement.encoded_payload)
                    if s.replacement
                    else s.byte_count
                )
                if actual_bytes != expected_bytes:
                    add(
                        "error",
                        "pcm",
                        s.index,
                        "raw PCM16 payload length disagrees with sample count",
                        f"payload={actual_bytes} expected={expected_bytes}",
                    )
                else:
                    add(
                        "ok",
                        "pcm",
                        s.index,
                        "raw PCM16 payload resolved",
                        f"samples={info.sample_count} bytes={actual_bytes}",
                    )
            elif active_fmt == 0 and active_type == 0:
                expected_nibbles = s.sample_count + 2 * math.ceil(s.sample_count / 14)
                if s.replacement:
                    expected_nibbles = s.replacement.nibble_count
                    calculated_nibbles = replacement_info.sample_count + 2 * math.ceil(replacement_info.sample_count / 14)
                    if expected_nibbles != calculated_nibbles:
                        add("error", "dsp", s.index, "replacement DSP nibble count disagrees with sample count", f"stored={expected_nibbles} expected={calculated_nibbles}")
                    replacement_frames = 8 * math.ceil(replacement_info.sample_count / 14)
                    if len(s.replacement.encoded_payload) != replacement_frames:
                        add("error", "dsp", s.index, "replacement DSP payload length disagrees with frame count", f"payload={len(s.replacement.encoded_payload)} expected_frames={replacement_frames}")
                else:
                    if s.nibble_count != expected_nibbles:
                        add("error", "dsp", s.index, "DSP nibble count disagrees with sample count", f"stored={s.nibble_count} expected={expected_nibbles}")
                    if s.storage_frame_byte_count > s.original_extent:
                        add("error", "dsp", s.index, "DSP physical frame bytes exceed MPBW extent", f"frames={s.storage_frame_byte_count} extent={s.original_extent}")
                if s.data_offset % 0x20:
                    add("warning", "dsp", s.index, "MPBW sample offset is not 0x20 aligned", f"offset=0x{s.data_offset:X}")
            if aud_rate < 4000 or aud_rate > 96000:
                add("warning", "pitch", s.index, "unusual audition rate", f"{aud_rate} Hz; {note}")
            elif abs(semis) >= 24:
                add("warning", "pitch", s.index, "large pitch shift from root", f"{semis} semitones; {note}; rate={aud_rate}")
            else:
                add("ok", "pitch", s.index, "audition rate resolved", f"{aud_rate} Hz; {note}")

            pts = self.loop_points_samples(s.index)
            if s.loop_flag:
                if not pts:
                    loop_info = self.replacement_decode_info(s) if s.replacement else s
                    add("error", "loop", s.index, "loop flag set but loop points are invalid/empty", f"start=0x{loop_info.loop_start:X} end=0x{loop_info.loop_end:X}")
                else:
                    start, end_excl = pts
                    length = max(0, end_excl - start)
                    if start < 0 or end_excl > s.current_sample_count:
                        add("warning", "loop", s.index, "loop points clamp outside decoded sample count", f"start={start} end_excl={end_excl} samples={s.current_sample_count}")
                    if length <= 0:
                        add("error", "loop", s.index, "loop length is zero", f"start={start} end_excl={end_excl}")
                    else:
                        add("ok", "loop", s.index, "loop points resolved", f"start={start} end_excl={end_excl} length={length}")

            if s.replacement:
                rep = s.replacement
                if not rep.wav_path.exists():
                    add("warning", "replacement", s.index, "replacement source path no longer exists", str(rep.wav_path))
                if rep.source_wav_rate and rep.content_sample_rate and rep.source_wav_rate != rep.content_sample_rate:
                    add("info", "replacement", s.index, "replacement was resampled for game pitch", f"source={rep.source_wav_rate} Hz content={rep.content_sample_rate} Hz stored_rate_word={rep.sample_rate} base={rep.sample_rate / MLT_RATE_WORD_DIVISOR:.3f} Hz")
                rep_info = self.replacement_decode_info(s)
                if rep_info.type_byte == 0x0A:
                    add(
                        "ok",
                        "replacement",
                        s.index,
                        "raw PCM16 replacement prepared",
                        f"samples={rep_info.sample_count} bytes={len(rep.encoded_payload)}",
                    )
                else:
                    quality = f"DSP encode RMS error={rep.encode_rms_error:.1f}, peak error={rep.encode_peak_error}"
                    if rep.encode_peak_error > 24576:
                        add("warning", "replacement", s.index, "high DSP-ADPCM encode error", quality)
                    else:
                        add("ok", "replacement", s.index, "DSP-ADPCM decode-back check", quality)
                if s.loop_flag and rep.loop_start_sample >= s.current_sample_count:
                    add("warning", "replacement", s.index, "replacement loop start is beyond sample end", f"loop_start={rep.loop_start_sample} samples={s.current_sample_count}")
                if s.loop_flag and not (rep.loop_start_sample < rep.loop_end_sample_exclusive <= s.current_sample_count):
                    add("error", "replacement", s.index, "replacement loop boundaries are invalid", f"start={rep.loop_start_sample} end={rep.loop_end_sample_exclusive} samples={s.current_sample_count}")

        errors = sum(1 for r in rows if r["severity"] == "error")
        warnings = sum(1 for r in rows if r["severity"] == "warning")
        add("ok" if errors == 0 else "error", "summary", "-", "validation summary", f"errors={errors} warnings={warnings}")
        return rows

    def write_validation_report_csv(self, path: Path, trigger_note: int = DEFAULT_TRIGGER_NOTE) -> None:
        rows = self.validate_repack_plan(trigger_note=trigger_note)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            fields = ["severity", "area", "index", "message", "detail"]
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def write_replacement_manifest_template(self, path: Path, trigger_note: int = DEFAULT_TRIGGER_NOTE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = [
            "index", "alias", "replacement_wav", "expected_preview_hz", "stored_rate_word_x2", "base_rate_exact_hz", "root_key", "root_note",
            "loop_flag", "loop_start_sample", "loop_end_sample_exclusive", "usage", "notes"
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for s in self.samples:
                root = self.sample_root_key(s.index)
                rate = self.audition_sample_rate(s.index, pitch_correct=True, trigger_note=trigger_note)
                pts = self.loop_points_samples(s.index)
                writer.writerow({
                    "index": s.index,
                    "alias": s.alias,
                    "replacement_wav": "",
                    "expected_preview_hz": rate,
                    "stored_rate_word_x2": s.current_sample_rate,
                    "base_rate_exact_hz": f"{s.current_base_sample_rate_exact:.6f}",
                    "root_key": root if root is not None else "",
                    "root_note": note_name(root) if root is not None else "",
                    "loop_flag": int(s.loop_flag),
                    "loop_start_sample": pts[0] if pts else "",
                    "loop_end_sample_exclusive": pts[1] if pts else "",
                    "usage": ";".join(s.usage),
                    "notes": "Filename may start with 000_ or match the alias for batch replacement.",
                })

    def batch_replace_from_folder(
        self,
        folder: Path,
        preserve_bank_rate: bool = True,
        pitch_correct: bool = False,
        trigger_note: int = DEFAULT_TRIGGER_NOTE,
    ) -> List[Dict[str, object]]:
        wavs = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".wav"]
        by_index: Dict[int, Path] = {}
        by_stem = {p.stem.lower(): p for p in wavs}
        for p in wavs:
            m = re.match(r"^(\d{1,3})(?:[_\- .]|$)", p.stem)
            if m:
                idx = int(m.group(1))
                if 0 <= idx < len(self.samples):
                    by_index.setdefault(idx, p)
        for s in self.samples:
            alias_key = s.alias.lower()
            safe_key = self.safe_alias(s.index).lower()
            if s.index not in by_index:
                if alias_key in by_stem:
                    by_index[s.index] = by_stem[alias_key]
                elif safe_key in by_stem:
                    by_index[s.index] = by_stem[safe_key]

        rows: List[Dict[str, object]] = []
        for idx in sorted(by_index):
            wav_path = by_index[idx]
            try:
                self.replace_from_wav(
                    idx,
                    wav_path,
                    preserve_loop_ratio=True,
                    preserve_bank_rate=preserve_bank_rate,
                    auto_resample_to_audition=True,
                    pitch_correct=pitch_correct,
                    trigger_note=trigger_note,
                )
                rows.append({"index": idx, "status": "ok", "wav": str(wav_path), "alias": self.samples[idx].alias})
            except Exception as exc:
                rows.append({"index": idx, "status": "error", "wav": str(wav_path), "alias": self.samples[idx].alias, "error": str(exc)})
        return rows

