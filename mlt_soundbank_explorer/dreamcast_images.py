from __future__ import annotations

import math
import struct
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .dreamcast_codec import tone_capacity_samples


def u16le(data: bytes | bytearray, off: int) -> int:
    return struct.unpack_from('<H', data, off)[0]


def u32le(data: bytes | bytearray, off: int) -> int:
    return struct.unpack_from('<I', data, off)[0]


def p16le(value: int) -> bytes:
    return struct.pack('<H', int(value) & 0xFFFF)


def p32le(value: int) -> bytes:
    return struct.pack('<I', int(value) & 0xFFFFFFFF)


def align(value: int, n: int) -> int:
    return (int(value) + n - 1) & ~(n - 1)


@dataclass
class DreamcastReplacement:
    wav_path: Path
    pcm_le_i16: bytes
    payload: bytes
    sample_count: int
    sample_rate: int
    format: str


@dataclass
class ToneRef:
    record_offset: int
    usage: str
    format: str
    loop: bool
    loop_start: int
    loop_end: int
    base_note: int = 60


@dataclass
class ToneRecord:
    ptr: int
    format: str
    raw_payload: bytes
    sample_count: int
    refs: List[ToneRef]
    replacement: Optional[DreamcastReplacement] = None

    @property
    def current_payload(self) -> bytes:
        return self.replacement.payload if self.replacement else self.raw_payload

    @property
    def current_sample_count(self) -> int:
        return self.replacement.sample_count if self.replacement else self.sample_count


class DreamcastToneImage:
    nominal_rate: int = 22050

    def __init__(self, data: bytes, label: str = ''):
        self.data = bytes(data)
        self.label = label
        self.tones: List[ToneRecord] = []
        self._parse()

    @property
    def modified(self) -> bool:
        return any(t.replacement is not None for t in self.tones)

    def _parse(self) -> None:
        raise NotImplementedError

    def build(self) -> bytes:
        raise NotImplementedError


class DreamcastMPBImage(DreamcastToneImage):
    nominal_rate = 22050

    def _parse(self) -> None:
        d = self.data
        if len(d) < 48 or d[:4] not in (b'SMPB', b'SMDB'):
            raise ValueError('Not a Dreamcast SMPB/SMDB bank')
        self.magic = d[:4]
        self.version = u32le(d, 4)
        if self.version not in (1, 0x5001, 2):
            raise ValueError(f'Unsupported Dreamcast MPB version 0x{self.version:X}')
        self.file_size = u32le(d, 8)
        if not 48 <= self.file_size <= len(d):
            raise ValueError(f'Dreamcast MPB file size is invalid: {self.file_size} / {len(d)}')
        self.ptr_programs = u32le(d, 16)
        self.num_programs = u32le(d, 20)
        self.ptr_velocities = u32le(d, 24)
        self.num_velocities = u32le(d, 28)
        if self.num_programs > 128 or self.num_velocities > 31:
            raise ValueError('Dreamcast MPB directory counts exceed driver limits')
        if self.ptr_programs + self.num_programs * 4 > len(d):
            raise ValueError('Dreamcast MPB program table is out of bounds')

        refs_by_ptr: Dict[int, List[ToneRef]] = defaultdict(list)
        for pi in range(self.num_programs):
            program_ptr = u32le(d, self.ptr_programs + pi * 4)
            if not program_ptr:
                continue
            if program_ptr + 16 > len(d):
                raise ValueError(f'MPB program {pi} pointer is out of bounds')
            for li in range(4):
                layer_ptr = u32le(d, program_ptr + li * 4)
                if not layer_ptr:
                    continue
                if layer_ptr + 16 > len(d):
                    raise ValueError(f'MPB layer {pi}:{li} pointer is out of bounds')
                num_splits = u32le(d, layer_ptr)
                split_ptr = u32le(d, layer_ptr + 4)
                if num_splits > 128 or split_ptr + num_splits * 48 > len(d):
                    raise ValueError(f'MPB split table {pi}:{li} is out of bounds')
                for si in range(num_splits):
                    off = split_ptr + si * 48
                    jump = d[off]
                    flags = d[off + 1]
                    tone_ptr = u16le(d, off + 2) + ((jump & 0x7F) << 16)
                    if not tone_ptr:
                        continue
                    fmt = 'adpcm' if flags & 1 else ('pcm8' if jump & 0x80 else 'pcm16')
                    refs_by_ptr[tone_ptr].append(ToneRef(
                        record_offset=off,
                        usage=f'P{pi:03d}.L{li}.S{si}',
                        format=fmt,
                        loop=bool(flags & 2),
                        loop_start=u16le(d, off + 4),
                        loop_end=u16le(d, off + 6),
                        base_note=d[off + 38],
                    ))

        ptrs = sorted(refs_by_ptr)
        data_end = self.file_size - (8 if (self.version & 0xFF) >= 2 else 4)
        if ptrs and not (0 < ptrs[0] <= data_end):
            raise ValueError('Dreamcast MPB tone data starts outside the bank')
        tones: List[ToneRecord] = []
        for i, ptr in enumerate(ptrs):
            end = ptrs[i + 1] if i + 1 < len(ptrs) else data_end
            if end < ptr or end > len(d):
                raise ValueError('Dreamcast MPB tone extent is invalid')
            refs = refs_by_ptr[ptr]
            formats = {r.format for r in refs}
            if len(formats) != 1:
                raise ValueError(f'Shared tone 0x{ptr:X} has conflicting formats')
            fmt = next(iter(formats))
            payload = d[ptr:end]
            capacity = tone_capacity_samples(len(payload), fmt)
            valid_ends = [r.loop_end for r in refs if 0 < r.loop_end < 0xFFFF and r.loop_end <= capacity]
            sample_count = max(valid_ends) if valid_ends else min(capacity, 65534)
            tones.append(ToneRecord(ptr, fmt, payload, sample_count, refs))
        self.tones = tones

    def build(self) -> bytes:
        if not self.modified or not self.tones:
            return self.data
        first_tone = min(t.ptr for t in self.tones)
        out = bytearray(self.data[:first_tone])
        original_counts = {t.ptr: max(1, t.sample_count) for t in self.tones}
        for tone in sorted(self.tones, key=lambda t: t.ptr):
            new_ptr = len(out)
            out.extend(tone.current_payload)
            new_count = tone.current_sample_count
            if new_count >= 65535:
                raise ValueError('Dreamcast MPB replacement exceeds 65534 samples')
            for ref in tone.refs:
                jump = ((new_ptr >> 16) & 0x7F) | (0x80 if ref.format == 'pcm8' else 0)
                out[ref.record_offset] = jump
                out[ref.record_offset + 2:ref.record_offset + 4] = p16le(new_ptr & 0xFFFF)
                if tone.replacement:
                    old_count = original_counts[tone.ptr]
                    if ref.loop:
                        start = max(0, min(new_count - 1, round((ref.loop_start / old_count) * new_count)))
                        if 0 < ref.loop_end < 0xFFFF:
                            end = max(start + 1, min(new_count, round((ref.loop_end / old_count) * new_count)))
                        else:
                            end = new_count
                    else:
                        start, end = 0, new_count
                    out[ref.record_offset + 4:ref.record_offset + 6] = p16le(start)
                    out[ref.record_offset + 6:ref.record_offset + 8] = p16le(end)

        end_pos = len(out)
        file_size = end_pos + (8 if (self.version & 0xFF) >= 2 else 4)
        out[8:12] = p32le(file_size)
        if (self.version & 0xFF) >= 2:
            out += p32le(sum(out[4:end_pos]) & 0xFFFFFFFF)
        out += b'ENDB'
        out += b'\x00' * (align(len(out), 32) - len(out))
        return bytes(out)


class DreamcastOSBImage(DreamcastToneImage):
    nominal_rate = 44100

    def _parse(self) -> None:
        d = self.data
        if len(d) < 16 or d[:4] != b'SOSB':
            raise ValueError('Not a Dreamcast SOSB bank')
        self.version = u32le(d, 4)
        if self.version not in (1, 2):
            raise ValueError(f'Unsupported Dreamcast OSB version {self.version}')
        self.file_size = u32le(d, 8)
        self.num_programs = u32le(d, 12)
        if self.num_programs > 65535 or 16 + self.num_programs * 4 > len(d):
            raise ValueError('Dreamcast OSB program table is out of bounds')
        refs_by_ptr: Dict[int, List[ToneRef]] = defaultdict(list)
        for pi in range(self.num_programs):
            ptr = u32le(d, 16 + pi * 4)
            if not ptr:
                continue
            if ptr + (56 if self.version == 1 else 64) > len(d) or d[ptr:ptr+4] != b'SOSP':
                raise ValueError(f'OSB program {pi} is invalid')
            jump = d[ptr + 4]
            flags = d[ptr + 5]
            tone_ptr = u16le(d, ptr + 6) + ((jump & 0x7F) << 16)
            if not tone_ptr:
                continue
            fmt = 'adpcm' if flags & 1 else ('pcm8' if jump & 0x80 else 'pcm16')
            base_note = d[ptr + (42 if self.version == 1 else 44)]
            refs_by_ptr[tone_ptr].append(ToneRef(
                record_offset=ptr,
                usage=f'OSB.P{pi:03d}',
                format=fmt,
                loop=bool(flags & 2),
                loop_start=u16le(d, ptr + 8),
                loop_end=u16le(d, ptr + 10),
                base_note=base_note,
            ))
        ptrs = sorted(refs_by_ptr)
        data_end = self.file_size - (8 if self.version >= 2 else 4)
        tones: List[ToneRecord] = []
        for i, ptr in enumerate(ptrs):
            if ptr < 4 or d[ptr-4:ptr] != b'SOSD':
                raise ValueError(f'OSB tone at 0x{ptr:X} has no SOSD marker')
            if i + 1 < len(ptrs):
                raw_size = (ptrs[i+1] - ptr) - 8
            else:
                raw_size = (data_end - ptr) - 4
            raw_size = max(0, raw_size)
            refs = refs_by_ptr[ptr]
            fmt = refs[0].format
            if any(r.format != fmt for r in refs):
                raise ValueError('Shared OSB tone has conflicting formats')
            capacity = tone_capacity_samples(raw_size, fmt)
            valid_ends = [r.loop_end for r in refs if 0 < r.loop_end < 0xFFFF and r.loop_end <= capacity]
            sample_count = max(valid_ends) if valid_ends else min(capacity, 65534)
            needed_bytes = math.ceil(sample_count * ({'adpcm':0.5,'pcm8':1.0,'pcm16':2.0}[fmt]))
            payload = d[ptr:ptr+min(raw_size, needed_bytes if valid_ends else raw_size)]
            tones.append(ToneRecord(ptr, fmt, payload, sample_count, refs))
        self.tones = tones

    def build(self) -> bytes:
        if not self.modified or not self.tones:
            return self.data
        first_chunk = min(t.ptr - 4 for t in self.tones)
        out = bytearray(self.data[:first_chunk])
        for tone in sorted(self.tones, key=lambda t: t.ptr):
            out += b'SOSD'
            new_ptr = len(out)
            out += tone.current_payload
            out += b'\x00' * (align(len(out), 4) - len(out))
            out += b'ENDD'
            new_count = tone.current_sample_count
            if new_count >= 65535:
                raise ValueError('Dreamcast OSB replacement exceeds 65534 samples')
            old_count = max(1, tone.sample_count)
            for ref in tone.refs:
                base = ref.record_offset
                jump = ((new_ptr >> 16) & 0x7F) | (0x80 if ref.format == 'pcm8' else 0)
                out[base + 4] = jump
                out[base + 6:base + 8] = p16le(new_ptr & 0xFFFF)
                if tone.replacement:
                    if ref.loop:
                        start = max(0, min(new_count - 1, round((ref.loop_start / old_count) * new_count)))
                        end = max(start + 1, min(new_count, round((ref.loop_end / old_count) * new_count))) if ref.loop_end else new_count
                    else:
                        start, end = 0, new_count
                    out[base + 8:base + 10] = p16le(start)
                    out[base + 10:base + 12] = p16le(end)
                    if self.version == 1:
                        out[base + 48:base + 52] = p32le(end)
                    else:
                        out[base + 52:base + 56] = p32le(end)
        end_pos = len(out)
        file_size = end_pos + (8 if self.version >= 2 else 4)
        out[8:12] = p32le(file_size)
        if self.version >= 2:
            out += p32le(sum(out[4:end_pos]) & 0xFFFFFFFF)
        out += b'ENDB'
        out += b'\xFF' * (align(len(out), 32) - len(out))
        return bytes(out)
