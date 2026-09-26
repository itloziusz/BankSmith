from __future__ import annotations

import math
import os
import struct
import tempfile
import wave
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .core import *

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



def _cubic_interp_i16(y0: int, y1: int, y2: int, y3: int, t: float) -> int:
    a0 = -0.5 * y0 + 1.5 * y1 - 1.5 * y2 + 0.5 * y3
    a1 = y0 - 2.5 * y1 + 2.0 * y2 - 0.5 * y3
    a2 = -0.5 * y0 + 0.5 * y2
    a3 = y1
    return clamp16(round(((a0 * t + a1) * t + a2) * t + a3))


def resample_pcm16_cubic(
    pcm_le_i16: bytes,
    src_rate: int | float,
    dst_rate: int | float,
) -> bytes:
    """Dependency-free cubic mono PCM16 resampler used for upsampling."""
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
    last = len(samples) - 1
    for i in range(new_len):
        pos = i * ratio
        j = int(pos)
        t = pos - j
        j0 = max(0, j - 1)
        j1 = max(0, min(last, j))
        j2 = max(0, min(last, j + 1))
        j3 = max(0, min(last, j + 2))
        out.append(_cubic_interp_i16(samples[j0], samples[j1], samples[j2], samples[j3], t))
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
