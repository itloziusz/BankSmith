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

@dataclass(frozen=True)
class ChildChunkInfo:
    header: int
    body: int
    size: int
    body_end: int
    next_header: int
    magic: bytes


def child_chunks(data: bytes, container_body: int, container_end: int) -> List[ChildChunkInfo]:
    """Parse every aligned child and retain each exact inter-child span."""
    chunks: List[ChildChunkInfo] = []
    pos = int(container_body)
    container_end = int(container_end)
    while pos < container_end:
        if pos + 16 > container_end:
            raise ValueError("Truncated child chunk header in container")
        size = u32be(data, pos + 12)
        body = pos + 16
        body_end = body + size
        if body_end > container_end:
            raise ValueError(f"Child chunk {data[pos:pos + 8]!r} extends beyond its container")
        aligned_end = align16(body_end)
        if aligned_end > container_end:
            if body_end != container_end:
                raise ValueError(f"Child chunk {data[pos:pos + 8]!r} has invalid alignment padding")
            next_header = body_end
        else:
            next_header = aligned_end
        chunks.append(ChildChunkInfo(
            header=pos,
            body=body,
            size=size,
            body_end=body_end,
            next_header=next_header,
            magic=data[pos:pos + 8],
        ))
        pos = next_header
    return chunks


def find_chunk(data: bytes, magic: bytes, start: int = 0) -> Tuple[int, int, int]:
    pos = data.find(magic, start)
    if pos < 0:
        raise ValueError(f"Missing chunk {magic!r}")
    if pos + 16 > len(data):
        raise ValueError(f"Truncated chunk header for {magic!r}")
    size = u32be(data, pos + 12)
    return pos, pos + 16, size


def find_child_chunk(data: bytes, magic: bytes, container_body: int, container_end: int) -> Tuple[int, int, int]:
    """Find a declared, aligned child chunk without scanning audio payload bytes."""
    for chunk in child_chunks(data, container_body, container_end):
        if chunk.magic == magic:
            return chunk.header, chunk.body, chunk.size
    raise ValueError(f"Missing child chunk {magic!r}")


class MLTBank:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        if len(self.data) < 16 or not self.data.startswith(b"gcaxMLT "):
            raise ValueError("Not a gcaxMLT file")
        self.declared_file_size = u32be(self.data, 12)
        if self.declared_file_size != len(self.data):
            raise ValueError(
                f"MLT header size does not match file length "
                f"({self.declared_file_size} != {len(self.data)})"
            )
        self.mlt_child_chunks = child_chunks(self.data, 16, self.declared_file_size)

        # MLTM uses 0x10-byte records. Active records point to a bank from file offset 0x20.
        self.mltm_pos, self.mltm_body, self.mltm_size = find_child_chunk(
            self.data, b"gcaxMLTM", 16, self.declared_file_size
        )
        if self.mltm_size % MLTM_RECORD_SIZE:
            raise ValueError(f"MLTM directory size is not a multiple of 0x10: 0x{self.mltm_size:X}")
        self.mlt_directory_entries: List[MLTDirectoryEntry] = []
        for index in range(self.mltm_size // MLTM_RECORD_SIZE):
            start = self.mltm_body + index * MLTM_RECORD_SIZE
            raw = self.data[start:start + MLTM_RECORD_SIZE]
            is_dummy = raw[0] == 0xFF
            pointer_rel = u32be(raw, 8)
            pointer_abs = MLTM_POINTER_BASE + pointer_rel if not is_dummy else 0
            if not is_dummy and (pointer_abs < self.mltm_body + self.mltm_size or pointer_abs + 16 > len(self.data)):
                raise ValueError(
                    f"MLTM entry {index} points outside bank data: "
                    f"rel=0x{pointer_rel:X}, abs=0x{pointer_abs:X}"
                )
            self.mlt_directory_entries.append(MLTDirectoryEntry(
                index=index,
                type_id=raw[0],
                bank_id=raw[4],
                pointer_rel=pointer_rel,
                pointer_abs=pointer_abs,
                is_dummy=is_dummy,
                raw=raw,
            ))

        self.mpb_pos, self.mpb_body, self.mpb_size = find_child_chunk(
            self.data, b"gcaxMPB ", 16, self.declared_file_size
        )
        self.mpb_end = self.mpb_body + self.mpb_size
        if self.mpb_pos < 16 or self.mpb_end > len(self.data):
            raise ValueError("MPB chunk extends beyond the MLT file")
        self.mpb_child_chunks = child_chunks(self.data, self.mpb_body, self.mpb_end)
        self.mpb_directory_entry_index: Optional[int] = next(
            (
                entry.index for entry in self.mlt_directory_entries
                if not entry.is_dummy and entry.type_id == 1 and entry.pointer_abs == self.mpb_pos
            ),
            None,
        )
        if self.mpb_directory_entry_index is None:
            raise ValueError(
                f"MPB at 0x{self.mpb_pos:X} has no matching active type-1 MLTM directory record"
            )
        self.mpb_container_span_end = next(
            chunk.next_header for chunk in self.mlt_child_chunks if chunk.header == self.mpb_pos
        )
        self.mpb_container_padding = self.data[self.mpb_end:self.mpb_container_span_end]
        self.after_mpb = self.data[self.mpb_container_span_end:]

        self.mpbw_pos, self.mpbw_body, self.mpbw_size = find_child_chunk(
            self.data, b"gcaxMPBW", self.mpb_body, self.mpb_end
        )
        self.mpbp_pos, self.mpbp_body, self.mpbp_size = find_child_chunk(
            self.data, b"gcaxMPBP", self.mpb_body, self.mpb_end
        )
        if not (self.mpb_body <= self.mpbw_pos and self.mpbw_body + self.mpbw_size <= self.mpb_end):
            raise ValueError("MPBW chunk is outside the MPB container")
        if not (self.mpb_body <= self.mpbp_pos and self.mpbp_body + self.mpbp_size <= self.mpb_end):
            raise ValueError("MPBP chunk is outside the MPB container")

        # MPBP begins with three four-byte directory records: count, reserved byte and a relative u16 pointer.
        mpbp = self.data[self.mpbp_body:self.mpbp_body + self.mpbp_size]
        if len(mpbp) < 0x20:
            raise ValueError("MPBP body is too short for its directory header")
        self.mpbp_sample_count = mpbp[0]
        self.mpbp_sample_directory_unknown = mpbp[1]
        self.mpbp_sample_directory_rel = u16be(mpbp, 2)
        self.mpbp_velocity_curve_count = mpbp[4]
        self.mpbp_velocity_curve_directory_unknown = mpbp[5]
        self.mpbp_velocity_curve_table_rel = u16be(mpbp, 6)
        # Old report field names are kept for compatibility.
        self.mpbp_ramp_count = self.mpbp_velocity_curve_count
        self.mpbp_ramp_directory_unknown = self.mpbp_velocity_curve_directory_unknown
        self.mpbp_ramp_table_rel = self.mpbp_velocity_curve_table_rel
        self.mpbp_program_count = mpbp[8]
        self.mpbp_program_directory_unknown = mpbp[9]
        self.mpbp_program_pointer_rel = u16be(mpbp, 10)
        if self.mpbp_sample_directory_rel + self.mpbp_sample_count * 0x50 > len(mpbp):
            raise ValueError("MPBP sample directory is outside its chunk")
        if self.mpbp_program_pointer_rel + self.mpbp_program_count * 4 > len(mpbp):
            raise ValueError("MPBP program-pointer table is outside its chunk")
        if self.mpbp_velocity_curve_count and self.mpbp_velocity_curve_table_rel + self.mpbp_velocity_curve_count * 0x80 > len(mpbp):
            raise ValueError("MPBP velocity-curve table points outside its chunk")
        directory_ranges = []
        for name, start, length in (
            ("sample", self.mpbp_sample_directory_rel, self.mpbp_sample_count * 0x50),
            ("velocity_curve", self.mpbp_velocity_curve_table_rel, self.mpbp_velocity_curve_count * 0x80),
            ("program", self.mpbp_program_pointer_rel, self.mpbp_program_count * 4),
        ):
            if not length:
                continue
            if start < 0x20:
                raise ValueError(f"MPBP {name} directory overlaps the 0x20-byte header")
            directory_ranges.append((name, start, start + length))
        for i, (left_name, left_start, left_end) in enumerate(directory_ranges):
            for right_name, right_start, right_end in directory_ranges[i + 1:]:
                if max(left_start, right_start) < min(left_end, right_end):
                    raise ValueError(
                        f"MPBP directories overlap: {left_name} 0x{left_start:X}..0x{left_end:X}, "
                        f"{right_name} 0x{right_start:X}..0x{right_end:X}"
                    )

        self.samples: List[SampleInfo] = []
        self.usage_by_sample: Dict[int, List[str]] = {}
        self.instrument_layout_issues: List[str] = []
        self._parse_samples()
        self.mpbw_padding_byte = self._detect_mpbw_padding_byte()
        self._parse_instrument_usage()
        self._assign_aliases()

    def _parse_samples(self) -> None:
        sample_count = self.mpbp_sample_count
        sample_dir_abs = self.mpbp_body + self.mpbp_sample_directory_rel
        if sample_dir_abs + sample_count * 0x50 > self.mpbp_body + self.mpbp_size:
            raise ValueError("Sample directory exceeds MPBP chunk")

        samples: List[SampleInfo] = []
        for i in range(sample_count):
            entry_abs = sample_dir_abs + i * 0x50
            entry_rel = entry_abs - self.mpbp_body
            e = self.data[entry_abs:entry_abs + 0x50]
            num_samples = u32be(e, 0x00)
            num_nibbles = u32be(e, 0x04)
            sample_rate = u32be(e, 0x08)
            loop_flag = u16be(e, 0x0C)
            fmt = u16be(e, 0x0E)
            loop_start = u32be(e, 0x10)
            loop_end = u32be(e, 0x14)
            current_address = u32be(e, 0x18)
            coeffs = [s16be(e, 0x1C + j * 2) for j in range(16)]
            gain = u16be(e, 0x3C)
            initial_ps = u16be(e, 0x3E)
            initial_hist1 = s16be(e, 0x40)
            initial_hist2 = s16be(e, 0x42)
            loop_ps = u16be(e, 0x44)
            loop_hist1 = s16be(e, 0x46)
            loop_hist2 = s16be(e, 0x48)
            type_byte = e[0x4A]
            data_offset = u32be(e, 0x4C)
            byte_count = (num_nibbles + 1) // 2

            samples.append(SampleInfo(
                index=i,
                entry_rel=entry_rel,
                entry_abs=entry_abs,
                sample_count=num_samples,
                nibble_count=num_nibbles,
                sample_rate=sample_rate,
                loop_flag=loop_flag,
                fmt=fmt,
                loop_start=loop_start,
                loop_end=loop_end,
                current_address=current_address,
                coefficients=coeffs,
                gain=gain,
                initial_ps=initial_ps,
                initial_hist1=initial_hist1,
                initial_hist2=initial_hist2,
                loop_ps=loop_ps,
                loop_hist1=loop_hist1,
                loop_hist2=loop_hist2,
                type_byte=type_byte,
                data_offset=data_offset,
                byte_count=byte_count,
            ))

        # The next sample offset marks the end of the current stored block.
        offsets = sorted({s.data_offset for s in samples})
        for s in samples:
            if s.data_offset > self.mpbw_size:
                raise ValueError(f"Sample {s.index} points outside MPBW")
            next_off = next((off for off in offsets if off > s.data_offset), self.mpbw_size)
            s.original_extent = next_off - s.data_offset
            if s.byte_count > s.original_extent:
                raise ValueError(
                    f"Sample {s.index} encoded length exceeds its MPBW extent "
                    f"({s.byte_count} > {s.original_extent})"
                )

        self.samples = sorted(samples, key=lambda s: s.index)

    def _detect_mpbw_padding_byte(self) -> int:
        """Return the bank's dominant inter-sample padding byte (zero if none)."""
        padding = bytearray()
        for s in self.samples:
            start = self.mpbw_body + s.data_offset + s.storage_frame_byte_count
            end = self.mpbw_body + s.data_offset + s.original_extent
            if start < end:
                padding.extend(self.data[start:end])
        if not padding:
            return 0
        counts: Dict[int, int] = {}
        for value in padding:
            counts[value] = counts.get(value, 0) + 1
        return max(counts, key=counts.get)

    def _parse_instrument_usage(self) -> None:
        usage: Dict[int, List[str]] = {s.index: [] for s in self.samples}
        root_keys_by_sample: Dict[int, List[int]] = {s.index: [] for s in self.samples}
        sample_count = len(self.samples)
        issues: List[str] = []
        body = self.data[self.mpbp_body:self.mpbp_body + self.mpbp_size]
        pointer_table_rel = self.mpbp_program_pointer_rel
        program_count = self.mpbp_program_count

        for program_index in range(program_count):
            program_rel = pointer_table_rel + program_index * 4
            layer_count = body[program_rel]
            layer_rel = u16be(body, program_rel + 2)
            if layer_count == 0:
                continue
            if layer_rel + layer_count * MPBP_LAYER_DESCRIPTOR_SIZE > len(body):
                issues.append(f"P{program_index:02d}: layer descriptor table outside MPBP (rel=0x{layer_rel:X}, count={layer_count})")
                continue

            # Program -> 0x10-byte layer descriptor -> 0x30-byte split.
            for layer_index in range(layer_count):
                descriptor_rel = layer_rel + layer_index * MPBP_LAYER_DESCRIPTOR_SIZE
                if descriptor_rel + MPBP_LAYER_DESCRIPTOR_SIZE > len(body):
                    continue
                split_count = body[descriptor_rel]
                split_ptr = u16be(body, descriptor_rel + 2)
                if split_count == 0:
                    continue
                if split_ptr + split_count * MPBP_SPLIT_SIZE > len(body):
                    issues.append(f"P{program_index:02d}.L{layer_index}: split table outside MPBP (rel=0x{split_ptr:X}, count={split_count})")
                    continue
                for split_index in range(split_count):
                    block_rel = split_ptr + split_index * MPBP_SPLIT_SIZE
                    if block_rel + MPBP_SPLIT_SIZE > len(body):
                        continue
                    sample_index = u32be(body, block_rel)
                    root_key = body[block_rel + 0x0C]
                    if 0 <= sample_index < sample_count:
                        usage[sample_index].append(f"P{program_index:02d}.L{layer_index}.S{split_index}")
                        root_keys_by_sample[sample_index].append(root_key)
                    else:
                        issues.append(f"P{program_index:02d}.L{layer_index}.S{split_index}: sample index {sample_index} outside 0..{sample_count - 1}")

        # Keep the first occurrence of each mapping.
        for k, vals in usage.items():
            seen = set()
            deduped = []
            for v in vals:
                if v not in seen:
                    seen.add(v)
                    deduped.append(v)
            usage[k] = deduped
        self.usage_by_sample = usage
        self.instrument_layout_issues = issues
        for s in self.samples:
            s.usage = usage.get(s.index, [])
            roots = root_keys_by_sample.get(s.index, [])
            s.root_key_counts = dict(Counter(roots)) if roots else {}
            # Keep the first occurrence of each mapping. Most B2_WEAPONS samples have one root key.
            seen_roots = set()
            s.root_keys = []
            for rk in roots:
                if rk not in seen_roots:
                    seen_roots.add(rk)
                    s.root_keys.append(rk)

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
        return SampleInfo(
            index=s.index,
            entry_rel=s.entry_rel,
            entry_abs=s.entry_abs,
            sample_count=u32be(e, 0x00),
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
            return decode_dsp_adpcm(s.replacement.encoded_payload, self.replacement_decode_info(s))
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
        if not is_dsp_sample_nibble_address(info.loop_start) or not is_dsp_sample_nibble_address(info.loop_end):
            return None
        start = nibble_address_to_sample(info.loop_start)
        end_incl = nibble_address_to_sample(info.loop_end)
        total = max(1, info.sample_count)
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
        if not s.loop_flag:
            return None
        total = max(1, s.current_sample_count)
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

    def replace_from_wav(
        self,
        index: int,
        wav_path: Path,
        preserve_loop_ratio: bool = True,
        preserve_bank_rate: bool = True,
        auto_resample_to_audition: bool = True,
        pitch_correct: bool = False,
        trigger_note: int = DEFAULT_TRIGGER_NOTE,
    ) -> None:
        s = self.samples[index]
        source_pcm, source_sr = read_wav_as_mono_pcm16(wav_path)

        # Keep the bank rate by default and resample the imported WAV to match it.
        bank_sr = s.sample_rate if preserve_bank_rate else int(round(source_sr * MLT_RATE_WORD_DIVISOR))
        target_content_sr_exact = self.audition_sample_rate_exact(index, pitch_correct=pitch_correct, trigger_note=trigger_note) if preserve_bank_rate else float(source_sr)
        target_content_sr_header = self.audition_sample_rate(index, pitch_correct=pitch_correct, trigger_note=trigger_note) if preserve_bank_rate else int(source_sr)
        pcm = source_pcm
        if auto_resample_to_audition and abs(float(source_sr) - float(target_content_sr_exact)) > 1e-9:
            pcm = resample_pcm16_for_replacement(source_pcm, float(source_sr), float(target_content_sr_exact))
        content_sr = int(round(target_content_sr_header if auto_resample_to_audition else source_sr))
        new_count = max(1, len(pcm) // 2)

        if s.loop_flag:
            old_points = self.loop_points_samples(index)
            old_start, old_end_excl = old_points if old_points else (0, s.sample_count)
            if preserve_loop_ratio and s.sample_count > 1:
                # Scale both loop edges instead of forcing the loop to the file end.
                start_ratio = old_start / s.sample_count
                tail_ratio = (s.sample_count - old_end_excl) / s.sample_count
                loop_start_sample = max(0, min(new_count - 1, round(start_ratio * new_count)))
                tail_samples = round(tail_ratio * new_count)
                if old_end_excl < s.sample_count:
                    tail_samples = max(1, tail_samples)
                loop_end_sample_exclusive = max(
                    loop_start_sample + 1,
                    min(new_count, new_count - tail_samples),
                )
            else:
                loop_start_sample = 0
                loop_end_sample_exclusive = new_count
        else:
            loop_start_sample = 0
            loop_end_sample_exclusive = new_count

        payload, initial_ps, loop_ps, loop_hist1, loop_hist2, encoded_count = encode_dsp_adpcm(
            pcm, s.coefficients, loop_start_sample=loop_start_sample
        )
        frame_count = math.ceil(encoded_count / 14)
        nibble_count = encoded_count + frame_count * 2
        loop_start = sample_to_nibble_address(loop_start_sample if s.loop_flag else 0)
        # DSP loop end is stored as an inclusive nibble address.
        loop_end = sample_to_nibble_address(loop_end_sample_exclusive - 1)

        entry = bytearray(self.data[s.entry_abs:s.entry_abs + 0x50])
        entry[0x00:0x04] = p32be(encoded_count)
        entry[0x04:0x08] = p32be(nibble_count)
        entry[0x08:0x0C] = p32be(bank_sr)
        entry[0x0C:0x0E] = p16be(1 if s.loop_flag else 0)
        entry[0x0E:0x10] = p16be(0)  # DSP ADPCM
        entry[0x10:0x14] = p32be(loop_start)
        entry[0x14:0x18] = p32be(loop_end)
        entry[0x18:0x1C] = p32be(2)
        # Keep the original coefficient table. The encoder used it.
        entry[0x3C:0x3E] = p16be(0)
        entry[0x3E:0x40] = p16be(initial_ps)
        entry[0x40:0x42] = ps16be(0)
        entry[0x42:0x44] = ps16be(0)
        entry[0x44:0x46] = p16be(loop_ps if s.loop_flag else initial_ps)
        entry[0x46:0x48] = ps16be(loop_hist1 if s.loop_flag else 0)
        entry[0x48:0x4A] = ps16be(loop_hist2 if s.loop_flag else 0)
        entry[0x4A] = 0
        # 0x4C data offset is filled in during save/repack.

        # Decode once now so bad encoder state is caught before saving.
        check_info = SampleInfo(
            index=s.index,
            entry_rel=s.entry_rel,
            entry_abs=s.entry_abs,
            sample_count=encoded_count,
            nibble_count=nibble_count,
            sample_rate=bank_sr,
            loop_flag=s.loop_flag,
            fmt=0,
            loop_start=loop_start,
            loop_end=loop_end,
            current_address=2,
            coefficients=s.coefficients[:],
            gain=0,
            initial_ps=initial_ps,
            initial_hist1=0,
            initial_hist2=0,
            loop_ps=loop_ps,
            loop_hist1=loop_hist1,
            loop_hist2=loop_hist2,
            type_byte=0,
            data_offset=0,
            byte_count=len(payload),
        )
        decoded_check = decode_dsp_adpcm(payload, check_info)
        encoded_values = pcm16_bytes_to_list(pcm)
        decoded_values = pcm16_bytes_to_list(decoded_check)
        errors = [a - b for a, b in zip(encoded_values, decoded_values)]
        encode_peak_error = max((abs(v) for v in errors), default=0)
        encode_rms_error = math.sqrt(sum(v * v for v in errors) / len(errors)) if errors else 0.0

        s.replacement = Replacement(
            wav_path=Path(wav_path),
            pcm_le_i16=pcm,
            sample_rate=bank_sr,
            encoded_payload=payload,
            new_entry=bytes(entry),
            loop_start_sample=loop_start_sample,
            source_wav_rate=source_sr,
            content_sample_rate=content_sr,
            nibble_count=nibble_count,
            loop_end_sample_exclusive=loop_end_sample_exclusive,
            encode_peak_error=encode_peak_error,
            encode_rms_error=encode_rms_error,
        )

    def clear_replacement(self, index: int) -> None:
        self.samples[index].replacement = None

    def build_repacked(self) -> bytes:
        mpbp_body = bytearray(self.data[self.mpbp_body:self.mpbp_body + self.mpbp_size])
        mpbw_body_new = bytearray()

        for s in self.samples:
            # Align each sample start to 0x20, matching the observed MPBW layout.
            pad_len = align32(len(mpbw_body_new)) - len(mpbw_body_new)
            if pad_len:
                mpbw_body_new += bytes([self.mpbw_padding_byte]) * pad_len
            new_offset = len(mpbw_body_new)

            if s.replacement:
                payload = bytearray(s.replacement.encoded_payload)
                payload += bytes([self.mpbw_padding_byte]) * (align32(len(payload)) - len(payload))
                entry = bytearray(s.replacement.new_entry)
            else:
                payload = bytearray(self.sample_payload(s))
                entry = bytearray(self.data[s.entry_abs:s.entry_abs + 0x50])

            entry[0x4C:0x50] = p32be(new_offset)
            mpbp_body[s.entry_rel:s.entry_rel + 0x50] = entry
            mpbw_body_new += payload

        # MPBW chunk.
        mpbw_chunk = bytearray()
        mpbw_chunk += b"gcaxMPBW"
        mpbw_chunk += self.data[self.mpbw_pos + 8:self.mpbw_pos + 12]
        mpbw_chunk += p32be(len(mpbw_body_new))
        mpbw_chunk += mpbw_body_new

        # MPBP keeps the same body size; only sample entries are changed.
        mpbp_chunk = bytearray()
        mpbp_chunk += b"gcaxMPBP"
        mpbp_chunk += self.data[self.mpbp_pos + 8:self.mpbp_pos + 12]
        mpbp_chunk += p32be(len(mpbp_body))
        mpbp_chunk += mpbp_body

        def padding_bytes(original: bytes, needed: int, fallback: int = 0x55) -> bytes:
            if needed <= 0:
                return b""
            if len(original) == needed:
                return original
            if original:
                return (original * math.ceil(needed / len(original)))[:needed]
            return bytes([fallback]) * needed

        # Keep every child chunk in its original order, including unknown extensions.
        mpb_body_builder = bytearray()
        for child_index, child in enumerate(self.mpb_child_chunks):
            if child.header == self.mpbw_pos:
                child_bytes = bytes(mpbw_chunk)
            elif child.header == self.mpbp_pos:
                child_bytes = bytes(mpbp_chunk)
            else:
                child_bytes = self.data[child.header:child.body_end]
            mpb_body_builder += child_bytes

            originally_padded = child.next_header > child.body_end
            needs_next_alignment = child_index < len(self.mpb_child_chunks) - 1
            if needs_next_alignment or originally_padded:
                needed = align16(len(mpb_body_builder)) - len(mpb_body_builder)
                original_padding = self.data[child.body_end:child.next_header]
                mpb_body_builder += padding_bytes(original_padding, needed)

        mpb_body_new = bytes(mpb_body_builder)
        mpb_chunk = bytearray()
        mpb_chunk += b"gcaxMPB "
        mpb_chunk += self.data[self.mpb_pos + 8:self.mpb_pos + 12]
        mpb_chunk += p32be(len(mpb_body_new))
        mpb_chunk += mpb_body_new

        # Move later MLTM bank pointers when the rebuilt MPB changes size.
        old_after_mpb = self.mpb_container_span_end
        raw_new_after_mpb = self.mpb_pos + len(mpb_chunk)
        new_after_mpb = align16(raw_new_after_mpb) if (self.after_mpb or self.mpb_container_padding) else raw_new_after_mpb
        later_entry_delta = new_after_mpb - old_after_mpb
        prefix = bytearray(self.data[:self.mpb_pos])
        if later_entry_delta:
            for directory_entry in self.mlt_directory_entries:
                if directory_entry.is_dummy or directory_entry.pointer_abs < old_after_mpb:
                    continue
                record_abs = self.mltm_body + directory_entry.index * MLTM_RECORD_SIZE
                pointer_field_abs = record_abs + 8
                if pointer_field_abs + 4 > len(prefix):
                    raise ValueError(
                        f"Cannot update MLTM pointer for later entry {directory_entry.index}"
                    )
                new_pointer_rel = directory_entry.pointer_rel + later_entry_delta
                if not 0 <= new_pointer_rel <= 0xFFFFFFFF:
                    raise ValueError(
                        f"MLTM pointer overflow for later entry {directory_entry.index}: {new_pointer_rel}"
                    )
                prefix[pointer_field_abs:pointer_field_abs + 4] = p32be(new_pointer_rel)

        result = bytearray()
        result += prefix
        result += mpb_chunk
        top_padding_needed = new_after_mpb - len(result)
        result += padding_bytes(self.mpb_container_padding, top_padding_needed)
        result += self.after_mpb
        result[12:16] = p32be(len(result))
        return bytes(result)

    def save_as(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.build_repacked())

    def no_embedded_names_report(self) -> str:
        return (
            "Nem találtam beágyazott, emberi olvasásra szánt sample-name táblát. "
            "A nem-audio tartományokban csak chunk magic-ek és egy ABC teszt/blokk látszik, "
            "ezért a nevek generált aliasok."
        )


# Desktop interface



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
        add("info", "file", "-", "replacements", str(self.replacement_count()))
        add("info", "file", "-", "original_size", str(len(self.data)))
        add("info", "file", "-", "original_sha1", hashlib.sha1(self.data).hexdigest())

        try:
            repacked = self.build_repacked()
            add("info", "repack", "-", "repacked_size", str(len(repacked)))
            add("info", "repack", "-", "repacked_sha1", hashlib.sha1(repacked).hexdigest())
            if self.replacement_count() == 0:
                add("ok" if repacked == self.data else "error", "repack", "-", "no-edit identity check", "byte-identical" if repacked == self.data else "changed without replacements")
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
            if s.replacement or (s.fmt == 0 and s.type_byte == 0):
                expected_nibbles = s.sample_count + 2 * math.ceil(s.sample_count / 14)
                if s.replacement:
                    expected_nibbles = s.replacement.nibble_count
                    replacement_info = self.replacement_decode_info(s)
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

