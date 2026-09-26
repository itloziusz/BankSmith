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

class GCAXMetadataMixin:
    def _assign_aliases(self) -> None:
        stem = self.path.stem
        for s in self.samples:
            kind = "loop" if s.loop_flag else "oneshot"
            if s.usage:
                primary = s.usage[0].split(".")[0].lower()
                s.alias = f"{stem}_{primary}_sample_{s.index:03d}_{kind}"
            else:
                s.alias = f"{stem}_sample_{s.index:03d}_{kind}"

    def load_alias_csv(self, path: Path) -> int:
        changed = 0
        if not path.exists():
            return 0
        by_idx = {s.index: s for s in self.samples}
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    idx = int(row.get("index", ""))
                except ValueError:
                    continue
                alias = (row.get("alias") or row.get("proposed_name") or "").strip()
                if alias and idx in by_idx:
                    by_idx[idx].alias = alias
                    changed += 1
        return changed

    def write_alias_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as f:
            fields = [
                "index", "alias", "confidence", "reason", "stored_rate_word_x2", "base_rate_exact_hz", "root_key", "root_note", "trigger_note", "wav_preview_rate",
                "duration_seconds_at_preview_rate", "loop_flag", "usage", "source_offset_hex",
            ]
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for s in self.samples:
                wav_rate = self.audition_sample_rate(s.index, pitch_correct=True)
                writer.writerow({
                    "index": s.index,
                    "alias": s.alias,
                    "confidence": "generated",
                    "reason": "No embedded human-readable sample-name table was found; alias is generated from bank/program/sample metadata.",
                    "stored_rate_word_x2": s.current_sample_rate,
                    "base_rate_exact_hz": f"{s.current_base_sample_rate_exact:.6f}",
                    "root_key": self.sample_root_key(s.index) if self.sample_root_key(s.index) is not None else "",
                    "root_note": note_name(self.sample_root_key(s.index)) if self.sample_root_key(s.index) is not None else "",
                    "trigger_note": f"{DEFAULT_TRIGGER_NOTE}/{note_name(DEFAULT_TRIGGER_NOTE)}",
                    "wav_preview_rate": wav_rate,
                    "duration_seconds_at_preview_rate": f"{s.current_sample_count / wav_rate:.6f}" if wav_rate else "0",
                    "loop_flag": int(s.loop_flag),
                    "usage": ";".join(s.usage),
                    "source_offset_hex": f"0x{s.data_offset:06X}",
                })

    def write_loop_report_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [self.loop_point_report(s.index) for s in self.samples if s.loop_flag]
        rows = [r for r in rows if r]
        fields = [
            "index", "alias", "loop_start_addr_hex", "loop_end_addr_hex",
            "loop_start_sample", "loop_end_sample_inclusive", "loop_end_sample_exclusive",
            "loop_length_samples", "loop_length_seconds_pitch_corrected",
            "loop_ps", "loop_hist1", "loop_hist2",
        ]
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def sample_payload(self, s: SampleInfo) -> bytes:
        if s.replacement:
            return s.replacement.encoded_payload
        start = self.mpbw_body + s.data_offset
        return self.data[start:start + s.original_extent]

    def replacement_decode_info(self, s: SampleInfo) -> SampleInfo:
        """Build the DSP header actually written for a staged replacement."""
        if not s.replacement:
            raise ValueError("Sample has no replacement")
        e = s.replacement.new_entry
        replacement_type = e[0x4A]
        replacement_sample_count = (
            len(s.replacement.pcm_le_i16) // 2
            if replacement_type == 0x0A
            else u32be(e, 0x00)
        )
        return SampleInfo(
            index=s.index,
            entry_rel=s.entry_rel,
            entry_abs=s.entry_abs,
            sample_count=replacement_sample_count,
            nibble_count=u32be(e, 0x04),
            sample_rate=u32be(e, 0x08),
            loop_flag=u16be(e, 0x0C),
            fmt=u16be(e, 0x0E),
            loop_start=u32be(e, 0x10),
            loop_end=u32be(e, 0x14),
            current_address=u32be(e, 0x18),
            coefficients=[s16be(e, 0x1C + j * 2) for j in range(16)],
            gain=u16be(e, 0x3C),
            initial_ps=u16be(e, 0x3E),
            initial_hist1=s16be(e, 0x40),
            initial_hist2=s16be(e, 0x42),
            loop_ps=u16be(e, 0x44),
            loop_hist1=s16be(e, 0x46),
            loop_hist2=s16be(e, 0x48),
            type_byte=e[0x4A],
            data_offset=0,
            byte_count=len(s.replacement.encoded_payload),
        )

    def decode_sample(self, index: int) -> bytes:
        s = self.samples[index]
        if s.replacement:
            # Preview the encoded replacement, not the source WAV.
            info = self.replacement_decode_info(s)
            if info.type_byte == 0x0A:
                return decode_raw_be_pcm(s.replacement.encoded_payload, info)
            return decode_dsp_adpcm(s.replacement.encoded_payload, info)
        payload = self.sample_payload(s)
        if s.fmt == 0 and s.type_byte == 0:
            return decode_dsp_adpcm(payload, s)
        if s.type_byte == 0x0A:
            return decode_raw_be_pcm(payload, s)
        # Unknown type values still use DSP decoding when coefficients are present.
        if s.fmt == 0:
            return decode_dsp_adpcm(payload, s)
        raise ValueError(f"Unsupported sample format at index {index}: fmt={s.fmt}, type=0x{s.type_byte:02X}")

    def sample_root_key(self, index: int) -> Optional[int]:
        s = self.samples[index]
        if not s.root_keys:
            return None
        # Use the most common root key; file order breaks ties.
        counts = s.root_key_counts or {rk: 1 for rk in s.root_keys}
        return sorted(counts, key=lambda rk: (-counts[rk], s.root_keys.index(rk)))[0]

    def rate_correction(self, index: int, trigger_note: int = DEFAULT_TRIGGER_NOTE) -> Tuple[int, int, str]:
        s = self.samples[index]
        return effective_game_audition_rate(s.current_sample_rate, self.sample_root_key(index), trigger_note=trigger_note)

    def rate_correction_exact(self, index: int, trigger_note: int = DEFAULT_TRIGGER_NOTE) -> Tuple[float, int, str]:
        s = self.samples[index]
        return effective_game_rate_exact(s.current_sample_rate, self.sample_root_key(index), trigger_note=trigger_note)

    def audition_sample_rate_exact(self, index: int, pitch_correct: bool = False, trigger_note: int = DEFAULT_TRIGGER_NOTE) -> float:
        s = self.samples[index]
        if not pitch_correct:
            return s.current_base_sample_rate_exact
        corrected, _semis, _note = self.rate_correction_exact(index, trigger_note=trigger_note)
        return float(corrected or s.current_base_sample_rate_exact)

    def audition_sample_rate(self, index: int, pitch_correct: bool = False, trigger_note: int = DEFAULT_TRIGGER_NOTE) -> int:
        s = self.samples[index]
        if not pitch_correct:
            return int(round(s.current_base_sample_rate_exact))
        corrected, _semis, _note = self.rate_correction(index, trigger_note=trigger_note)
        return corrected or int(round(s.current_base_sample_rate_exact))

    def export_sample(self, index: int, path: Path, pitch_correct: bool = False, trigger_note: int = DEFAULT_TRIGGER_NOTE) -> None:
        write_wav(path, self.decode_sample(index), self.audition_sample_rate(index, pitch_correct=pitch_correct, trigger_note=trigger_note))

    def loop_point_report(self, index: int, trigger_note: int = DEFAULT_TRIGGER_NOTE) -> Optional[Dict[str, int | float | str]]:
        s = self.samples[index]
        if not s.loop_flag:
            return None
        # Staged replacements report their own loop and predictor state.
        info = self.replacement_decode_info(s) if s.replacement else s
        if info.sample_count <= 0:
            return None

        if not s.replacement and info.type_byte == 0x0A:
            # Raw PCM16 banks store loop positions directly as sample indices.
            start = int(info.loop_start)
            end_incl = int(info.loop_end)
        else:
            if not is_dsp_sample_nibble_address(info.loop_start) or not is_dsp_sample_nibble_address(info.loop_end):
                return None
            start = nibble_address_to_sample(info.loop_start)
            end_incl = nibble_address_to_sample(info.loop_end)

        total = info.sample_count
        start = max(0, min(start, total - 1))
        end_incl = max(start, min(end_incl, total - 1))
        end_excl = min(total, end_incl + 1)
        audition_rate = max(1, self.audition_sample_rate(index, pitch_correct=True, trigger_note=trigger_note))
        return {
            "index": s.index,
            "alias": s.alias,
            "loop_start_addr_hex": f"0x{info.loop_start:X}",
            "loop_end_addr_hex": f"0x{info.loop_end:X}",
            "loop_start_sample": start,
            "loop_end_sample_inclusive": end_incl,
            "loop_end_sample_exclusive": end_excl,
            "loop_length_samples": max(0, end_excl - start),
            "loop_length_seconds_pitch_corrected": max(0, end_excl - start) / audition_rate,
            "loop_ps": info.loop_ps,
            "loop_hist1": info.loop_hist1,
            "loop_hist2": info.loop_hist2,
        }

    def loop_points_samples(self, index: int) -> Optional[Tuple[int, int]]:
        """Return loop start/end-exclusive sample indices for the current sample.

        Original MLT/DSP loop start/end fields are nibble addresses. The loop
        end is treated as inclusive, then converted to Python-style exclusive.
        Replacement WAVs retain both original boundaries as relative positions.
        """
        s = self.samples[index]
        if not s.loop_flag or s.current_sample_count <= 0:
            return None
        total = s.current_sample_count
        if s.replacement:
            start = int(s.replacement.loop_start_sample)
            end_excl = int(s.replacement.loop_end_sample_exclusive or total)
        else:
            report = self.loop_point_report(index)
            if not report:
                return None
            start = int(report["loop_start_sample"])
            end_excl = int(report["loop_end_sample_exclusive"])
        start = max(0, min(start, total - 1))
        end_excl = max(start + 1, min(end_excl, total))
        return start, end_excl

    def build_loop_preview_pcm(
        self,
        index: int,
        preview_seconds: int = 20,
        pitch_correct: bool = False,
        trigger_note: int = DEFAULT_TRIGGER_NOTE,
        declick: bool = True,
        crossfade_ms: float = 3.0,
        zero_cross: bool = True,
        trim_silence: bool = True,
    ) -> bytes:
        """Build one gapless intro + repeated-loop PCM buffer for preview/export.

        A single WAV body avoids switching from an
        intro WAV to a loop WAV. That removes the player handoff gap. Optional
        zero-cross + short crossfade smoothing removes loop-boundary clicks.
        """
        pcm = self.decode_sample(index)
        points = self.loop_points_samples(index)
        if not points:
            return pcm
        start, end_excl = points
        sr = max(1, self.audition_sample_rate(index, pitch_correct=pitch_correct, trigger_note=trigger_note))
        loop_pcm, _diag = build_gapless_loop_preview_pcm(
            pcm,
            start,
            end_excl,
            sr,
            preview_seconds,
            declick=declick,
            crossfade_ms=crossfade_ms,
            zero_cross=zero_cross,
            trim_silence=trim_silence,
        )
        return loop_pcm

    def loop_preview_diagnostics(
        self,
        index: int,
        preview_seconds: int = 20,
        pitch_correct: bool = False,
        trigger_note: int = DEFAULT_TRIGGER_NOTE,
        declick: bool = True,
        crossfade_ms: float = 3.0,
        zero_cross: bool = True,
        trim_silence: bool = True,
    ) -> Dict[str, int | float | str]:
        pcm = self.decode_sample(index)
        points = self.loop_points_samples(index)
        if not points:
            return {"index": index, "status": "no_loop"}
        sr = max(1, self.audition_sample_rate(index, pitch_correct=pitch_correct, trigger_note=trigger_note))
        _pcm, diag = build_gapless_loop_preview_pcm(
            pcm, points[0], points[1], sr, preview_seconds,
            declick=declick, crossfade_ms=crossfade_ms, zero_cross=zero_cross, trim_silence=trim_silence,
        )
        s = self.samples[index]
        diag.update({"index": index, "alias": s.alias, "sample_rate": sr, "loop_start_original": points[0], "loop_end_original_exclusive": points[1]})
        return diag

    def export_loop_preview(
        self,
        index: int,
        path: Path,
        preview_seconds: int = 20,
        pitch_correct: bool = False,
        trigger_note: int = DEFAULT_TRIGGER_NOTE,
        declick: bool = True,
        crossfade_ms: float = 3.0,
        zero_cross: bool = True,
        trim_silence: bool = True,
    ) -> None:
        write_wav(
            path,
            self.build_loop_preview_pcm(
                index, preview_seconds, pitch_correct=pitch_correct, trigger_note=trigger_note,
                declick=declick, crossfade_ms=crossfade_ms, zero_cross=zero_cross, trim_silence=trim_silence,
            ),
            self.audition_sample_rate(index, pitch_correct=pitch_correct, trigger_note=trigger_note),
        )

    def write_loop_preview_seam_report_csv(
        self,
        path: Path,
        preview_seconds: int = 20,
        pitch_correct: bool = False,
        trigger_note: int = DEFAULT_TRIGGER_NOTE,
        declick: bool = True,
        crossfade_ms: float = 3.0,
        zero_cross: bool = True,
        trim_silence: bool = True,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for s in self.samples:
            if s.loop_flag:
                rows.append(self.loop_preview_diagnostics(
                    s.index, preview_seconds, pitch_correct=pitch_correct, trigger_note=trigger_note,
                    declick=declick, crossfade_ms=crossfade_ms, zero_cross=zero_cross, trim_silence=trim_silence,
                ))
        fields = sorted({k for row in rows for k in row.keys()}) or ["empty"]
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def export_all(self, out_dir: Path, pitch_correct: bool = False, trigger_note: int = DEFAULT_TRIGGER_NOTE) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        metadata = []
        for s in self.samples:
            safe_alias = "".join(c if c.isalnum() or c in "._-" else "_" for c in s.alias)
            audition_rate = self.audition_sample_rate(s.index, pitch_correct=pitch_correct, trigger_note=trigger_note)
            corrected_rate, semis, rate_note = self.rate_correction(s.index, trigger_note=trigger_note)
            name = f"{s.index:03d}_{safe_alias}_{audition_rate}Hz"
            if pitch_correct and audition_rate != int(round(s.current_base_sample_rate_exact)):
                name += f"_pitchFixed_from_word{s.current_sample_rate}"
            if s.loop_flag:
                name += "_loop"
            wav_path = out_dir / f"{name}.wav"
            self.export_sample(
                s.index,
                wav_path,
                pitch_correct=pitch_correct,
                trigger_note=trigger_note,
            )
            loop_report = self.loop_point_report(s.index) or {}
            metadata.append({
                "index": s.index,
                "filename": wav_path.name,
                "alias": s.alias,
                "stored_rate_word_x2": s.current_sample_rate,
                "base_rate_exact_hz": f"{s.current_base_sample_rate_exact:.6f}",
                "root_key": self.sample_root_key(s.index) if self.sample_root_key(s.index) is not None else "",
                "root_note": note_name(self.sample_root_key(s.index)) if self.sample_root_key(s.index) is not None else "",
                "trigger_note": f"{trigger_note}/{note_name(trigger_note)}",
                "wav_export_rate": audition_rate,
                "pitch_correction_semitones": semis,
                "pitch_correction_note": rate_note,
                "samples": s.current_sample_count,
                "duration_seconds_at_wav_rate": f"{s.current_sample_count / audition_rate:.6f}" if audition_rate else "0",
                "loop_flag": int(s.loop_flag),
                "loop_start_addr_hex": loop_report.get("loop_start_addr_hex", ""),
                "loop_end_addr_hex": loop_report.get("loop_end_addr_hex", ""),
                "loop_start_sample": loop_report.get("loop_start_sample", ""),
                "loop_end_sample_inclusive": loop_report.get("loop_end_sample_inclusive", ""),
                "loop_end_sample_exclusive": loop_report.get("loop_end_sample_exclusive", ""),
                "loop_length_samples": loop_report.get("loop_length_samples", ""),
                "loop_length_seconds_pitch_corrected": loop_report.get("loop_length_seconds_pitch_corrected", ""),
                "usage": ";".join(s.usage),
                "replacement": s.replacement_label,
            })
        with (out_dir / f"{self.path.stem}_metadata.csv").open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(metadata[0].keys()) if metadata else ["index"])
            writer.writeheader()
            writer.writerows(metadata)
        self.write_alias_csv(out_dir / f"{self.path.stem}_aliases.csv")

