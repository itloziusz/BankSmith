from __future__ import annotations

import math
import struct
from typing import Dict, List

from .core import *
from .audio import *

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


