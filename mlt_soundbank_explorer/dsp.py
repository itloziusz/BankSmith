from __future__ import annotations

import math
import struct
from typing import List, Optional, Tuple

from .core import *

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