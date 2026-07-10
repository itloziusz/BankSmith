#!/usr/bin/env python3
"""MLT Soundbank Explorer

Desktop editor for gcaxMLT soundbanks and their gcaxMPB sample data.

Main features:
- opens MLT banks and lists their samples, rates, loop data and program usage;
- previews and exports samples as mono PCM WAV;
- replaces samples from PCM or floating-point WAV files;
- re-encodes replacements to DSP-ADPCM and rebuilds the bank safely;
- exports aliases, loop reports, validation data and raw sample payloads.

The original MLT is never overwritten automatically. Save edited banks to a
new file and keep the source bank as a backup.
"""
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

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"Tkinter is required for the GUI: {exc}")


# Binary helpers


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


# DSP-ADPCM codec


def decode_dsp_adpcm(payload: bytes, info: SampleInfo) -> bytes:
    """Decode mono Nintendo/GameCube DSP-ADPCM to little-endian PCM16."""
    out = bytearray()
    hist1 = int(info.initial_hist1)
    hist2 = int(info.initial_hist2)
    produced = 0
    pos = 0

    while produced < info.sample_count and pos < len(payload):
        frame = payload[pos:min(pos + 8, len(payload))]
        pos += len(frame)
        pred_scale = frame[0]
        predictor = (pred_scale >> 4) & 0x0F
        scale_shift = pred_scale & 0x0F
        coef_index = predictor * 2
        coef1 = info.coefficients[coef_index] if coef_index < len(info.coefficients) else 0
        coef2 = info.coefficients[coef_index + 1] if coef_index + 1 < len(info.coefficients) else 0
        scale = 1 << scale_shift

        # The final DSP frame may be exported without its unused padding bytes.
        # Decode all available nibbles and stop exactly at advertised count.
        for b in frame[1:]:
            for nibble in (sign4(b >> 4), sign4(b)):
                if produced >= info.sample_count:
                    break
                decoded = (((nibble * scale) << 11) + 1024 + (coef1 * hist1) + (coef2 * hist2)) >> 11
                decoded = clamp16(decoded)
                out += struct.pack("<h", decoded)
                hist2, hist1 = hist1, decoded
                produced += 1

    if produced < info.sample_count:
        out += b"\x00\x00" * (info.sample_count - produced)
    return bytes(out)


def decode_raw_be_pcm(payload: bytes, info: SampleInfo) -> bytes:
    """Best-effort raw big-endian PCM16 fallback for MLT entries marked as raw."""
    need = info.sample_count * 2
    payload = payload[:need]
    out = bytearray()
    for off in range(0, len(payload) - 1, 2):
        out += struct.pack("<h", s16be(payload, off))
    if len(out) < need:
        out += b"\x00" * (need - len(out))
    return bytes(out)


def _decode_one_adpcm_sample(nibble: int, scale: int, coef1: int, coef2: int, hist1: int, hist2: int) -> int:
    return clamp16((((nibble * scale) << 11) + 1024 + (coef1 * hist1) + (coef2 * hist2)) >> 11)


def encode_dsp_adpcm(
    pcm_le_i16: bytes,
    coefficients: List[int],
    loop_start_sample: int = 0,
) -> Tuple[bytes, int, int, int, int, int]:
    """Encode PCM16 mono to compatible DSP-ADPCM.

    Returns: payload, initial_ps, loop_ps, loop_hist1, loop_hist2, encoded_sample_count

    This is a conservative encoder: it uses the supplied coefficient table and
    chooses the best predictor/scale per frame by local squared-error search.
    """
    if len(pcm_le_i16) % 2:
        pcm_le_i16 += b"\x00"
    samples = list(struct.unpack("<" + "h" * (len(pcm_le_i16) // 2), pcm_le_i16))
    if not samples:
        samples = [0]

    coefs = [int(c) for c in (coefficients or [])[:16]]
    while len(coefs) < 16:
        coefs.append(0)

    # If a source table is broken, fall back to predictor 0 = no prediction.
    if all(c == 0 for c in coefs):
        pred_range = range(1)  # predictor 0 only
    else:
        pred_range = range(8)

    loop_start_sample = max(0, min(int(loop_start_sample), len(samples) - 1))
    out = bytearray()
    hist1 = 0
    hist2 = 0
    initial_ps = 0
    loop_ps = 0
    loop_hist1 = 0
    loop_hist2 = 0
    loop_captured = False
    global_sample_index = 0

    for frame_start in range(0, len(samples), 14):
        frame_real = samples[frame_start:frame_start + 14]
        frame = frame_real + [0] * (14 - len(frame_real))

        best_err: Optional[int] = None
        best_control = 0
        best_nibbles: List[int] = [0] * 14
        best_decoded: List[int] = [0] * 14
        best_hist1 = hist1
        best_hist2 = hist2

        for pred in pred_range:
            coef1 = coefs[pred * 2]
            coef2 = coefs[pred * 2 + 1]
            # Try all legal-ish shifts used by common DSP encoders. 0..12 is
            # enough for PCM16 dynamic range; 13..15 would be very coarse.
            for shift in range(13):
                scale = 1 << shift
                hh1, hh2 = hist1, hist2
                nibbles: List[int] = []
                decoded_values: List[int] = []
                err = 0
                for sample in frame:
                    predicted = (coef1 * hh1 + coef2 * hh2 + 1024) >> 11
                    q = int(round((sample - predicted) / scale)) if scale else 0
                    if q < -8:
                        q = -8
                    elif q > 7:
                        q = 7
                    decoded = _decode_one_adpcm_sample(q, scale, coef1, coef2, hh1, hh2)
                    diff = sample - decoded
                    err += diff * diff
                    nibbles.append(q & 0x0F)
                    decoded_values.append(decoded)
                    hh2, hh1 = hh1, decoded
                    # Early exit for speed when already worse.
                    if best_err is not None and err > best_err:
                        break
                if best_err is None or err < best_err:
                    best_err = err
                    best_control = (pred << 4) | shift
                    best_nibbles = nibbles + [0] * (14 - len(nibbles))
                    best_decoded = decoded_values + [0] * (14 - len(decoded_values))
                    best_hist1 = hh1
                    best_hist2 = hh2

        if frame_start == 0:
            initial_ps = best_control
        if frame_start <= loop_start_sample < frame_start + 14:
            loop_ps = best_control

        out.append(best_control)
        for i in range(0, 14, 2):
            out.append(((best_nibbles[i] & 0x0F) << 4) | (best_nibbles[i + 1] & 0x0F))

        # Replay chosen frame to capture loop history at exact sample boundary.
        pred = (best_control >> 4) & 0x0F
        shift = best_control & 0x0F
        scale = 1 << shift
        coef1 = coefs[pred * 2]
        coef2 = coefs[pred * 2 + 1]
        for local_i, q_u in enumerate(best_nibbles):
            if global_sample_index >= len(samples):
                break
            if global_sample_index == loop_start_sample and not loop_captured:
                loop_hist1 = hist1
                loop_hist2 = hist2
                loop_captured = True
            q = sign4(q_u)
            decoded = _decode_one_adpcm_sample(q, scale, coef1, coef2, hist1, hist2)
            hist2, hist1 = hist1, decoded
            global_sample_index += 1

        # Padding must not affect the predictor history.

    if not loop_captured:
        loop_hist1 = 0
        loop_hist2 = 0
    return bytes(out), initial_ps, loop_ps, loop_hist1, loop_hist2, len(samples)


# WAV handling


def pcm16_to_wav_bytes(pcm_le_i16: bytes, sample_rate: int) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(int(sample_rate))
            wf.writeframes(pcm_le_i16)
        data = Path(tmp_path).read_bytes()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return data


def write_wav(path: Path, pcm_le_i16: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm_le_i16)


def read_wav_as_mono_pcm16(path: Path) -> Tuple[bytes, int]:
    """Read a standard RIFF/WAVE file as mono signed 16-bit PCM.

    ``wave`` only accepts a narrow subset of PCM files on several Python
    versions. Audio editors routinely produce WAVE_FORMAT_EXTENSIBLE and
    32-bit/64-bit IEEE-float WAVs, so replacements used to fail before the
    encoder was reached. This RIFF reader accepts the
    common PCM and float variants, skips unknown chunks, and mixes all input
    channels to mono deterministically.
    """
    raw_file = Path(path).read_bytes()
    if len(raw_file) < 12 or raw_file[:4] != b"RIFF" or raw_file[8:12] != b"WAVE":
        raise ValueError("Only little-endian RIFF/WAVE files are supported")

    fmt: Optional[bytes] = None
    data_parts: List[bytes] = []
    pos = 12
    while pos + 8 <= len(raw_file):
        chunk_id = raw_file[pos:pos + 4]
        chunk_size = struct.unpack_from("<I", raw_file, pos + 4)[0]
        body_start = pos + 8
        body_end = body_start + chunk_size
        if body_end > len(raw_file):
            raise ValueError(f"Truncated WAV chunk {chunk_id!r}")
        if chunk_id == b"fmt ":
            fmt = raw_file[body_start:body_end]
        elif chunk_id == b"data":
            data_parts.append(raw_file[body_start:body_end])
        pos = body_end + (chunk_size & 1)

    if fmt is None or len(fmt) < 16:
        raise ValueError("WAV has no valid fmt chunk")
    if not data_parts:
        raise ValueError("WAV has no data chunk")

    format_tag, channels, sample_rate, _byte_rate, block_align, bits = struct.unpack_from("<HHIIHH", fmt, 0)
    valid_bits = bits
    if format_tag == 0xFFFE:  # WAVE_FORMAT_EXTENSIBLE
        if len(fmt) < 40:
            raise ValueError("Truncated WAVE_FORMAT_EXTENSIBLE fmt chunk")
        valid_bits = struct.unpack_from("<H", fmt, 18)[0] or bits
        # The first DWORD of the subformat GUID is the legacy format tag.
        format_tag = struct.unpack_from("<H", fmt, 24)[0]
    if channels < 1:
        raise ValueError("WAV has no channels")
    if sample_rate <= 0:
        raise ValueError("WAV has an invalid sample rate")
    if format_tag not in (1, 3):
        raise ValueError(f"Unsupported WAV format tag: 0x{format_tag:04X} (use PCM or IEEE float)")
    if format_tag == 1 and bits not in (8, 16, 24, 32):
        raise ValueError(f"Unsupported PCM WAV depth: {bits}-bit")
    if format_tag == 3 and bits not in (32, 64):
        raise ValueError(f"Unsupported IEEE-float WAV depth: {bits}-bit")

    bytes_per_sample = bits // 8
    expected_block_align = channels * bytes_per_sample
    if block_align != expected_block_align:
        raise ValueError(f"Invalid WAV block alignment: {block_align}, expected {expected_block_align}")
    raw = b"".join(data_parts)
    if len(raw) % block_align:
        raise ValueError("WAV data length is not an exact number of frames")

    def decode_pcm_sample(buf: bytes, offset: int) -> int:
        if format_tag == 3:
            value = struct.unpack_from("<f" if bits == 32 else "<d", buf, offset)[0]
            if not math.isfinite(value):
                value = 0.0
            return clamp16(round(max(-1.0, min(1.0, value)) * (32768 if value < 0 else 32767)))

        if bits == 8:
            value = int(buf[offset]) - 128
        elif bits == 16:
            value = struct.unpack_from("<h", buf, offset)[0]
        elif bits == 24:
            value = int.from_bytes(buf[offset:offset + 3], "little", signed=True)
        else:
            value = struct.unpack_from("<i", buf, offset)[0]

        # Extensible PCM can specify fewer meaningful bits; those bits are
        # left-aligned in the storage container.
        meaningful = max(1, min(int(valid_bits), int(bits)))
        if meaningful < bits:
            value >>= bits - meaningful
        if meaningful > 16:
            value >>= meaningful - 16
        elif meaningful < 16:
            value <<= 16 - meaningful
        return clamp16(value)

    values: List[int] = []
    for frame_start in range(0, len(raw), block_align):
        mixed = sum(decode_pcm_sample(raw, frame_start + ch * bytes_per_sample) for ch in range(channels))
        values.append(clamp16(round(mixed / channels)))
    return i16_list_to_pcm16_bytes(values), int(sample_rate)


def resample_pcm16_linear(pcm_le_i16: bytes, src_rate: int | float, dst_rate: int | float) -> bytes:
    """Simple dependency-free mono PCM16 linear resampler.

    Exact float destination rates matter for this MLT family because
    stored rates such as 31183 at root F#4 resolve to 11024.855 Hz at C4, not exactly
    11025 Hz. WAV headers still need integer rates, but replacement sample counts
    should be derived from the exact ratio to avoid slow loop drift.
    """
    src_rate_f = float(src_rate)
    dst_rate_f = float(dst_rate)
    if src_rate_f <= 0 or dst_rate_f <= 0 or abs(src_rate_f - dst_rate_f) < 1e-9:
        return pcm_le_i16
    if len(pcm_le_i16) < 4:
        return pcm_le_i16
    samples = list(struct.unpack("<" + "h" * (len(pcm_le_i16) // 2), pcm_le_i16))
    if not samples:
        return pcm_le_i16
    new_len = max(1, int(round(len(samples) * dst_rate_f / src_rate_f)))
    if new_len == 1:
        return struct.pack("<h", samples[0])
    out: List[int] = []
    scale = (len(samples) - 1) / (new_len - 1)
    for i in range(new_len):
        pos = i * scale
        j = int(pos)
        frac = pos - j
        if j >= len(samples) - 1:
            v = samples[-1]
        else:
            v = int(round(samples[j] * (1.0 - frac) + samples[j + 1] * frac))
        out.append(clamp16(v))
    return struct.pack("<" + "h" * len(out), *out)


def _sinc(x: float) -> float:
    if abs(x) < 1e-12:
        return 1.0
    pix = math.pi * x
    return math.sin(pix) / pix


def resample_pcm16_bandlimited(pcm_le_i16: bytes, src_rate: int | float, dst_rate: int | float, half_taps: int = 16) -> bytes:
    """Resample mono PCM16 with a windowed-sinc anti-alias filter.

    Replacements commonly go from 44.1/48 kHz down to a game rate such as
    11.024855 kHz. Linear interpolation folds high frequencies back into the
    audible band. The filter is only selected for downsampling, where that
    anti-alias protection matters; upsampling remains on the faster cubic path.
    """
    src_rate_f = float(src_rate)
    dst_rate_f = float(dst_rate)
    if src_rate_f <= 0 or dst_rate_f <= 0 or abs(src_rate_f - dst_rate_f) < 1e-9:
        return pcm_le_i16
    samples = pcm16_bytes_to_list(pcm_le_i16)
    if len(samples) < 4:
        return resample_pcm16_linear(pcm_le_i16, src_rate_f, dst_rate_f)
    new_len = max(1, int(round(len(samples) * dst_rate_f / src_rate_f)))
    if new_len == 1:
        return struct.pack("<h", samples[0])

    half = max(4, min(64, int(half_taps)))
    cutoff = min(1.0, dst_rate_f / src_rate_f) * 0.96
    ratio = src_rate_f / dst_rate_f
    last = len(samples) - 1
    out: List[int] = []
    for i in range(new_len):
        # Centre-aligned positions avoid a one-sample shift at either edge.
        pos = (i + 0.5) * ratio - 0.5
        center = math.floor(pos)
        total = 0.0
        weight_sum = 0.0
        for source_index in range(center - half + 1, center + half + 1):
            distance = pos - source_index
            if abs(distance) >= half:
                continue
            weight = cutoff * _sinc(cutoff * distance) * _sinc(distance / half)
            clamped_index = max(0, min(last, source_index))
            total += samples[clamped_index] * weight
            weight_sum += weight
        out.append(clamp16(round(total / weight_sum if abs(weight_sum) > 1e-12 else samples[max(0, min(last, center))])))
    return i16_list_to_pcm16_bytes(out)


def resample_pcm16_for_replacement(pcm_le_i16: bytes, src_rate: int | float, dst_rate: int | float) -> bytes:
    """Use anti-aliased downsampling and smooth cubic upsampling for imports."""
    if float(dst_rate) < float(src_rate):
        return resample_pcm16_bandlimited(pcm_le_i16, src_rate, dst_rate)
    return resample_pcm16_cubic(pcm_le_i16, src_rate, dst_rate)


# Loop preview helpers


def pcm16_bytes_to_list(pcm_le_i16: bytes) -> List[int]:
    if not pcm_le_i16:
        return []
    count = len(pcm_le_i16) // 2
    return list(struct.unpack("<" + "h" * count, pcm_le_i16[:count * 2]))


def i16_list_to_pcm16_bytes(samples: List[int]) -> bytes:
    if not samples:
        return b""
    return struct.pack("<" + "h" * len(samples), *(clamp16(v) for v in samples))


def _sign_nonzero(v: int) -> int:
    return -1 if v < 0 else 1 if v > 0 else 0


def nearest_zero_crossing_index(samples: List[int], center: int, window: int = 96) -> int:
    """Find a nearby low-energy zero crossing without moving the loop point far.

    The returned value is a sample index usable as a loop boundary. If no real
    sign crossing exists in the search window, the quietest nearby sample is
    returned. This is preview-only; it does not rewrite the MLT loop points.
    """
    if not samples:
        return 0
    n = len(samples)
    center = max(1, min(n - 1, int(center)))
    window = max(0, int(window))
    lo = max(1, center - window)
    hi = min(n - 1, center + window)
    best = center
    best_score = (10**18, 10**18)
    for i in range(lo, hi + 1):
        prev_s = _sign_nonzero(samples[i - 1])
        cur_s = _sign_nonzero(samples[i])
        crossing = (prev_s != 0 and cur_s != 0 and prev_s != cur_s) or samples[i] == 0
        if crossing:
            # Low energy + short movement beats a distant perfect-looking crossing.
            score = (abs(samples[i]) + abs(samples[i - 1]), abs(i - center))
            if score < best_score:
                best_score = score
                best = i
    if best != center or best_score[0] < 10**18:
        return best
    # Fallback: quietest point near the requested boundary.
    for i in range(lo, hi + 1):
        score = (abs(samples[i]), abs(i - center))
        if score < best_score:
            best_score = score
            best = i
    return best


def trim_loop_head_silence(samples: List[int], start: int, end_excl: int, threshold: int = 12, max_trim: int = 2048) -> int:
    """Skip accidental silent padding at the beginning of the loop segment.

    Some extracted loop ranges contain a tiny silent head, or old preview code
    created an audible pause by switching from an intro WAV to a loop WAV. This
    function only moves inside the existing loop region and only for preview.
    """
    n = len(samples)
    start = max(0, min(int(start), n - 1 if n else 0))
    end_excl = max(start + 1, min(int(end_excl), n))
    limit = min(end_excl - 1, start + max(0, int(max_trim)))
    i = start
    # Require several non-quiet samples before accepting the new head; avoids
    # snapping to a random single-sample tick.
    while i < limit:
        if abs(samples[i]) > threshold:
            look = samples[i:min(end_excl, i + 16)]
            if sum(1 for v in look if abs(v) > threshold) >= max(1, min(4, len(look))):
                return i
        i += 1
    return start


def resolve_preview_loop_bounds(
    pcm_le_i16: bytes,
    start: int,
    end_excl: int,
    *,
    zero_cross: bool = True,
    trim_silence: bool = True,
    zero_cross_window: int = 96,
    silence_threshold: int = 12,
    max_silence_trim: int = 2048,
) -> Tuple[int, int, Dict[str, int]]:
    """Return preview-safe loop bounds plus small diagnostics."""
    samples = pcm16_bytes_to_list(pcm_le_i16)
    total = len(samples)
    if total <= 1:
        return 0, total, {"original_start": start, "original_end_exclusive": end_excl, "trimmed_samples": 0, "zero_cross_start_delta": 0, "zero_cross_end_delta": 0}
    original_start = max(0, min(int(start), total - 1))
    original_end = max(original_start + 1, min(int(end_excl), total))
    s = original_start
    e = original_end
    if trim_silence:
        s = trim_loop_head_silence(samples, s, e, threshold=silence_threshold, max_trim=max_silence_trim)
    z_s_delta = 0
    z_e_delta = 0
    if zero_cross and e - s > max(8, zero_cross_window * 2):
        zs = nearest_zero_crossing_index(samples, s, zero_cross_window)
        ze = nearest_zero_crossing_index(samples, e - 1, zero_cross_window) + 1
        if zs < ze - 8:
            z_s_delta = zs - s
            z_e_delta = ze - e
            s, e = zs, ze
    s = max(0, min(s, total - 1))
    e = max(s + 1, min(e, total))
    return s, e, {
        "original_start": original_start,
        "original_end_exclusive": original_end,
        "trimmed_samples": max(0, s - original_start),
        "zero_cross_start_delta": z_s_delta,
        "zero_cross_end_delta": z_e_delta,
    }


def append_segment_with_crossfade(out: List[int], segment: List[int], fade_samples: int) -> None:
    """Append a repeated loop segment while smoothing the seam.

    The overlap removes the click/pop that appears when the last sample of one
    loop copy jumps to the first sample of the next copy.
    """
    if not segment:
        return
    fade = max(0, int(fade_samples))
    if fade <= 0 or len(out) < fade or len(segment) <= fade * 2:
        out.extend(segment)
        return
    fade = min(fade, len(out), len(segment) // 3)
    if fade <= 0:
        out.extend(segment)
        return
    for i in range(fade):
        t = (i + 1) / (fade + 1)
        # Equal-power-ish curve without imports/deps beyond math.
        a = math.cos(t * math.pi * 0.5)
        b = math.sin(t * math.pi * 0.5)
        out[-fade + i] = clamp16(round(out[-fade + i] * a + segment[i] * b))
    out.extend(segment[fade:])


def build_gapless_loop_preview_pcm(
    pcm_le_i16: bytes,
    start: int,
    end_excl: int,
    sample_rate: int,
    preview_seconds: int,
    *,
    declick: bool = True,
    crossfade_ms: float = 3.0,
    zero_cross: bool = True,
    trim_silence: bool = True,
) -> Tuple[bytes, Dict[str, int | float]]:
    """Create a single intro+loop-preview WAV body with no player-switch gap.

    This replaces the old Windows path that played intro.wav and then loop.wav
    separately. That separate PlaySound handoff was the source of the audible
    gap before the loop segment started.
    """
    samples = pcm16_bytes_to_list(pcm_le_i16)
    if not samples:
        return b"", {"preview_start": 0, "preview_end_exclusive": 0, "crossfade_samples": 0}
    rate = max(1, int(sample_rate))
    s, e, diag = resolve_preview_loop_bounds(
        pcm_le_i16,
        start,
        end_excl,
        zero_cross=bool(zero_cross and declick),
        trim_silence=bool(trim_silence),
    )
    intro = samples[:s]
    loop = samples[s:e]
    if not loop:
        return pcm_le_i16, {"preview_start": s, "preview_end_exclusive": e, "crossfade_samples": 0, **diag}
    target = max(len(samples), int(rate * max(1, int(preview_seconds))))
    out = list(intro)
    fade = 0
    if declick:
        fade = int(round(rate * max(0.0, float(crossfade_ms)) / 1000.0))
        fade = max(0, min(fade, max(0, len(loop) // 4), 2048))
    # First append is direct; intro->loop is contiguous in the source unless the
    # preview-only silence/zero-cross correction moved the start.
    out.extend(loop)
    while len(out) < target:
        append_segment_with_crossfade(out, loop, fade)
    if len(out) > target:
        out = out[:target]
    diag.update({
        "preview_start": s,
        "preview_end_exclusive": e,
        "crossfade_samples": fade,
        "crossfade_ms_effective": (fade / rate) * 1000.0,
        "target_samples": target,
    })
    return i16_list_to_pcm16_bytes(out), diag


# MLT parsing and repacking


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


class VerticalScrolledFrame(ttk.Frame):
    """Scrollable control panel whose vertical bar appears only when required."""

    def __init__(self, master, *, padding=0):
        super().__init__(master)
        background = ttk.Style(self).lookup("TFrame", "background")
        if not background:
            background = self.winfo_toplevel().cget("background")

        self.canvas = tk.Canvas(
            self,
            highlightthickness=0,
            borderwidth=0,
            background=background,
        )
        # A classic Tk scrollbar remains clearly visible with Windows themes.
        self.scrollbar = tk.Scrollbar(
            self,
            orient=tk.VERTICAL,
            command=self.canvas.yview,
            width=16,
        )
        self.content = ttk.Frame(self.canvas, padding=padding)
        self._content_window = self.canvas.create_window(
            (0, 0), window=self.content, anchor=tk.NW
        )
        self._scrollbar_visible = False
        self._sync_job: Optional[str] = None

        self.canvas.configure(yscrollcommand=self._on_canvas_yview)
        self.canvas.grid(row=0, column=0, sticky=tk.NSEW)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.content.bind("<Configure>", self._schedule_scroll_sync, add="+")
        self.canvas.bind("<Configure>", self._on_canvas_configure, add="+")

        # Mouse-wheel events are global in Tk, but scrolling is restricted to
        # widgets that belong to this panel.
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self.canvas.bind_all("<Button-4>", self._on_mousewheel, add="+")
        self.canvas.bind_all("<Button-5>", self._on_mousewheel, add="+")
        self.after_idle(self._sync_scroll_state)

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self._content_window, width=max(1, event.width))
        self._schedule_scroll_sync()

    def _schedule_scroll_sync(self, _event=None) -> None:
        if self._sync_job is not None:
            try:
                self.after_cancel(self._sync_job)
            except tk.TclError:
                pass
        self._sync_job = self.after_idle(self._sync_scroll_state)

    def _sync_scroll_state(self) -> None:
        self._sync_job = None
        bbox = self.canvas.bbox("all")
        self.canvas.configure(scrollregion=bbox or (0, 0, 0, 0))

        content_height = 0 if bbox is None else max(0, int(bbox[3] - bbox[1]))
        viewport_height = max(1, int(self.canvas.winfo_height()))
        needs_scrollbar = content_height > viewport_height + 1

        if needs_scrollbar and not self._scrollbar_visible:
            self.scrollbar.grid(row=0, column=1, sticky=tk.NS)
            self._scrollbar_visible = True
        elif not needs_scrollbar and self._scrollbar_visible:
            self.scrollbar.grid_remove()
            self._scrollbar_visible = False
            self.canvas.yview_moveto(0.0)

    def _on_canvas_yview(self, first: str, last: str) -> None:
        self.scrollbar.set(first, last)

    def _contains_widget(self, widget) -> bool:
        current = widget
        while current is not None:
            if current in (self, self.canvas, self.content):
                return True
            current = getattr(current, "master", None)
        return False

    def _on_mousewheel(self, event):
        if not self._contains_widget(getattr(event, "widget", None)):
            return None
        if not self._scrollbar_visible:
            return None

        if getattr(event, "num", None) == 4:
            units = -1
        elif getattr(event, "num", None) == 5:
            units = 1
        else:
            delta = int(getattr(event, "delta", 0))
            if delta == 0:
                return None
            units = -max(1, abs(delta) // 120) if delta > 0 else max(1, abs(delta) // 120)

        self.canvas.yview_scroll(units, "units")
        return "break"


class MLTExplorerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("MLT Soundbank Explorer")
        self.root.geometry("1180x720")
        self.bank: Optional[MLTBank] = None
        self.current_temp_wav: Optional[Path] = None
        self._play_generation = 0
        self._preview_process: Optional[subprocess.Popen] = None
        self._background_active = False
        self._closing = False
        self._settings_refresh_job: Optional[str] = None
        self._ui_queue: queue.Queue = queue.Queue()
        self._ui_queue_job: Optional[str] = None
        self.status_var = tk.StringVar(value="Open an MLT file to begin.")
        self.filter_var = tk.StringVar()
        self.loop_preview_seconds_var = tk.IntVar(value=20)
        self.loop_declick_var = tk.BooleanVar(value=True)
        self.loop_zero_cross_var = tk.BooleanVar(value=True)
        self.loop_trim_silence_var = tk.BooleanVar(value=True)
        self.loop_crossfade_ms_var = tk.DoubleVar(value=3.0)
        self.pitch_correct_var = tk.BooleanVar(value=False)
        self.trigger_note_var = tk.IntVar(value=DEFAULT_TRIGGER_NOTE)
        self.preserve_bank_rate_var = tk.BooleanVar(value=True)
        self.detail_var = tk.StringVar(value="No sample selected.")
        self.view_filter_var = tk.StringVar(value="All samples")
        self.rate_filter_var = tk.StringVar(value="Any stored rate word")
        self.program_filter_var = tk.StringVar(value="All programs")
        self.pitch_preset_var = tk.StringVar(value="Base stored/2 rate")
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._start_ui_queue_pump()

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Open MLT...", command=self.open_mlt, accelerator="Ctrl+O")
        file_menu.add_command(label="Save Repacked As...", command=self.save_as, accelerator="Ctrl+S")
        file_menu.add_separator()
        file_menu.add_command(label="Load Editor Project JSON...", command=self.load_project_json)
        file_menu.add_command(label="Save Editor Project JSON...", command=self.save_project_json)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.destroy)
        menubar.add_cascade(label="File", menu=file_menu)

        preview_menu = tk.Menu(menubar, tearoff=False)
        preview_menu.add_command(label="Preview Selected", command=self.preview_selected, accelerator="Space")
        preview_menu.add_command(label="Preview Selected in Loop Mode", command=self.preview_loop_selected)
        preview_menu.add_command(label="Stop Preview", command=self.stop_preview, accelerator="Esc")
        preview_menu.add_separator()
        preview_menu.add_checkbutton(label="Gapless/de-click loop preview", variable=self.loop_declick_var)
        preview_menu.add_checkbutton(label="Snap preview loop to nearby zero-crossing", variable=self.loop_zero_cross_var)
        preview_menu.add_checkbutton(label="Trim silent head inside loop preview", variable=self.loop_trim_silence_var)
        preview_menu.add_separator()
        preview_menu.add_radiobutton(label="Base sample-rate (stored / 2)", variable=self.pitch_preset_var, value="Base stored/2 rate", command=self.apply_pitch_preset)
        preview_menu.add_radiobutton(label="Game pitch: C4 / note 60", variable=self.pitch_preset_var, value="Game C4 / 60", command=self.apply_pitch_preset)
        preview_menu.add_radiobutton(label="Game pitch: C3 / note 48", variable=self.pitch_preset_var, value="Game C3 / 48", command=self.apply_pitch_preset)
        preview_menu.add_radiobutton(label="Custom trigger note", variable=self.pitch_preset_var, value="Custom", command=self.apply_pitch_preset)
        menubar.add_cascade(label="Preview", menu=preview_menu)

        export_menu = tk.Menu(menubar, tearoff=False)
        export_menu.add_command(label="Export Selected WAV...", command=self.export_selected)
        export_menu.add_command(label="Export Selected Loop Preview WAV...", command=self.export_loop_preview_selected)
        export_menu.add_command(label="Export All WAV...", command=self.export_all)
        export_menu.add_separator()
        export_menu.add_command(label="Export Selected Raw DSP Payload...", command=self.export_selected_raw_payload)
        export_menu.add_command(label="Export All Raw DSP Payloads...", command=self.export_all_raw_payloads)
        export_menu.add_separator()
        export_menu.add_command(label="Save Aliases CSV...", command=self.save_aliases)
        export_menu.add_command(label="Save Loop Report CSV...", command=self.save_loop_report)
        export_menu.add_command(label="Save Program Map CSV...", command=self.save_program_map_csv)
        export_menu.add_command(label="Save Bank Tree JSON...", command=self.save_bank_tree_json)
        export_menu.add_command(label="Save Replacement Manifest Template CSV...", command=self.save_replacement_manifest_template)
        menubar.add_cascade(label="Export", menu=export_menu)

        edit_menu = tk.Menu(menubar, tearoff=False)
        edit_menu.add_command(label="Replace Selected From WAV...", command=self.replace_selected)
        edit_menu.add_command(label="Batch Replace From Folder...", command=self.batch_replace_from_folder_gui)
        edit_menu.add_command(label="Clear Selected Replacement", command=self.clear_replacement)
        edit_menu.add_separator()
        edit_menu.add_command(label="Rename Alias...", command=self.rename_alias)
        menubar.add_cascade(label="Edit", menu=edit_menu)

        view_menu = tk.Menu(menubar, tearoff=False)
        for label in ["All samples", "Looped only", "One-shot only", "Mapped only", "Unmapped only", "Replaced only", "No replacement"]:
            view_menu.add_radiobutton(label=label, variable=self.view_filter_var, value=label, command=self.refresh_tree)
        view_menu.add_separator()
        view_menu.add_command(label="Show Program Map...", command=self.show_program_map)
        view_menu.add_command(label="Show Selected Layer / Hex Details...", command=self.show_selected_layer_fields)
        menubar.add_cascade(label="View", menu=view_menu)

        reports_menu = tk.Menu(menubar, tearoff=False)
        reports_menu.add_command(label="Run Complete Parameter Forensics...", command=self.save_parameter_forensics)
        reports_menu.add_separator()
        reports_menu.add_command(label="Run Deep Audit...", command=self.save_deep_audit)
        reports_menu.add_command(label="Run Sample-Rate Forensics...", command=self.save_samplerate_forensics)
        reports_menu.add_command(label="Validate Repack Plan...", command=self.validate_repack_plan_gui)
        reports_menu.add_command(label="Save Validation Report CSV...", command=self.save_validation_report_csv)
        reports_menu.add_command(label="Save Loop Preview Seam Report CSV...", command=self.save_loop_preview_seam_report_csv)
        reports_menu.add_command(label="Show Reverse Engineering Summary...", command=self.show_reverse_summary)
        menubar.add_cascade(label="Reports", menu=reports_menu)

        self.root.config(menu=menubar)
        self.root.bind("<Control-o>", lambda _e: self.open_mlt())
        self.root.bind("<Control-s>", lambda _e: self.save_as())
        self.root.bind("<space>", lambda _e: self.preview_selected())
        self.root.bind("<Escape>", lambda _e: self.stop_preview())

    def _build_ui(self) -> None:
        self._build_menu()
        top = ttk.Frame(self.root, padding=8)
        top.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(top, text="Open MLT", command=self.open_mlt).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(top, text="Save Repacked As", command=self.save_as).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(top, text="Export All WAV", command=self.export_all).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(top, text="Batch Replace", command=self.batch_replace_from_folder_gui).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(top, text="Validate", command=self.validate_repack_plan_gui).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(top, text="Save Aliases CSV", command=self.save_aliases).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(top, text="Save Loop Report CSV", command=self.save_loop_report).pack(side=tk.LEFT, padx=(0, 16))

        ttk.Label(top, text="Filter:").pack(side=tk.LEFT)
        filter_entry = ttk.Entry(top, textvariable=self.filter_var, width=32)
        filter_entry.pack(side=tk.LEFT, padx=(4, 6))
        filter_entry.bind("<KeyRelease>", lambda _e: self.refresh_tree())
        ttk.Button(top, text="Clear", command=lambda: (self.filter_var.set(""), self.refresh_tree())).pack(side=tk.LEFT, padx=(0, 12))

        ttk.Label(top, text="View:").pack(side=tk.LEFT)
        view_combo = ttk.Combobox(
            top,
            textvariable=self.view_filter_var,
            state="readonly",
            width=15,
            values=("All samples", "Looped only", "One-shot only", "Mapped only", "Unmapped only", "Replaced only", "No replacement"),
        )
        view_combo.pack(side=tk.LEFT, padx=(4, 8))
        view_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_tree())

        ttk.Label(top, text="Rate word:").pack(side=tk.LEFT)
        rate_combo = ttk.Combobox(
            top,
            textvariable=self.rate_filter_var,
            state="readonly",
            width=14,
            values=(
                "Any stored rate word",
                "15592 word (7796 Hz base)",
                "31183 word (15591.5 Hz base)",
                "33038 word (16519 Hz base)",
                "44100 word (22050 Hz base)",
                "62367 word (31183.5 Hz base)",
            ),
        )
        rate_combo.pack(side=tk.LEFT, padx=(4, 8))
        rate_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_tree())

        ttk.Label(top, text="Program filter:").pack(side=tk.LEFT)
        self.program_combo = ttk.Combobox(
            top,
            textvariable=self.program_filter_var,
            state="readonly",
            width=14,
            values=("All programs",),
        )
        self.program_combo.pack(side=tk.LEFT, padx=(4, 0))
        self.program_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_tree())

        mid = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        mid.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        left = ttk.Frame(mid)
        right_host = ttk.Frame(mid)
        self.details_scroller = VerticalScrolledFrame(right_host, padding=8)
        self.details_scroller.pack(fill=tk.BOTH, expand=True)
        right = self.details_scroller.content
        mid.add(left, weight=4)
        mid.add(right_host, weight=2)

        columns = ("idx", "alias", "bank_rate", "wav_rate", "dur", "loop", "samples", "offset", "usage", "replacement")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        headers = {
            "idx": ("#", 50),
            "alias": ("Alias / recovered name", 285),
            "bank_rate": ("Rate word", 90),
            "wav_rate": ("WAV Hz", 75),
            "dur": ("Sec", 70),
            "loop": ("Loop", 55),
            "samples": ("Samples", 85),
            "offset": ("Offset", 90),
            "usage": ("Usage", 165),
            "replacement": ("Replacement WAV", 180),
        }
        for key, (label, width) in headers.items():
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor=tk.W if key in ("alias", "usage", "replacement") else tk.CENTER)
        yscroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.update_details())
        self.tree.bind("<Double-1>", lambda _e: self.preview_selected())
        self.tree.bind("<Button-3>", self.show_context_menu)
        self._build_context_menu()

        detail_label = ttk.Label(right, textvariable=self.detail_var, justify=tk.LEFT, wraplength=390)
        detail_label.pack(anchor=tk.NW, fill=tk.X, pady=(0, 12))

        ttk.Button(right, text="Preview", command=self.preview_selected).pack(fill=tk.X, pady=3)
        ttk.Button(right, text="Preview Loop Mode", command=self.preview_loop_selected).pack(fill=tk.X, pady=3)

        loop_row = ttk.Frame(right)
        loop_row.pack(fill=tk.X, pady=3)
        ttk.Label(loop_row, text="Loop preview sec:").pack(side=tk.LEFT)
        ttk.Spinbox(loop_row, from_=3, to=120, width=6, textvariable=self.loop_preview_seconds_var).pack(side=tk.RIGHT)

        fade_row = ttk.Frame(right)
        fade_row.pack(fill=tk.X, pady=3)
        ttk.Label(fade_row, text="Loop crossfade ms:").pack(side=tk.LEFT)
        ttk.Spinbox(fade_row, from_=0.0, to=20.0, increment=0.5, width=6, textvariable=self.loop_crossfade_ms_var).pack(side=tk.RIGHT)

        ttk.Checkbutton(right, text="Gapless/de-click loop preview", variable=self.loop_declick_var).pack(fill=tk.X, pady=2)
        ttk.Checkbutton(right, text="Zero-cross loop preview bounds", variable=self.loop_zero_cross_var).pack(fill=tk.X, pady=2)
        ttk.Checkbutton(right, text="Trim silent loop head in preview", variable=self.loop_trim_silence_var).pack(fill=tk.X, pady=2)

        ttk.Checkbutton(
            right,
            text="Game-pitch preview/export WAV",
            variable=self.pitch_correct_var,
            command=self.refresh_tree,
        ).pack(fill=tk.X, pady=3)

        note_row = ttk.Frame(right)
        note_row.pack(fill=tk.X, pady=3)
        ttk.Label(note_row, text="Trigger note:").pack(side=tk.LEFT)
        ttk.Spinbox(note_row, from_=0, to=127, width=6, textvariable=self.trigger_note_var, command=self.refresh_tree).pack(side=tk.LEFT, padx=4)
        ttk.Label(note_row, text="default C4 / 60").pack(side=tk.LEFT)

        preset_row = ttk.Frame(right)
        preset_row.pack(fill=tk.X, pady=3)
        ttk.Label(preset_row, text="Pitch preset:").pack(side=tk.LEFT)
        preset_combo = ttk.Combobox(
            preset_row,
            textvariable=self.pitch_preset_var,
            state="readonly",
            width=18,
            values=("Base stored/2 rate", "Game C4 / 60", "Game C3 / 48", "Custom"),
        )
        preset_combo.pack(side=tk.RIGHT)
        preset_combo.bind("<<ComboboxSelected>>", lambda _e: self.apply_pitch_preset())
        ttk.Checkbutton(
            right,
            text="Preserve original bank pitch on replace",
            variable=self.preserve_bank_rate_var,
        ).pack(fill=tk.X, pady=3)

        ttk.Button(right, text="Stop Preview", command=self.stop_preview).pack(fill=tk.X, pady=3)
        ttk.Separator(right).pack(fill=tk.X, pady=8)
        ttk.Button(right, text="Export Selected WAV", command=self.export_selected).pack(fill=tk.X, pady=3)
        ttk.Button(right, text="Export Loop Preview WAV", command=self.export_loop_preview_selected).pack(fill=tk.X, pady=3)
        ttk.Button(right, text="Replace Selected From WAV", command=self.replace_selected).pack(fill=tk.X, pady=3)
        ttk.Button(right, text="Clear Selected Replacement", command=self.clear_replacement).pack(fill=tk.X, pady=3)
        ttk.Button(right, text="Batch Replace From Folder", command=self.batch_replace_from_folder_gui).pack(fill=tk.X, pady=3)
        ttk.Button(right, text="Validate Repack Plan", command=self.validate_repack_plan_gui).pack(fill=tk.X, pady=3)
        ttk.Separator(right).pack(fill=tk.X, pady=8)
        ttk.Button(right, text="Rename Alias", command=self.rename_alias).pack(fill=tk.X, pady=3)
        ttk.Button(right, text="Show Program Map", command=self.show_program_map).pack(fill=tk.X, pady=3)
        ttk.Button(right, text="Selected Layer / Hex Details", command=self.show_selected_layer_fields).pack(fill=tk.X, pady=3)
        ttk.Button(right, text="Run Deep Audit", command=self.save_deep_audit).pack(fill=tk.X, pady=3)

        help_text = (
            "Workflow:\n"
            "1. Open .mlt.\n"
            "2. Export/preview samples.\n"
            "3. Replace entries with PCM WAV files.\n"
            "4. Save Repacked As.\n\n"
            "Always keep the original MLT as backup. The tool rebuilds MPBW and updates MPBP offsets automatically. Menus at the top expose extra export/audit/report actions. Loop preview is one gapless file with optional zero-cross/crossfade de-clicking. Game-pitch export uses stored sample-rate / 2 + split root key + trigger note. The default pitch preset is Base stored/2 rate."
        )
        help_label = ttk.Label(right, text=help_text, justify=tk.LEFT, wraplength=390)
        help_label.pack(anchor=tk.SW, fill=tk.X, pady=(16, 0))

        def update_right_panel_wrap(event) -> None:
            wrap_width = max(180, int(event.width) - 36)
            detail_label.configure(wraplength=wrap_width)
            help_label.configure(wraplength=wrap_width)

        self.details_scroller.canvas.bind("<Configure>", update_right_panel_wrap, add="+")
        self.details_scroller.canvas.yview_moveto(0.0)

        bottom = ttk.Frame(self.root, padding=(8, 4))
        bottom.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Label(bottom, textvariable=self.status_var).pack(side=tk.LEFT, fill=tk.X, expand=True)

    def apply_pitch_preset(self) -> None:
        preset = self.pitch_preset_var.get()
        if preset == "Game C3 / 48":
            self.pitch_correct_var.set(True)
            self.trigger_note_var.set(48)
        elif preset == "Game C4 / 60":
            self.pitch_correct_var.set(True)
            self.trigger_note_var.set(60)
        elif preset == "Base stored/2 rate":
            self.pitch_correct_var.set(False)
        self.stop_preview(silent=True)
        self._schedule_settings_refresh()

    def _schedule_settings_refresh(self) -> None:
        """Coalesce quick setting changes into one tree/detail refresh."""
        if self._closing:
            return
        if self._settings_refresh_job is not None:
            try:
                self.root.after_cancel(self._settings_refresh_job)
            except tk.TclError:
                pass
        self._settings_refresh_job = self.root.after_idle(self._refresh_after_settings_change)

    def _refresh_after_settings_change(self) -> None:
        self._settings_refresh_job = None
        if self._closing:
            return
        self.refresh_tree()
        self.update_details()

    def _start_ui_queue_pump(self) -> None:
        if self._closing or self._ui_queue_job is not None:
            return
        self._ui_queue_job = self.root.after(30, self._drain_ui_queue)

    def _post_ui(self, callback, *args) -> None:
        self._ui_queue.put((callback, args))

    def _drain_ui_queue(self) -> None:
        self._ui_queue_job = None
        if self._closing:
            return
        while True:
            try:
                callback, args = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            callback(*args)
        self._ui_queue_job = self.root.after(30, self._drain_ui_queue)

    def _on_close(self) -> None:
        self._closing = True
        if self._ui_queue_job is not None:
            try:
                self.root.after_cancel(self._ui_queue_job)
            except tk.TclError:
                pass
            self._ui_queue_job = None
        try:
            self.stop_preview(silent=True)
        finally:
            self.root.destroy()

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
            "tool": "MLT Soundbank Explorer bank tree",
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

    def open_mlt(self) -> None:
        path = filedialog.askopenfilename(title="Open MLT", filetypes=[("MLT files", "*.mlt"), ("All files", "*.*")])
        if not path:
            return
        try:
            bank = MLTBank(Path(path))
            # Load sidecar aliases when present.
            for candidate in [Path(path).with_suffix(".aliases.csv"), Path(path).with_name(Path(path).stem + "_aliases.csv")]:
                if candidate.exists():
                    bank.load_alias_csv(candidate)
                    break
            self.bank = bank
            self.update_program_filter_values()
            self.refresh_tree()
            self.status_var.set(f"Opened {Path(path).name}: {len(bank.samples)} samples. {bank.no_embedded_names_report()}")
        except Exception as exc:
            messagebox.showerror("Open failed", str(exc))

    def selected_index(self) -> Optional[int]:
        item = self.tree.focus()
        if not item:
            return None
        try:
            return int(self.tree.item(item, "values")[0])
        except Exception:
            return None

    def refresh_tree(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        if not self.bank:
            return
        f = self.filter_var.get().strip().lower()
        for s in self.bank.samples:
            usage = ";".join(s.usage[:5]) + ("…" if len(s.usage) > 5 else "")
            audition_rate = self.bank.audition_sample_rate(s.index, pitch_correct=self.pitch_correct_var.get(), trigger_note=int(self.trigger_note_var.get()))
            values = (
                s.index,
                s.alias,
                s.current_sample_rate,
                audition_rate,
                f"{s.current_sample_count / audition_rate:.3f}" if audition_rate else "0.000",
                "yes" if s.loop_flag else "no",
                s.current_sample_count,
                f"0x{s.data_offset:06X}",
                usage,
                s.replacement_label,
            )
            hay = " ".join(str(v).lower() for v in values)
            if f and f not in hay:
                continue
            view_mode = self.view_filter_var.get()
            if view_mode == "Looped only" and not s.loop_flag:
                continue
            if view_mode == "One-shot only" and s.loop_flag:
                continue
            if view_mode == "Mapped only" and not s.usage:
                continue
            if view_mode == "Unmapped only" and s.usage:
                continue
            if view_mode == "Replaced only" and not s.replacement:
                continue
            if view_mode == "No replacement" and s.replacement:
                continue
            rate_mode = self.rate_filter_var.get()
            if rate_mode != "Any stored rate word":
                try:
                    wanted_rate = int(rate_mode.split()[0])
                except Exception:
                    wanted_rate = None
                if wanted_rate is not None and s.current_sample_rate != wanted_rate:
                    continue
            program_mode = self.program_filter_var.get()
            if program_mode != "All programs":
                program_code = program_mode.split()[0]
                if not any(u.startswith(program_code) for u in s.usage):
                    continue
            self.tree.insert("", tk.END, values=values)
        self.update_details()

    def update_details(self) -> None:
        if not self.bank:
            self.detail_var.set("No MLT loaded.")
            return
        idx = self.selected_index()
        if idx is None:
            self.detail_var.set("No sample selected.")
            return
        s = self.bank.samples[idx]
        trigger_note = int(self.trigger_note_var.get())
        corrected_rate, semis, rate_note = self.bank.rate_correction(idx, trigger_note=trigger_note)
        audition_rate = self.bank.audition_sample_rate(idx, pitch_correct=self.pitch_correct_var.get(), trigger_note=trigger_note)
        loop_report = self.bank.loop_point_report(idx, trigger_note=trigger_note)
        lines = [
            f"Sample #{s.index}",
            f"Alias: {s.alias}",
            f"Stored MPBP rate word: {s.current_sample_rate}",
            f"Base sample rate (stored / 2): {s.current_base_sample_rate_exact:.3f} Hz",
            f"Preview/export rate: {audition_rate} Hz",
            f"Root key(s): {', '.join(str(r) + '/' + note_name(r) for r in s.root_keys) if s.root_keys else '-'}",
            f"Trigger note: {trigger_note}/{note_name(trigger_note)}",
            f"Game pitch shift: {semis:+d} semitone(s) ({rate_note})",
            f"Duration at preview/export rate: {s.current_sample_count / audition_rate:.6f} s" if audition_rate else "Duration: 0 s",
            f"Samples: {s.current_sample_count}",
            f"Loop: {'yes' if s.loop_flag else 'no'}",
            f"Loop start addr/sample: {loop_report['loop_start_addr_hex']} / {loop_report['loop_start_sample']}" if loop_report else "Loop start addr/sample: -",
            f"Loop end addr/sample inclusive: {loop_report['loop_end_addr_hex']} / {loop_report['loop_end_sample_inclusive']}" if loop_report else "Loop end addr/sample inclusive: -",
            f"Current loop region [start, end): {self.bank.loop_points_samples(idx) if s.loop_flag else '-'}",
            f"Original MPBW offset: 0x{s.data_offset:06X}",
            f"Format: fmt={s.fmt}, type=0x{s.type_byte:02X}",
            f"Usage: {', '.join(s.usage) if s.usage else 'not mapped'}",
        ]
        if s.replacement:
            lines += [
                "",
                f"Replacement: {s.replacement.wav_path.name}",
                f"Source WAV rate: {s.replacement.source_wav_rate} Hz",
                f"Encoded content rate: {s.replacement.content_sample_rate} Hz",
                f"Stored rate word written: {s.replacement.sample_rate} (base {s.replacement.sample_rate / MLT_RATE_WORD_DIVISOR:.3f} Hz)",
                f"Replacement loop start sample: {s.replacement.loop_start_sample if s.loop_flag else '-'}",
                f"Encoded bytes: {len(s.replacement.encoded_payload)}",
                f"DSP encode-back RMS / peak error: {s.replacement.encode_rms_error:.1f} / {s.replacement.encode_peak_error}",
            ]
        self.detail_var.set("\n".join(lines))

    def _run_background(self, title: str, func) -> None:
        """Run file and codec work off the UI thread."""
        if self._background_active:
            self.status_var.set("Another background task is still running.")
            return

        self._background_active = True
        self.status_var.set(f"{title}...")

        def finish_ok() -> None:
            self._background_active = False
            if not self._closing:
                self.status_var.set(f"{title} done.")

        def finish_error(message: str) -> None:
            self._background_active = False
            if not self._closing:
                self.status_var.set(f"{title} failed.")
                messagebox.showerror(title, message)

        def worker() -> None:
            try:
                func()
            except Exception as exc:
                self._post_ui(finish_error, str(exc))
                return
            self._post_ui(finish_ok)

        threading.Thread(target=worker, daemon=True, name=f"MLT-{title}").start()

    def preview_selected(self) -> None:
        if not self.bank:
            return
        idx = self.selected_index()
        if idx is None:
            return
        try:
            self.stop_preview(silent=True)
            s = self.bank.samples[idx]
            pcm = self.bank.decode_sample(idx)
            tmp = Path(tempfile.gettempdir()) / f"mlt_preview_{os.getpid()}_{idx:03d}.wav"
            write_wav(tmp, pcm, self.bank.audition_sample_rate(idx, pitch_correct=self.pitch_correct_var.get(), trigger_note=int(self.trigger_note_var.get())))
            self.current_temp_wav = tmp
            self._play_wav(tmp)
            self.status_var.set(f"Previewing sample {idx}: {s.alias}")
        except Exception as exc:
            messagebox.showerror("Preview failed", str(exc))

    def preview_loop_selected(self) -> None:
        if not self.bank:
            return
        idx = self.selected_index()
        if idx is None:
            return
        try:
            s = self.bank.samples[idx]
            points = self.bank.loop_points_samples(idx)
            if not points:
                messagebox.showinfo("Loop preview", "This sample has no loop flag / loop points.")
                return
            self.stop_preview(silent=True)
            start, end_excl = points
            pcm = self.bank.decode_sample(idx)
            preview_seconds = int(self.loop_preview_seconds_var.get() or 20)
            self._play_generation += 1
            generation = self._play_generation

            # Build one WAV so playback does not pause at the loop handoff.
            loop_pcm = self.bank.build_loop_preview_pcm(
                idx,
                preview_seconds,
                pitch_correct=self.pitch_correct_var.get(),
                trigger_note=int(self.trigger_note_var.get()),
                declick=self.loop_declick_var.get(),
                crossfade_ms=float(self.loop_crossfade_ms_var.get() or 0.0),
                zero_cross=self.loop_zero_cross_var.get(),
                trim_silence=self.loop_trim_silence_var.get(),
            )
            diag = self.bank.loop_preview_diagnostics(
                idx,
                preview_seconds,
                pitch_correct=self.pitch_correct_var.get(),
                trigger_note=int(self.trigger_note_var.get()),
                declick=self.loop_declick_var.get(),
                crossfade_ms=float(self.loop_crossfade_ms_var.get() or 0.0),
                zero_cross=self.loop_zero_cross_var.get(),
                trim_silence=self.loop_trim_silence_var.get(),
            )
            tmp = Path(tempfile.gettempdir()) / f"mlt_preview_{os.getpid()}_{idx:03d}_gapless_loop_{preview_seconds}s.wav"
            write_wav(tmp, loop_pcm, self.bank.audition_sample_rate(idx, pitch_correct=self.pitch_correct_var.get(), trigger_note=int(self.trigger_note_var.get())))
            self.current_temp_wav = tmp
            self._play_wav(tmp)
            self.status_var.set(
                f"Gapless loop preview sample {idx}: start {diag.get('preview_start', start)}, "
                f"loop {int(diag.get('preview_end_exclusive', end_excl)) - int(diag.get('preview_start', start))} samples, "
                f"crossfade {diag.get('crossfade_samples', 0)} samples, {preview_seconds}s."
            )
        except Exception as exc:
            messagebox.showerror("Loop preview failed", str(exc))

    def _play_wav(self, wav_path: Path) -> None:
        system = platform.system().lower()
        if system == "windows":
            import winsound
            winsound.PlaySound(str(wav_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
            return
        if system == "darwin":
            self._preview_process = subprocess.Popen(["afplay", str(wav_path)])
            return
        opener = shutil.which("aplay") or shutil.which("xdg-open")
        if opener:
            self._preview_process = subprocess.Popen([opener, str(wav_path)])
        else:
            messagebox.showinfo("Preview", f"WAV written to:\n{wav_path}")

    def stop_preview(self, silent: bool = False) -> None:
        self._play_generation += 1
        if platform.system().lower() == "windows":
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        process = self._preview_process
        self._preview_process = None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        if not silent:
            self.status_var.set("Preview stopped.")

    def export_selected(self) -> None:
        if not self.bank:
            return
        idx = self.selected_index()
        if idx is None:
            return
        s = self.bank.samples[idx]
        safe_alias = "".join(c if c.isalnum() or c in "._-" else "_" for c in s.alias)
        audition_rate = self.bank.audition_sample_rate(idx, pitch_correct=self.pitch_correct_var.get(), trigger_note=int(self.trigger_note_var.get()))
        default = f"{idx:03d}_{safe_alias}_{audition_rate}Hz.wav"
        path = filedialog.asksaveasfilename(title="Export selected WAV", initialfile=default, defaultextension=".wav", filetypes=[("WAV", "*.wav")])
        if not path:
            return
        try:
            self.bank.export_sample(idx, Path(path), pitch_correct=self.pitch_correct_var.get(), trigger_note=int(self.trigger_note_var.get()))
            self.status_var.set(f"Exported {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))

    def export_loop_preview_selected(self) -> None:
        if not self.bank:
            return
        idx = self.selected_index()
        if idx is None:
            return
        s = self.bank.samples[idx]
        if not self.bank.loop_points_samples(idx):
            messagebox.showinfo("Export loop preview", "This sample has no loop flag / loop points.")
            return
        safe_alias = "".join(c if c.isalnum() or c in "._-" else "_" for c in s.alias)
        seconds = int(self.loop_preview_seconds_var.get() or 20)
        audition_rate = self.bank.audition_sample_rate(idx, pitch_correct=self.pitch_correct_var.get(), trigger_note=int(self.trigger_note_var.get()))
        default = f"{idx:03d}_{safe_alias}_{audition_rate}Hz_loopPreview_{seconds}s.wav"
        path = filedialog.asksaveasfilename(
            title="Export loop preview WAV",
            initialfile=default,
            defaultextension=".wav",
            filetypes=[("WAV", "*.wav")],
        )
        if not path:
            return
        try:
            self.bank.export_loop_preview(
                idx, Path(path), seconds,
                pitch_correct=self.pitch_correct_var.get(),
                trigger_note=int(self.trigger_note_var.get()),
                declick=self.loop_declick_var.get(),
                crossfade_ms=float(self.loop_crossfade_ms_var.get() or 0.0),
                zero_cross=self.loop_zero_cross_var.get(),
                trim_silence=self.loop_trim_silence_var.get(),
            )
            self.status_var.set(f"Exported loop preview {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Export loop preview failed", str(exc))

    def export_all(self) -> None:
        if not self.bank:
            return
        out = filedialog.askdirectory(title="Export all WAV files")
        if not out:
            return
        bank = self.bank
        pitch_correct = bool(self.pitch_correct_var.get())
        trigger_note = int(self.trigger_note_var.get())
        self._run_background("Export all", lambda: bank.export_all(Path(out), pitch_correct=pitch_correct, trigger_note=trigger_note))

    def replace_selected(self) -> None:
        if not self.bank:
            return
        idx = self.selected_index()
        if idx is None:
            return
        path = filedialog.askopenfilename(title="Choose replacement WAV", filetypes=[("WAV", "*.wav"), ("All files", "*.*")])
        if not path:
            return

        bank = self.bank
        preserve_bank_rate = bool(self.preserve_bank_rate_var.get())
        pitch_correct = bool(self.pitch_correct_var.get())
        trigger_note = int(self.trigger_note_var.get())

        def work():
            bank.replace_from_wav(
                idx,
                Path(path),
                preserve_loop_ratio=True,
                preserve_bank_rate=preserve_bank_rate,
                auto_resample_to_audition=True,
                pitch_correct=pitch_correct,
                trigger_note=trigger_note,
            )
            self._post_ui(self.refresh_tree)
            self._post_ui(self.update_details)

        self._run_background(f"Encoding replacement for sample {idx}", work)

    def clear_replacement(self) -> None:
        if not self.bank:
            return
        idx = self.selected_index()
        if idx is None:
            return
        self.bank.clear_replacement(idx)
        self.refresh_tree()
        self.status_var.set(f"Cleared replacement for sample {idx}")

    def save_as(self) -> None:
        if not self.bank:
            return
        default = self.bank.path.with_name(self.bank.path.stem + "_repacked.mlt").name
        path = filedialog.asksaveasfilename(title="Save repacked MLT", initialfile=default, defaultextension=".mlt", filetypes=[("MLT", "*.mlt"), ("All files", "*.*")])
        if not path:
            return
        self._run_background("Save repacked MLT", lambda: self.bank.save_as(Path(path)))

    def save_aliases(self) -> None:
        if not self.bank:
            return
        default = self.bank.path.with_name(self.bank.path.stem + "_aliases.csv").name
        path = filedialog.asksaveasfilename(title="Save aliases CSV", initialfile=default, defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            self.bank.write_alias_csv(Path(path))
            self.status_var.set(f"Saved aliases to {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Save aliases failed", str(exc))

    def save_loop_report(self) -> None:
        if not self.bank:
            return
        default = self.bank.path.with_name(self.bank.path.stem + "_loop_points.csv").name
        path = filedialog.asksaveasfilename(title="Save loop-point report CSV", initialfile=default, defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            self.bank.write_loop_report_csv(Path(path))
            self.status_var.set(f"Saved loop-point report to {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Save loop report failed", str(exc))

    def rename_alias(self) -> None:
        if not self.bank:
            return
        idx = self.selected_index()
        if idx is None:
            return
        s = self.bank.samples[idx]
        win = tk.Toplevel(self.root)
        win.title(f"Rename sample {idx}")
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text="Alias:").pack(padx=10, pady=(10, 2), anchor=tk.W)
        var = tk.StringVar(value=s.alias)
        ent = ttk.Entry(win, textvariable=var, width=58)
        ent.pack(padx=10, pady=4)
        ent.focus_set()

        def ok():
            new_alias = var.get().strip()
            if new_alias:
                s.alias = new_alias
                self.refresh_tree()
            win.destroy()

        btns = ttk.Frame(win)
        btns.pack(padx=10, pady=10, fill=tk.X)
        ttk.Button(btns, text="OK", command=ok).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side=tk.RIGHT)
        win.bind("<Return>", lambda _e: ok())
        win.bind("<Escape>", lambda _e: win.destroy())



    def _build_context_menu(self) -> None:
        self.context_menu = tk.Menu(self.root, tearoff=False)
        self.context_menu.add_command(label="Preview", command=self.preview_selected)
        self.context_menu.add_command(label="Preview Loop Mode", command=self.preview_loop_selected)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Export WAV...", command=self.export_selected)
        self.context_menu.add_command(label="Export Loop Preview WAV...", command=self.export_loop_preview_selected)
        self.context_menu.add_command(label="Export Raw DSP Payload...", command=self.export_selected_raw_payload)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Replace From WAV...", command=self.replace_selected)
        self.context_menu.add_command(label="Clear Replacement", command=self.clear_replacement)
        self.context_menu.add_command(label="Rename Alias...", command=self.rename_alias)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Layer / Hex Details...", command=self.show_selected_layer_fields)

    def show_context_menu(self, event) -> None:
        row_id = self.tree.identify_row(event.y)
        if row_id:
            self.tree.selection_set(row_id)
            self.tree.focus(row_id)
            self.update_details()
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def update_program_filter_values(self) -> None:
        values = ["All programs"]
        if self.bank:
            programs = sorted({u.split(".")[0] for s in self.bank.samples for u in s.usage})
            values += [f"{p} only" for p in programs]
        try:
            self.program_combo.configure(values=tuple(values))
        except Exception:
            pass
        if self.program_filter_var.get() not in values:
            self.program_filter_var.set("All programs")

    def export_selected_raw_payload(self) -> None:
        if not self.bank:
            return
        idx = self.selected_index()
        if idx is None:
            return
        s = self.bank.samples[idx]
        default = f"{idx:03d}_{self.bank.safe_alias(idx)}_DSPADPCM.bin"
        path = filedialog.asksaveasfilename(title="Export raw DSP-ADPCM payload", initialfile=default, defaultextension=".bin", filetypes=[("Binary", "*.bin"), ("All files", "*.*")])
        if not path:
            return
        try:
            self.bank.export_raw_payload(idx, Path(path), include_padding=False)
            self.status_var.set(f"Exported raw payload for sample {idx} to {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Raw export failed", str(exc))

    def export_all_raw_payloads(self) -> None:
        if not self.bank:
            return
        out = filedialog.askdirectory(title="Export all raw DSP-ADPCM payloads")
        if not out:
            return
        self._run_background("Export raw payloads", lambda: self.bank.export_all_raw_payloads(Path(out), include_padding=False))

    def save_replacement_manifest_template(self) -> None:
        if not self.bank:
            return
        default = self.bank.path.with_name(self.bank.path.stem + "_replacement_manifest_template.csv").name
        path = filedialog.asksaveasfilename(title="Save replacement manifest template CSV", initialfile=default, defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            self.bank.write_replacement_manifest_template(Path(path), trigger_note=int(self.trigger_note_var.get()))
            self.status_var.set(f"Saved replacement manifest template to {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Manifest template failed", str(exc))

    def validate_repack_plan_gui(self) -> None:
        if not self.bank:
            return
        rows = self.bank.validate_repack_plan(trigger_note=int(self.trigger_note_var.get()))
        errors = sum(1 for r in rows if r.get("area") != "summary" and r.get("severity") == "error")
        warnings = sum(1 for r in rows if r.get("area") != "summary" and r.get("severity") == "warning")
        win = tk.Toplevel(self.root)
        win.title(f"Repack validation: {errors} error(s), {warnings} warning(s)")
        win.geometry("980x560")
        cols = ("severity", "area", "index", "message", "detail")
        tree = ttk.Treeview(win, columns=cols, show="headings")
        widths = {"severity": 80, "area": 110, "index": 70, "message": 260, "detail": 430}
        for c in cols:
            tree.heading(c, text=c)
            tree.column(c, width=widths[c], anchor=tk.W if c in ("message", "detail") else tk.CENTER)
        y = ttk.Scrollbar(win, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=y.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        y.pack(side=tk.RIGHT, fill=tk.Y)
        for r in rows:
            if r.get("severity") in ("error", "warning", "ok") or r.get("area") in ("summary", "repack", "file"):
                tree.insert("", tk.END, values=(r.get("severity"), r.get("area"), r.get("index"), r.get("message"), r.get("detail")))
        self.status_var.set(f"Validation done: {errors} error(s), {warnings} warning(s)")

    def save_validation_report_csv(self) -> None:
        if not self.bank:
            return
        default = self.bank.path.with_name(self.bank.path.stem + "_validation.csv").name
        path = filedialog.asksaveasfilename(title="Save validation report CSV", initialfile=default, defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            self.bank.write_validation_report_csv(Path(path), trigger_note=int(self.trigger_note_var.get()))
            self.status_var.set(f"Saved validation report to {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Validation report failed", str(exc))

    def batch_replace_from_folder_gui(self) -> None:
        if not self.bank:
            return
        folder = filedialog.askdirectory(title="Choose folder containing replacement WAVs")
        if not folder:
            return

        bank = self.bank
        preserve_bank_rate = bool(self.preserve_bank_rate_var.get())
        pitch_correct = bool(self.pitch_correct_var.get())
        trigger_note = int(self.trigger_note_var.get())

        def work():
            rows = bank.batch_replace_from_folder(
                Path(folder),
                preserve_bank_rate=preserve_bank_rate,
                pitch_correct=pitch_correct,
                trigger_note=trigger_note,
            )
            ok = sum(1 for r in rows if r.get("status") == "ok")
            err = sum(1 for r in rows if r.get("status") == "error")
            report_path = Path(folder) / "mlt_batch_replace_report.csv"
            fields = sorted({k for r in rows for k in r.keys()}) or ["empty"]
            with report_path.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader(); writer.writerows(rows)
            self._post_ui(self.refresh_tree)
            self._post_ui(self.update_details)
            self._post_ui(self.status_var.set, f"Batch replace finished: {ok} ok, {err} error(s). Report: {report_path.name}")
        self._run_background("Batch replace", work)

    def save_loop_preview_seam_report_csv(self) -> None:
        if not self.bank:
            return
        default = self.bank.path.with_name(self.bank.path.stem + "_loop_preview_seam.csv").name
        path = filedialog.asksaveasfilename(
            title="Save loop preview seam report CSV",
            initialfile=default,
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
        )
        if not path:
            return
        try:
            self.bank.write_loop_preview_seam_report_csv(
                Path(path),
                int(self.loop_preview_seconds_var.get() or 20),
                pitch_correct=self.pitch_correct_var.get(),
                trigger_note=int(self.trigger_note_var.get()),
                declick=self.loop_declick_var.get(),
                crossfade_ms=float(self.loop_crossfade_ms_var.get() or 0.0),
                zero_cross=self.loop_zero_cross_var.get(),
                trim_silence=self.loop_trim_silence_var.get(),
            )
            self.status_var.set(f"Saved loop seam report to {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Save loop seam report failed", str(exc))

    def save_project_json(self) -> None:
        if not self.bank:
            return
        default = self.bank.path.with_name(self.bank.path.stem + "_mlt_project.json").name
        path = filedialog.asksaveasfilename(title="Save editor project JSON", initialfile=default, defaultextension=".json", filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            data = {
                "tool": "MLT Soundbank Explorer project",
                "mlt_path": str(self.bank.path),
                "settings": {
                    "pitch_correct": bool(self.pitch_correct_var.get()),
                    "trigger_note": int(self.trigger_note_var.get()),
                    "preserve_bank_rate": bool(self.preserve_bank_rate_var.get()),
                    "loop_preview_seconds": int(self.loop_preview_seconds_var.get()),
                    "loop_declick": bool(self.loop_declick_var.get()),
                    "loop_zero_cross": bool(self.loop_zero_cross_var.get()),
                    "loop_trim_silence": bool(self.loop_trim_silence_var.get()),
                    "loop_crossfade_ms": float(self.loop_crossfade_ms_var.get() or 0.0),
                    "clean_audition": bool(self.clean_audition_var.get()),
                    "fixed_render_rate": bool(self.fixed_render_rate_var.get()),
                    "render_rate": int(self.render_rate_var.get()),
                    "dc_filter": bool(self.dc_filter_var.get()),
                    "decrackle_filter": bool(self.decrackle_filter_var.get()),
                    "decrackle_strength": str(self.decrackle_strength_var.get()),
                    "limiter": bool(self.limiter_var.get()),
                    "edge_fade": bool(self.edge_fade_var.get()),
                },
                "samples": [
                    {
                        "index": s.index,
                        "alias": s.alias,
                        "replacement_wav": str(s.replacement.wav_path) if s.replacement else "",
                    }
                    for s in self.bank.samples
                ],
            }
            Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            self.status_var.set(f"Saved editor project to {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Save project failed", str(exc))

    def load_project_json(self) -> None:
        path = filedialog.askopenfilename(title="Load editor project JSON", filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            mlt_path = Path(data.get("mlt_path", ""))
            if self.bank is None:
                if not mlt_path.exists():
                    chosen = filedialog.askopenfilename(title="Project MLT missing; choose MLT", filetypes=[("MLT", "*.mlt"), ("All files", "*.*")])
                    if not chosen:
                        return
                    mlt_path = Path(chosen)
                self.bank = MLTBank(mlt_path)
            settings = data.get("settings", {})
            if "pitch_correct" in settings:
                self.pitch_correct_var.set(bool(settings["pitch_correct"]))
            if "trigger_note" in settings:
                self.trigger_note_var.set(int(settings["trigger_note"]))
            if "preserve_bank_rate" in settings:
                self.preserve_bank_rate_var.set(bool(settings["preserve_bank_rate"]))
            if "loop_preview_seconds" in settings:
                self.loop_preview_seconds_var.set(int(settings["loop_preview_seconds"]))
            if "loop_declick" in settings:
                self.loop_declick_var.set(bool(settings["loop_declick"]))
            if "loop_zero_cross" in settings:
                self.loop_zero_cross_var.set(bool(settings["loop_zero_cross"]))
            if "loop_trim_silence" in settings:
                self.loop_trim_silence_var.set(bool(settings["loop_trim_silence"]))
            if "loop_crossfade_ms" in settings:
                self.loop_crossfade_ms_var.set(float(settings["loop_crossfade_ms"]))
            if "clean_audition" in settings:
                self.clean_audition_var.set(bool(settings["clean_audition"]))
            if "fixed_render_rate" in settings:
                self.fixed_render_rate_var.set(bool(settings["fixed_render_rate"]))
            if "render_rate" in settings:
                self.render_rate_var.set(int(settings["render_rate"]))
            if "dc_filter" in settings:
                self.dc_filter_var.set(bool(settings["dc_filter"]))
            if "decrackle_filter" in settings:
                self.decrackle_filter_var.set(bool(settings["decrackle_filter"]))
            if "decrackle_strength" in settings:
                self.decrackle_strength_var.set(str(settings["decrackle_strength"]))
            if "limiter" in settings:
                self.limiter_var.set(bool(settings["limiter"]))
            if "edge_fade" in settings:
                self.edge_fade_var.set(bool(settings["edge_fade"]))
            by_idx = {s.index: s for s in self.bank.samples}
            samples = data.get("samples", [])
            for row in samples:
                try:
                    idx = int(row.get("index"))
                except Exception:
                    continue
                if idx in by_idx and row.get("alias"):
                    by_idx[idx].alias = str(row.get("alias"))

            bank = self.bank
            preserve_bank_rate = bool(self.preserve_bank_rate_var.get())
            pitch_correct = bool(self.pitch_correct_var.get())
            trigger_note = int(self.trigger_note_var.get())
            if not pitch_correct:
                self.pitch_preset_var.set("Base stored/2 rate")
            elif trigger_note == 60:
                self.pitch_preset_var.set("Game C4 / 60")
            elif trigger_note == 48:
                self.pitch_preset_var.set("Game C3 / 48")
            else:
                self.pitch_preset_var.set("Custom")

            def work():
                applied = 0; missing = 0; failed = 0
                for row in samples:
                    try:
                        idx = int(row.get("index"))
                    except Exception:
                        continue
                    rep = str(row.get("replacement_wav") or "").strip()
                    if not rep:
                        continue
                    rp = Path(rep)
                    if not rp.exists():
                        missing += 1; continue
                    try:
                        bank.replace_from_wav(
                            idx,
                            rp,
                            preserve_loop_ratio=True,
                            preserve_bank_rate=preserve_bank_rate,
                            auto_resample_to_audition=True,
                            pitch_correct=pitch_correct,
                            trigger_note=trigger_note,
                        )
                        applied += 1
                    except Exception:
                        failed += 1
                self._post_ui(self.update_program_filter_values)
                self._post_ui(self.refresh_tree)
                self._post_ui(self.status_var.set, f"Loaded project: aliases applied, replacements {applied} applied, {missing} missing, {failed} failed.")
            self._run_background("Load project", work)
        except Exception as exc:
            messagebox.showerror("Load project failed", str(exc))



# Audio cleanup and rendered export

DEFAULT_CLEAN_RENDER_RATE = 44100
CLEAN_RENDER_RATES = (22050, 32000, 44100, 48000)


def _cubic_interp_i16(y0: int, y1: int, y2: int, y3: int, t: float) -> int:
    # Catmull-Rom interpolation. Good enough for preview/export without scipy.
    a0 = -0.5 * y0 + 1.5 * y1 - 1.5 * y2 + 0.5 * y3
    a1 = y0 - 2.5 * y1 + 2.0 * y2 - 0.5 * y3
    a2 = -0.5 * y0 + 0.5 * y2
    a3 = y1
    return clamp16(round(((a0 * t + a1) * t + a2) * t + a3))


def resample_pcm16_cubic(pcm_le_i16: bytes, src_rate: int | float, dst_rate: int | float) -> bytes:
    """Mono PCM16 resampler for clean audition WAV output.

    The exact source rate can be fractional for this MLT family. Keeping one
    implementation for both integer and fractional ratios also avoids the old
    ``audioop.ratecv`` linear path, which was a noticeably poorer conversion
    for integer-rate files and is deprecated in current Python releases.
    """
    src_rate_f = float(src_rate)
    dst_rate_f = float(dst_rate)
    if src_rate_f <= 0 or dst_rate_f <= 0 or abs(src_rate_f - dst_rate_f) < 1e-9:
        return pcm_le_i16
    samples = pcm16_bytes_to_list(pcm_le_i16)
    if len(samples) < 4:
        return resample_pcm16_linear(pcm_le_i16, src_rate_f, dst_rate_f)
    new_len = max(1, int(round(len(samples) * dst_rate_f / src_rate_f)))
    if new_len == 1:
        return struct.pack("<h", samples[0])
    out: List[int] = []
    ratio = src_rate_f / dst_rate_f
    n = len(samples)
    for i in range(new_len):
        pos = i * ratio
        j = int(pos)
        t = pos - j
        j0 = max(0, j - 1)
        j1 = max(0, min(n - 1, j))
        j2 = max(0, min(n - 1, j + 1))
        j3 = max(0, min(n - 1, j + 2))
        out.append(_cubic_interp_i16(samples[j0], samples[j1], samples[j2], samples[j3], t))
    return i16_list_to_pcm16_bytes(out)


def audio_stats_from_pcm(pcm_le_i16: bytes) -> Dict[str, float | int]:
    samples = pcm16_bytes_to_list(pcm_le_i16)
    if not samples:
        return {"samples": 0, "peak": 0, "rms": 0.0, "dc_offset": 0.0, "clipped_samples": 0, "big_jump_count": 0, "max_jump": 0, "zero_crossings": 0}
    peak = max(abs(v) for v in samples)
    rms = math.sqrt(sum(v * v for v in samples) / len(samples))
    dc = sum(samples) / len(samples)
    clipped = sum(1 for v in samples if abs(v) >= 32760)
    max_jump = 0
    big_jumps = 0
    zero_cross = 0
    prev = samples[0]
    for v in samples[1:]:
        d = abs(v - prev)
        if d > max_jump:
            max_jump = d
        if d > 24000:
            big_jumps += 1
        if (prev < 0 <= v) or (prev > 0 >= v):
            zero_cross += 1
        prev = v
    return {"samples": len(samples), "peak": int(peak), "rms": float(rms), "dc_offset": float(dc), "clipped_samples": int(clipped), "big_jump_count": int(big_jumps), "max_jump": int(max_jump), "zero_crossings": int(zero_cross)}


def remove_dc_offset_samples(samples: List[int]) -> List[int]:
    if not samples:
        return samples
    dc = sum(samples) / len(samples)
    if abs(dc) < 1.0:
        return samples[:]
    return [clamp16(round(v - dc)) for v in samples]


def decrackle_spikes_samples(samples: List[int], strength: str = "Light") -> List[int]:
    """Conservative one-sample impulse repair.

    This does NOT try to denoise the whole sound. It only fixes isolated needle
    spikes where the middle sample is far from both neighbours while the
    neighbours agree with each other. That avoids killing gunshot/impact transients.
    """
    if len(samples) < 5:
        return samples[:]
    strength_l = str(strength or "Light").lower()
    if strength_l.startswith("off"):
        return samples[:]
    if strength_l.startswith("strong"):
        threshold = 9000
        neighbor_limit = 7000
    elif strength_l.startswith("medium"):
        threshold = 12000
        neighbor_limit = 9000
    else:
        threshold = 16000
        neighbor_limit = 11000
    out = samples[:]
    for i in range(2, len(samples) - 2):
        prev_v = samples[i - 1]
        cur = samples[i]
        next_v = samples[i + 1]
        pred = (prev_v + next_v) / 2.0
        if abs(cur - pred) > threshold and abs(prev_v - next_v) < neighbor_limit:
            # Require the surrounding trend to be calmer than the spike itself.
            left_jump = abs(samples[i - 1] - samples[i - 2])
            right_jump = abs(samples[i + 2] - samples[i + 1])
            if left_jump < threshold and right_jump < threshold:
                out[i] = clamp16(round(pred))
    return out


def apply_edge_fades_samples(samples: List[int], sample_rate: int, fade_ms: float = 0.35) -> List[int]:
    """Tiny edge fade to remove player start/stop clicks without dulling attacks."""
    if not samples or sample_rate <= 0 or fade_ms <= 0:
        return samples[:]
    n = len(samples)
    fade = int(round(sample_rate * fade_ms / 1000.0))
    fade = max(0, min(fade, n // 8, 64))
    if fade <= 1:
        return samples[:]
    out = samples[:]
    for i in range(fade):
        t = (i + 1) / fade
        out[i] = clamp16(round(out[i] * t))
        out[n - 1 - i] = clamp16(round(out[n - 1 - i] * t))
    return out


def apply_peak_limiter_samples(samples: List[int], ceiling: float = 0.965) -> List[int]:
    """Preview/export safety limiter. It scales peaks down; it does not alter MLT."""
    if not samples:
        return samples[:]
    ceiling = max(0.1, min(1.0, float(ceiling)))
    peak = max(abs(v) for v in samples)
    target = int(32767 * ceiling)
    if peak <= target or peak <= 0:
        return samples[:]
    gain = target / peak
    return [clamp16(round(v * gain)) for v in samples]


def clean_pcm16_for_audition(
    pcm_le_i16: bytes,
    input_rate: int,
    output_rate: int = DEFAULT_CLEAN_RENDER_RATE,
    *,
    fixed_output_rate: bool = True,
    dc_filter: bool = True,
    decrackle: bool = True,
    decrackle_strength: str = "Light",
    limiter: bool = True,
    edge_fade: bool = True,
) -> Tuple[bytes, int, Dict[str, float | int | str]]:
    """Render a player-friendly PCM WAV body for preview/export.

    Pipeline:
    1) keep the game-pitch duration, but optionally render to 44.1/48 kHz;
    2) remove tiny DC offset;
    3) repair isolated one-sample crackle spikes;
    4) apply a safety limiter so clipped peaks do not rasp in OS mixers;
    5) apply a sub-ms edge fade for start/stop clicks.
    """
    in_rate_exact = max(1.0, float(input_rate))
    out_rate = max(1, int(round(output_rate if fixed_output_rate else input_rate)))
    pre = audio_stats_from_pcm(pcm_le_i16)
    work = pcm_le_i16
    if fixed_output_rate and abs(float(out_rate) - in_rate_exact) > 1e-9:
        work = resample_pcm16_cubic(work, in_rate_exact, float(out_rate))
    samples = pcm16_bytes_to_list(work)
    if dc_filter:
        samples = remove_dc_offset_samples(samples)
    if decrackle:
        samples = decrackle_spikes_samples(samples, decrackle_strength)
    if limiter:
        samples = apply_peak_limiter_samples(samples, ceiling=0.965)
    if edge_fade:
        samples = apply_edge_fades_samples(samples, out_rate, fade_ms=0.35)
    cleaned = i16_list_to_pcm16_bytes(samples)
    post = audio_stats_from_pcm(cleaned)
    diag: Dict[str, float | int | str] = {f"pre_{k}": v for k, v in pre.items()}
    diag.update({f"post_{k}": v for k, v in post.items()})
    diag.update({"input_rate_exact": f"{in_rate_exact:.6f}", "input_rate_header": int(round(in_rate_exact)), "output_rate": out_rate, "fixed_output_rate": int(bool(fixed_output_rate)), "dc_filter": int(bool(dc_filter)), "decrackle": int(bool(decrackle)), "decrackle_strength": str(decrackle_strength), "limiter": int(bool(limiter)), "edge_fade": int(bool(edge_fade))})
    return cleaned, out_rate, diag


def _bank_render_sample_for_wav(
    self: MLTBank,
    index: int,
    *,
    pitch_correct: bool = False,
    trigger_note: int = DEFAULT_TRIGGER_NOTE,
    clean: bool = True,
    fixed_output_rate: bool = True,
    output_rate: int = DEFAULT_CLEAN_RENDER_RATE,
    dc_filter: bool = True,
    decrackle: bool = True,
    decrackle_strength: str = "Light",
    limiter: bool = True,
    edge_fade: bool = True,
) -> Tuple[bytes, int, Dict[str, float | int | str]]:
    pcm = self.decode_sample(index)
    src_rate_exact = self.audition_sample_rate_exact(index, pitch_correct=pitch_correct, trigger_note=trigger_note)
    src_rate_header = self.audition_sample_rate(index, pitch_correct=pitch_correct, trigger_note=trigger_note)
    if not clean:
        return pcm, src_rate_header, {"input_rate_exact": f"{src_rate_exact:.6f}", "input_rate_header": src_rate_header, "output_rate": src_rate_header, "clean": 0, **audio_stats_from_pcm(pcm)}
    pcm2, out_rate, diag = clean_pcm16_for_audition(
        pcm,
        src_rate_exact,
        output_rate=output_rate,
        fixed_output_rate=fixed_output_rate,
        dc_filter=dc_filter,
        decrackle=decrackle,
        decrackle_strength=decrackle_strength,
        limiter=limiter,
        edge_fade=edge_fade,
    )
    diag["clean"] = 1
    return pcm2, out_rate, diag


def _bank_render_loop_preview_for_wav(
    self: MLTBank,
    index: int,
    preview_seconds: int = 20,
    *,
    pitch_correct: bool = False,
    trigger_note: int = DEFAULT_TRIGGER_NOTE,
    loop_declick: bool = True,
    crossfade_ms: float = 3.0,
    zero_cross: bool = True,
    trim_silence: bool = True,
    clean: bool = True,
    fixed_output_rate: bool = True,
    output_rate: int = DEFAULT_CLEAN_RENDER_RATE,
    dc_filter: bool = True,
    decrackle: bool = True,
    decrackle_strength: str = "Light",
    limiter: bool = True,
    edge_fade: bool = True,
) -> Tuple[bytes, int, Dict[str, float | int | str]]:
    src_rate_exact = self.audition_sample_rate_exact(index, pitch_correct=pitch_correct, trigger_note=trigger_note)
    src_rate_header = self.audition_sample_rate(index, pitch_correct=pitch_correct, trigger_note=trigger_note)
    pcm = self.build_loop_preview_pcm(
        index,
        preview_seconds,
        pitch_correct=pitch_correct,
        trigger_note=trigger_note,
        declick=loop_declick,
        crossfade_ms=crossfade_ms,
        zero_cross=zero_cross,
        trim_silence=trim_silence,
    )
    if not clean:
        return pcm, src_rate_header, {"input_rate_exact": f"{src_rate_exact:.6f}", "input_rate_header": src_rate_header, "output_rate": src_rate_header, "clean": 0, **audio_stats_from_pcm(pcm)}
    pcm2, out_rate, diag = clean_pcm16_for_audition(
        pcm,
        src_rate_exact,
        output_rate=output_rate,
        fixed_output_rate=fixed_output_rate,
        dc_filter=dc_filter,
        decrackle=decrackle,
        decrackle_strength=decrackle_strength,
        limiter=limiter,
        edge_fade=edge_fade,
    )
    diag["clean"] = 1
    return pcm2, out_rate, diag


def _bank_export_sample_rendered(self: MLTBank, index: int, path: Path, **kwargs) -> None:
    pcm, rate, _diag = self.render_sample_for_wav(index, **kwargs)
    write_wav(path, pcm, rate)


def _bank_export_loop_preview_rendered(self: MLTBank, index: int, path: Path, preview_seconds: int = 20, **kwargs) -> None:
    pcm, rate, _diag = self.render_loop_preview_for_wav(index, preview_seconds=preview_seconds, **kwargs)
    write_wav(path, pcm, rate)


def _bank_export_all_rendered(self: MLTBank, out_dir: Path, **kwargs) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for s in self.samples:
        safe_alias = self.safe_alias(s.index)
        pcm, rate, diag = self.render_sample_for_wav(s.index, **kwargs)
        name = f"{s.index:03d}_{safe_alias}_{rate}Hz"
        if s.loop_flag:
            name += "_loop"
        wav_path = out_dir / f"{name}.wav"
        write_wav(wav_path, pcm, rate)
        row = {
            "index": s.index,
            "filename": wav_path.name,
            "alias": s.alias,
            "stored_rate_word_x2": s.current_sample_rate,
            "base_rate_exact_hz": f"{s.current_base_sample_rate_exact:.6f}",
            "render_rate": rate,
            "root_key": self.sample_root_key(s.index) if self.sample_root_key(s.index) is not None else "",
            "trigger_note": kwargs.get("trigger_note", DEFAULT_TRIGGER_NOTE),
            "loop_flag": int(s.loop_flag),
            "usage": ";".join(s.usage),
        }
        row.update(diag)
        rows.append(row)
    if rows:
        fields = sorted({k for r in rows for k in r.keys()})
        with (out_dir / f"{self.path.stem}_clean_audio_metadata.csv").open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader(); writer.writerows(rows)
    self.write_alias_csv(out_dir / f"{self.path.stem}_aliases.csv")


def _bank_write_audio_quality_report_csv(self: MLTBank, path: Path, **kwargs) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for s in self.samples:
        _pcm, rate, diag = self.render_sample_for_wav(s.index, **kwargs)
        row = {
            "index": s.index,
            "alias": s.alias,
            "stored_rate_word_x2": s.current_sample_rate,
            "base_rate_exact_hz": f"{s.current_base_sample_rate_exact:.6f}",
            "audition_or_render_rate": rate,
            "loop_flag": int(s.loop_flag),
            "usage": ";".join(s.usage),
            "root_key": self.sample_root_key(s.index) if self.sample_root_key(s.index) is not None else "",
        }
        row.update(diag)
        # Warnings written to the quality report.
        warnings = []
        if int(diag.get("pre_clipped_samples", 0)) > 0:
            warnings.append("source clips/rasps possible")
        if int(diag.get("pre_big_jump_count", 0)) > 0:
            warnings.append("large sample jumps")
        if int(diag.get("post_clipped_samples", 0)) > 0:
            warnings.append("post still clips")
        row["warnings"] = "; ".join(warnings)
        rows.append(row)
    fields = sorted({k for r in rows for k in r.keys()}) or ["empty"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)


# Add clean-render methods without changing the repacker.
MLTBank.render_sample_for_wav = _bank_render_sample_for_wav  # type: ignore[attr-defined]
MLTBank.render_loop_preview_for_wav = _bank_render_loop_preview_for_wav  # type: ignore[attr-defined]
MLTBank.export_sample_rendered = _bank_export_sample_rendered  # type: ignore[attr-defined]
MLTBank.export_loop_preview_rendered = _bank_export_loop_preview_rendered  # type: ignore[attr-defined]
MLTBank.export_all_rendered = _bank_export_all_rendered  # type: ignore[attr-defined]
MLTBank.write_audio_quality_report_csv = _bank_write_audio_quality_report_csv  # type: ignore[attr-defined]


# GUI additions
_OLD_APP_INIT = MLTExplorerApp.__init__
_OLD_BUILD_UI = MLTExplorerApp._build_ui
_OLD_PREVIEW_SELECTED = MLTExplorerApp.preview_selected
_OLD_PREVIEW_LOOP_SELECTED = MLTExplorerApp.preview_loop_selected
_OLD_EXPORT_SELECTED = MLTExplorerApp.export_selected
_OLD_EXPORT_LOOP_PREVIEW_SELECTED = MLTExplorerApp.export_loop_preview_selected
_OLD_EXPORT_ALL = MLTExplorerApp.export_all
_OLD_SAVE_PROJECT_JSON = MLTExplorerApp.save_project_json
_OLD_LOAD_PROJECT_JSON = MLTExplorerApp.load_project_json


def _app_init(self: MLTExplorerApp, root: tk.Tk) -> None:
    self.root = root
    self.root.title("MLT Soundbank Explorer")
    self.root.geometry("1220x760")
    self.bank = None
    self.current_temp_wav = None
    self._play_generation = 0
    self._preview_process = None
    self._background_active = False
    self._closing = False
    self._settings_refresh_job = None
    self._ui_queue = queue.Queue()
    self._ui_queue_job = None
    self.status_var = tk.StringVar(value="Open an MLT file to begin.")
    self.filter_var = tk.StringVar()
    self.loop_preview_seconds_var = tk.IntVar(value=20)
    self.loop_declick_var = tk.BooleanVar(value=True)
    self.loop_zero_cross_var = tk.BooleanVar(value=True)
    self.loop_trim_silence_var = tk.BooleanVar(value=True)
    self.loop_crossfade_ms_var = tk.DoubleVar(value=3.0)
    self.pitch_correct_var = tk.BooleanVar(value=False)
    self.trigger_note_var = tk.IntVar(value=DEFAULT_TRIGGER_NOTE)
    self.preserve_bank_rate_var = tk.BooleanVar(value=True)
    self.detail_var = tk.StringVar(value="No sample selected.")
    self.view_filter_var = tk.StringVar(value="All samples")
    self.rate_filter_var = tk.StringVar(value="Any stored rate word")
    self.program_filter_var = tk.StringVar(value="All programs")
    self.pitch_preset_var = tk.StringVar(value="Base stored/2 rate")
    # Audio cleanup controls
    self.clean_audition_var = tk.BooleanVar(value=True)
    self.fixed_render_rate_var = tk.BooleanVar(value=True)
    self.render_rate_var = tk.IntVar(value=DEFAULT_CLEAN_RENDER_RATE)
    self.dc_filter_var = tk.BooleanVar(value=True)
    self.decrackle_filter_var = tk.BooleanVar(value=True)
    self.decrackle_strength_var = tk.StringVar(value="Light")
    self.limiter_var = tk.BooleanVar(value=True)
    self.edge_fade_var = tk.BooleanVar(value=True)
    self._build_ui()
    self.root.protocol("WM_DELETE_WINDOW", self._on_close)
    self._start_ui_queue_pump()


def _build_ui_with_audio_tools(self: MLTExplorerApp) -> None:
    _OLD_BUILD_UI(self)
    # Add the audio cleanup menu.
    try:
        menubar = self.root.nametowidget(self.root.cget("menu"))
        clean_menu = tk.Menu(menubar, tearoff=False)
        clean_menu.add_checkbutton(label="Clean audition/render WAVs", variable=self.clean_audition_var)
        clean_menu.add_checkbutton(label="Render to fixed 44.1/48 kHz", variable=self.fixed_render_rate_var)
        clean_menu.add_separator()
        for rate in CLEAN_RENDER_RATES:
            clean_menu.add_radiobutton(label=f"Render rate: {rate} Hz", variable=self.render_rate_var, value=rate)
        clean_menu.add_separator()
        clean_menu.add_checkbutton(label="Remove DC offset", variable=self.dc_filter_var)
        clean_menu.add_checkbutton(label="De-crackle isolated spikes", variable=self.decrackle_filter_var)
        for value in ("Light", "Medium", "Strong"):
            clean_menu.add_radiobutton(label=f"De-crackle strength: {value}", variable=self.decrackle_strength_var, value=value)
        clean_menu.add_checkbutton(label="Safety limiter / headroom", variable=self.limiter_var)
        clean_menu.add_checkbutton(label="Tiny edge fade", variable=self.edge_fade_var)
        clean_menu.add_separator()
        clean_menu.add_command(label="Export All Clean WAV...", command=self.export_all_clean)
        clean_menu.add_command(label="Save Audio Quality Report CSV...", command=self.save_audio_quality_report_csv)
        menubar.add_cascade(label="Audio Clean", menu=clean_menu)
    except Exception:
        pass
    # Add quick cleanup controls above the status line.
    try:
        strip = ttk.Frame(self.root, padding=(8, 2))
        strip.pack(side=tk.BOTTOM, fill=tk.X, before=self.root.children.get('!label'))
    except Exception:
        strip = ttk.Frame(self.root, padding=(8, 2))
        strip.pack(side=tk.BOTTOM, fill=tk.X)
    ttk.Checkbutton(strip, text="Clean preview/export", variable=self.clean_audition_var).pack(side=tk.LEFT, padx=(0, 8))
    ttk.Checkbutton(strip, text="Fixed render Hz", variable=self.fixed_render_rate_var).pack(side=tk.LEFT, padx=(0, 4))
    ttk.Combobox(strip, textvariable=self.render_rate_var, state="readonly", width=8, values=CLEAN_RENDER_RATES).pack(side=tk.LEFT, padx=(0, 12))
    ttk.Checkbutton(strip, text="Limiter", variable=self.limiter_var).pack(side=tk.LEFT, padx=(0, 8))
    ttk.Checkbutton(strip, text="De-crackle", variable=self.decrackle_filter_var).pack(side=tk.LEFT, padx=(0, 4))
    ttk.Combobox(strip, textvariable=self.decrackle_strength_var, state="readonly", width=8, values=("Light", "Medium", "Strong")).pack(side=tk.LEFT, padx=(0, 12))
    ttk.Button(strip, text="Audio Quality Report", command=self.save_audio_quality_report_csv).pack(side=tk.LEFT, padx=(0, 6))


def _clean_render_options(self: MLTExplorerApp) -> Dict[str, object]:
    return {
        "clean": bool(self.clean_audition_var.get()),
        "fixed_output_rate": bool(self.fixed_render_rate_var.get()),
        "output_rate": int(self.render_rate_var.get() or DEFAULT_CLEAN_RENDER_RATE),
        "dc_filter": bool(self.dc_filter_var.get()),
        "decrackle": bool(self.decrackle_filter_var.get()),
        "decrackle_strength": str(self.decrackle_strength_var.get() or "Light"),
        "limiter": bool(self.limiter_var.get()),
        "edge_fade": bool(self.edge_fade_var.get()),
    }


def _preview_selected_clean(self: MLTExplorerApp) -> None:
    if not self.bank:
        return
    idx = self.selected_index()
    if idx is None:
        return
    try:
        self.stop_preview(silent=True)
        s = self.bank.samples[idx]
        pcm, rate, diag = self.bank.render_sample_for_wav(
            idx,
            pitch_correct=self.pitch_correct_var.get(),
            trigger_note=int(self.trigger_note_var.get()),
            **_clean_render_options(self),
        )
        tmp = Path(tempfile.gettempdir()) / f"mlt_preview_{os.getpid()}_{idx:03d}.wav"
        write_wav(tmp, pcm, rate)
        self.current_temp_wav = tmp
        self._play_wav(tmp)
        self.status_var.set(f"Previewing sample {idx}: {s.alias} | {rate} Hz | peak {diag.get('post_peak', diag.get('peak', 0))}")
    except Exception as exc:
        messagebox.showerror("Preview failed", str(exc))


def _preview_loop_selected_clean(self: MLTExplorerApp) -> None:
    if not self.bank:
        return
    idx = self.selected_index()
    if idx is None:
        return
    try:
        s = self.bank.samples[idx]
        if not self.bank.loop_points_samples(idx):
            messagebox.showinfo("Loop preview", "This sample has no loop flag / loop points.")
            return
        self.stop_preview(silent=True)
        seconds = int(self.loop_preview_seconds_var.get() or 20)
        pcm, rate, diag = self.bank.render_loop_preview_for_wav(
            idx,
            preview_seconds=seconds,
            pitch_correct=self.pitch_correct_var.get(),
            trigger_note=int(self.trigger_note_var.get()),
            loop_declick=self.loop_declick_var.get(),
            crossfade_ms=float(self.loop_crossfade_ms_var.get() or 0.0),
            zero_cross=self.loop_zero_cross_var.get(),
            trim_silence=self.loop_trim_silence_var.get(),
            **_clean_render_options(self),
        )
        tmp = Path(tempfile.gettempdir()) / f"mlt_preview_{os.getpid()}_{idx:03d}_loop_clean_{seconds}s.wav"
        write_wav(tmp, pcm, rate)
        self.current_temp_wav = tmp
        self._play_wav(tmp)
        self.status_var.set(f"Loop preview sample {idx}: {s.alias} | {rate} Hz | clean={bool(self.clean_audition_var.get())} | {seconds}s")
    except Exception as exc:
        messagebox.showerror("Loop preview failed", str(exc))


def _export_selected_clean(self: MLTExplorerApp) -> None:
    if not self.bank:
        return
    idx = self.selected_index()
    if idx is None:
        return
    s = self.bank.samples[idx]
    # Include the final WAV rate in the filename.
    _pcm, rate, _diag = self.bank.render_sample_for_wav(
        idx,
        pitch_correct=self.pitch_correct_var.get(),
        trigger_note=int(self.trigger_note_var.get()),
        **_clean_render_options(self),
    )
    default = f"{idx:03d}_{self.bank.safe_alias(idx)}_{rate}Hz_clean.wav"
    path = filedialog.asksaveasfilename(title="Export selected WAV", initialfile=default, defaultextension=".wav", filetypes=[("WAV", "*.wav")])
    if not path:
        return
    try:
        self.bank.export_sample_rendered(
            idx,
            Path(path),
            pitch_correct=self.pitch_correct_var.get(),
            trigger_note=int(self.trigger_note_var.get()),
            **_clean_render_options(self),
        )
        self.status_var.set(f"Exported {Path(path).name}")
    except Exception as exc:
        messagebox.showerror("Export failed", str(exc))


def _export_loop_preview_selected_clean(self: MLTExplorerApp) -> None:
    if not self.bank:
        return
    idx = self.selected_index()
    if idx is None:
        return
    s = self.bank.samples[idx]
    if not self.bank.loop_points_samples(idx):
        messagebox.showinfo("Export loop preview", "This sample has no loop flag / loop points.")
        return
    seconds = int(self.loop_preview_seconds_var.get() or 20)
    _pcm, rate, _diag = self.bank.render_loop_preview_for_wav(
        idx,
        preview_seconds=seconds,
        pitch_correct=self.pitch_correct_var.get(),
        trigger_note=int(self.trigger_note_var.get()),
        loop_declick=self.loop_declick_var.get(),
        crossfade_ms=float(self.loop_crossfade_ms_var.get() or 0.0),
        zero_cross=self.loop_zero_cross_var.get(),
        trim_silence=self.loop_trim_silence_var.get(),
        **_clean_render_options(self),
    )
    default = f"{idx:03d}_{self.bank.safe_alias(idx)}_{rate}Hz_loopPreview_{seconds}s_clean.wav"
    path = filedialog.asksaveasfilename(title="Export loop preview WAV", initialfile=default, defaultextension=".wav", filetypes=[("WAV", "*.wav")])
    if not path:
        return
    try:
        self.bank.export_loop_preview_rendered(
            idx,
            Path(path),
            preview_seconds=seconds,
            pitch_correct=self.pitch_correct_var.get(),
            trigger_note=int(self.trigger_note_var.get()),
            loop_declick=self.loop_declick_var.get(),
            crossfade_ms=float(self.loop_crossfade_ms_var.get() or 0.0),
            zero_cross=self.loop_zero_cross_var.get(),
            trim_silence=self.loop_trim_silence_var.get(),
            **_clean_render_options(self),
        )
        self.status_var.set(f"Exported loop preview {Path(path).name}")
    except Exception as exc:
        messagebox.showerror("Export loop preview failed", str(exc))


def _export_all_rendered_gui(self: MLTExplorerApp) -> None:
    if not self.bank:
        return
    out = filedialog.askdirectory(title="Export all WAV files")
    if not out:
        return
    bank = self.bank
    pitch_correct = bool(self.pitch_correct_var.get())
    trigger_note = int(self.trigger_note_var.get())
    clean_kwargs = _clean_render_options(self)
    self._run_background("Export all clean", lambda: bank.export_all_rendered(
        Path(out),
        pitch_correct=pitch_correct,
        trigger_note=trigger_note,
        **clean_kwargs,
    ))


def _export_all_clean_gui(self: MLTExplorerApp) -> None:
    _export_all_rendered_gui(self)


def _save_audio_quality_report_csv(self: MLTExplorerApp) -> None:
    if not self.bank:
        return
    default = self.bank.path.with_name(self.bank.path.stem + "_audio_quality.csv").name
    path = filedialog.asksaveasfilename(title="Save audio quality report CSV", initialfile=default, defaultextension=".csv", filetypes=[("CSV", "*.csv")])
    if not path:
        return
    try:
        self.bank.write_audio_quality_report_csv(
            Path(path),
            pitch_correct=self.pitch_correct_var.get(),
            trigger_note=int(self.trigger_note_var.get()),
            **_clean_render_options(self),
        )
        self.status_var.set(f"Saved audio quality report to {Path(path).name}")
    except Exception as exc:
        messagebox.showerror("Audio quality report failed", str(exc))


MLTExplorerApp.__init__ = _app_init  # type: ignore[assignment]
MLTExplorerApp._build_ui = _build_ui_with_audio_tools  # type: ignore[assignment]
MLTExplorerApp.preview_selected = _preview_selected_clean  # type: ignore[assignment]
MLTExplorerApp.preview_loop_selected = _preview_loop_selected_clean  # type: ignore[assignment]
MLTExplorerApp.export_selected = _export_selected_clean  # type: ignore[assignment]
MLTExplorerApp.export_loop_preview_selected = _export_loop_preview_selected_clean  # type: ignore[assignment]
MLTExplorerApp.export_all = _export_all_rendered_gui  # type: ignore[assignment]
MLTExplorerApp.export_all_clean = _export_all_clean_gui  # type: ignore[attr-defined]
MLTExplorerApp.save_audio_quality_report_csv = _save_audio_quality_report_csv  # type: ignore[attr-defined]


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


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
