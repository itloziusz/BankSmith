from __future__ import annotations

import struct

STEP_TABLE = (230, 230, 230, 230, 307, 409, 512, 614)


def clamp16(v: int) -> int:
    return -32768 if v < -32768 else 32767 if v > 32767 else int(v)


def _aica_step(code: int, history: int, step_size: int) -> tuple[int, int]:
    code &= 0x0F
    sign = code & 8
    delta = code & 7
    diff = ((1 + (delta << 1)) * step_size) >> 3
    diff = max(0, min(32767, diff))
    new_step = (STEP_TABLE[delta] * step_size) >> 8
    step_size = max(127, min(24576, new_step))
    value = history - diff if sign else history + diff
    value = clamp16(value)
    return value, step_size


def decode_aica_adpcm(payload: bytes, sample_count: int | None = None, *, high_pass: bool = True) -> bytes:
    """Decode Yamaha/AICA 4-bit ADPCM to little-endian PCM16.

    AICA consumes the low nibble first, then the high nibble. The state starts
    with history=0 and stepSize=127, matching Sega's driver/reference codec.
    """
    capacity = len(payload) * 2
    count = capacity if sample_count is None else max(0, min(int(sample_count), capacity))
    out = bytearray()
    history = 0
    step_size = 127
    produced = 0
    for b in payload:
        for code in (b & 0x0F, (b >> 4) & 0x0F):
            if produced >= count:
                return bytes(out)
            if high_pass:
                history = history * 254 // 256
            history, step_size = _aica_step(code, history, step_size)
            out += struct.pack('<h', history)
            produced += 1
    return bytes(out)


def encode_aica_adpcm(pcm_le_i16: bytes) -> bytes:
    """Encode little-endian PCM16 to Yamaha/AICA 4-bit ADPCM."""
    if len(pcm_le_i16) & 1:
        pcm_le_i16 += b'\x00'
    count = len(pcm_le_i16) // 2
    samples = struct.unpack('<' + 'h' * count, pcm_le_i16) if count else ()
    history = 0
    step_size = 127
    out = bytearray()
    pending_low: int | None = None
    for sample in samples:
        delta_pcm = (int(sample) & -8) - history
        denom = step_size << 14
        code = (abs(delta_pcm) << 16) // denom if denom else 0
        code = max(0, min(7, code))
        if delta_pcm < 0:
            code |= 8
        if pending_low is None:
            pending_low = code & 0x0F
        else:
            out.append((pending_low & 0x0F) | ((code & 0x0F) << 4))
            pending_low = None
        history, step_size = _aica_step(code, history, step_size)
    if pending_low is not None:
        out.append(pending_low)
    return bytes(out)


def decode_pcm8(payload: bytes, sample_count: int | None = None) -> bytes:
    count = len(payload) if sample_count is None else max(0, min(int(sample_count), len(payload)))
    out = bytearray()
    for b in payload[:count]:
        value = struct.unpack('<b', bytes((b,)))[0] << 8
        out += struct.pack('<h', value)
    return bytes(out)


def encode_pcm8(pcm_le_i16: bytes) -> bytes:
    if len(pcm_le_i16) & 1:
        pcm_le_i16 += b'\x00'
    count = len(pcm_le_i16) // 2
    samples = struct.unpack('<' + 'h' * count, pcm_le_i16) if count else ()
    return bytes(((int(v) >> 8) & 0xFF) for v in samples)


def decode_pcm16le(payload: bytes, sample_count: int | None = None) -> bytes:
    available = len(payload) // 2
    count = available if sample_count is None else max(0, min(int(sample_count), available))
    return payload[:count * 2]


def encode_pcm16le(pcm_le_i16: bytes) -> bytes:
    return pcm_le_i16[:len(pcm_le_i16) & ~1]


def decode_tone(payload: bytes, fmt: str, sample_count: int | None = None) -> bytes:
    if fmt == 'adpcm':
        return decode_aica_adpcm(payload, sample_count)
    if fmt == 'pcm8':
        return decode_pcm8(payload, sample_count)
    if fmt == 'pcm16':
        return decode_pcm16le(payload, sample_count)
    raise ValueError(f'Unsupported Dreamcast tone format: {fmt}')


def encode_tone(pcm_le_i16: bytes, fmt: str) -> bytes:
    if fmt == 'adpcm':
        return encode_aica_adpcm(pcm_le_i16)
    if fmt == 'pcm8':
        return encode_pcm8(pcm_le_i16)
    if fmt == 'pcm16':
        return encode_pcm16le(pcm_le_i16)
    raise ValueError(f'Unsupported Dreamcast tone format: {fmt}')


def tone_capacity_samples(payload_size: int, fmt: str) -> int:
    if fmt == 'adpcm':
        return int(payload_size) * 2
    if fmt == 'pcm8':
        return int(payload_size)
    if fmt == 'pcm16':
        return int(payload_size) // 2
    raise ValueError(fmt)
