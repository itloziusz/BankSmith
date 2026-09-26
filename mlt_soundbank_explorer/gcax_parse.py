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

from .gcax_chunks import *

class MLTBankBase:
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

            # Raw big-endian PCM16 entries (type 0x0A) leave the DSP sample
            # count/nibble fields at zero. Their valid PCM length is carried by
            # the direct sample-end field and padded MPBW extent instead.
            if s.type_byte == 0x0A and s.sample_count == 0:
                capacity_samples = s.original_extent // 2
                if 0 < s.loop_end < capacity_samples:
                    s.sample_count = int(s.loop_end) + 1
                else:
                    s.sample_count = capacity_samples
                s.byte_count = s.sample_count * 2

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

