from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import platform
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import wave
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

def u16be(b: bytes | bytearray, off: int) -> int:
    return struct.unpack_from(">H", b, off)[0]


def s16be(b: bytes | bytearray, off: int) -> int:
    return struct.unpack_from(">h", b, off)[0]


def u32be(b: bytes | bytearray, off: int) -> int:
    return struct.unpack_from(">I", b, off)[0]


def p16be(v: int) -> bytes:
    return struct.pack(">H", int(v) & 0xFFFF)


def ps16be(v: int) -> bytes:
    return struct.pack(">h", max(-32768, min(32767, int(v))))


def p32be(v: int) -> bytes:
    return struct.pack(">I", int(v) & 0xFFFFFFFF)


def align(x: int, n: int = 0x10) -> int:
    return (x + (n - 1)) & ~(n - 1)


def align16(x: int) -> int:
    return align(x, 0x10)


def align32(x: int) -> int:
    return align(x, 0x20)


def clamp16(v: int) -> int:
    return -32768 if v < -32768 else 32767 if v > 32767 else int(v)


STANDARD_AUDITION_RATES = (5512, 8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000)
COMMON_RATE_SNAP_MAX_CENTS = 8.0
MLT_RATE_WORD_DIVISOR = 2.0
DEFAULT_TRIGGER_NOTE = 60  # C4. MPBP stores twice the base sample rate.
NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
MPBP_LAYER_DESCRIPTOR_SIZE = 0x10
MPBP_SPLIT_SIZE = 0x30
MLTM_RECORD_SIZE = 0x10
MLTM_POINTER_BASE = 0x20


def note_name(midi_note: int) -> str:
    midi_note = int(midi_note)
    return f"{NOTE_NAMES[midi_note % 12]}{midi_note // 12 - 1}"


def closest_standard_rate(rate: float, max_cents: float = COMMON_RATE_SNAP_MAX_CENTS) -> Optional[int]:
    if rate <= 0:
        return None
    nearest = min(STANDARD_AUDITION_RATES, key=lambda base: abs(math.log(rate / base, 2)))
    cents = 1200.0 * math.log(rate / nearest, 2)
    return int(nearest) if abs(cents) <= float(max_cents) else None


def closest_standard_rate_with_error(rate: float) -> Tuple[Optional[int], float, float]:
    if rate <= 0:
        return None, 0.0, 0.0
    nearest = min(STANDARD_AUDITION_RATES, key=lambda r: abs(math.log(max(rate, 1e-12) / r, 2)))
    cents = 1200.0 * math.log(max(rate, 1e-12) / nearest, 2)
    ppm = (rate / nearest - 1.0) * 1_000_000.0
    return nearest, cents, ppm


def effective_game_rate_exact(bank_rate: int, root_key: Optional[int], trigger_note: int = DEFAULT_TRIGGER_NOTE) -> Tuple[float, int, str]:
    bank_rate = int(bank_rate)
    if bank_rate <= 0:
        return float(bank_rate), 0, "invalid"
    base_rate = bank_rate / MLT_RATE_WORD_DIVISOR
    if root_key is None:
        return float(base_rate), 0, "no root key; exact rate is stored rate / 2"
    semis = int(trigger_note) - int(root_key)
    raw_rate = base_rate * (2 ** (semis / 12.0))
    return float(raw_rate), semis, f"exact game pitch: stored rate / 2, trigger {trigger_note}/{note_name(trigger_note)} vs root {root_key}/{note_name(root_key)}"


def effective_game_audition_rate(bank_rate: int, root_key: Optional[int], trigger_note: int = DEFAULT_TRIGGER_NOTE) -> Tuple[int, int, str]:
    """Return the WAV audition rate that matches in-game sample playback.

    MPBP entry offset 0x08 stores twice the sample's base rate. The instrument
    block stores a root key at offset 0x0C. When the engine triggers the sound at
    a MIDI note, playback speed/pitch is approximately:

        base_rate = stored_rate / 2
        effective_rate = base_rate * 2 ** ((trigger_note - root_key) / 12)

    B2_WEAPONS maps the observed bank rates/root keys to normal rates when the
    default trigger note is 60/C4:
      31183 Hz @ root 66 -> ~11025 Hz
      44100 Hz @ root 60 -> 22050 Hz
      62367 Hz @ root 54 -> ~44100 Hz

    Returns: effective_rate_hz, semitone_delta_from_root, explanatory_note.
    """
    bank_rate = int(bank_rate)
    if bank_rate <= 0:
        return bank_rate, 0, "invalid"
    raw_rate, semis, exact_note = effective_game_rate_exact(bank_rate, root_key, trigger_note)
    if root_key is None:
        standard = closest_standard_rate(raw_rate)
        return (int(standard) if standard is not None else int(round(raw_rate))), 0, "no root key; raw/nearest-standard bank rate"
    snapped = closest_standard_rate(raw_rate)
    effective = int(snapped if snapped is not None else round(raw_rate))
    return effective, semis, exact_note.replace("exact game pitch", "game pitch")


def sign4(v: int) -> int:
    v &= 0x0F
    return v - 16 if v >= 8 else v


def sample_to_nibble_address(sample_index: int) -> int:
    sample_index = max(0, int(sample_index))
    return sample_index + 2 * (sample_index // 14) + 2


def is_dsp_sample_nibble_address(addr: int) -> bool:
    """True only for a DSP ADPCM data nibble, never a two-nibble frame header."""
    addr = int(addr)
    return addr >= 2 and (addr & 0x0F) >= 2


def nibble_address_to_sample(addr: int) -> int:
    addr = int(addr)
    if not is_dsp_sample_nibble_address(addr):
        raise ValueError(f"Not a DSP sample nibble address: 0x{addr:X}")
    frame = addr // 16
    rem = addr % 16
    return frame * 14 + (rem - 2)


# Data records


@dataclass(frozen=True)
class MLTDirectoryEntry:
    """One 0x10-byte gcaxMLTM directory record."""

    index: int
    type_id: int
    bank_id: int
    pointer_rel: int
    pointer_abs: int
    is_dummy: bool
    raw: bytes


@dataclass
class Replacement:
    wav_path: Path
    pcm_le_i16: bytes
    sample_rate: int  # MPBP stored rate word (twice the physical base Hz)
    encoded_payload: bytes
    new_entry: bytes
    loop_start_sample: int
    source_wav_rate: int = 0
    content_sample_rate: int = 0  # rate of pcm_le_i16 after optional resampling
    nibble_count: int = 0
    loop_end_sample_exclusive: int = 0
    encode_peak_error: int = 0
    encode_rms_error: float = 0.0


@dataclass
class SampleInfo:
    index: int
    entry_rel: int
    entry_abs: int
    sample_count: int
    nibble_count: int
    sample_rate: int
    loop_flag: int
    fmt: int
    loop_start: int
    loop_end: int
    current_address: int
    coefficients: List[int]
    gain: int
    initial_ps: int
    initial_hist1: int
    initial_hist2: int
    loop_ps: int
    loop_hist1: int
    loop_hist2: int
    type_byte: int
    data_offset: int
    byte_count: int
    original_extent: int = 0
    alias: str = ""
    usage: List[str] = field(default_factory=list)
    root_keys: List[int] = field(default_factory=list)
    root_key_counts: Dict[int, int] = field(default_factory=dict)
    replacement: Optional[Replacement] = None

    @property
    def duration_seconds(self) -> float:
        return self.sample_count / (self.sample_rate / MLT_RATE_WORD_DIVISOR) if self.sample_rate else 0.0

    @property
    def base_sample_rate_exact(self) -> float:
        return self.sample_rate / MLT_RATE_WORD_DIVISOR

    @property
    def is_looped(self) -> bool:
        return bool(self.loop_flag)

    @property
    def current_sample_count(self) -> int:
        if self.replacement:
            return len(self.replacement.pcm_le_i16) // 2
        return self.sample_count

    @property
    def current_sample_rate(self) -> int:
        if self.replacement:
            return self.replacement.sample_rate
        return self.sample_rate

    @property
    def current_duration_seconds(self) -> float:
        sr = self.current_sample_rate / MLT_RATE_WORD_DIVISOR
        return self.current_sample_count / sr if sr else 0.0

    @property
    def current_base_sample_rate_exact(self) -> float:
        return self.current_sample_rate / MLT_RATE_WORD_DIVISOR

    @property
    def replacement_label(self) -> str:
        return self.replacement.wav_path.name if self.replacement else ""

    @property
    def logical_encoded_byte_count(self) -> int:
        """Bytes described by the DSP nibble count, excluding storage padding."""
        nibble_count = self.replacement.nibble_count if self.replacement else self.nibble_count
        return (int(nibble_count) + 1) // 2

    @property
    def storage_frame_byte_count(self) -> int:
        """Physical DSP frame bytes needed to decode every advertised sample."""
        if self.fmt == 0 and self.type_byte == 0:
            return 8 * math.ceil(self.current_sample_count / 14)
        return self.logical_encoded_byte_count
