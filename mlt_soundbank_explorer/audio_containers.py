"""Read-only audio containers: gcax/ps2 DTPK, CRI CSB and big-endian ADX MDT.

These formats are inspected, extracted and decoded to WAV. They are not
repacked, so the probes report ``editable_audio=False``.
"""
from __future__ import annotations

import math
import re
import struct
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DTPK_GC_MAGIC = b"gcaxDTPK"
DTPK_PS2_MAGIC = b"ps2_DTPK"

_PS_COEFS = ((0, 0), (60, 0), (115, -52), (98, -55), (122, -60))


def _clamp16(v: int) -> int:
    return -32768 if v < -32768 else 32767 if v > 32767 else v


def _s4(v: int) -> int:
    v &= 0xF
    return v - 16 if v >= 8 else v


@dataclass
class DecodedAudio:
    pcm: bytes  # interleaved little-endian PCM16
    rate: int
    channels: int = 1


def write_pcm_wav(path: Path, audio: DecodedAudio) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(audio.channels)
        wf.setsampwidth(2)
        wf.setframerate(int(audio.rate))
        wf.writeframes(audio.pcm)


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._") or "unnamed"


# ---------------------------------------------------------------------------
# Codecs
# ---------------------------------------------------------------------------

def nibble_address_to_sample_count(last_nibble: int) -> int:
    """Samples up to and including a DSP nibble address (frame headers are 2 nibbles)."""
    frame, pos = divmod(last_nibble, 16)
    return max(0, frame * 14 + pos - 2 + 1)


def decode_dsp_adpcm_stream(
    payload: bytes,
    coefs: List[int],
    sample_count: int,
    hist1: int = 0,
    hist2: int = 0,
) -> bytes:
    out = bytearray()
    produced = 0
    pos = 0
    while produced < sample_count and pos < len(payload):
        frame = payload[pos:pos + 8]
        pos += 8
        ps = frame[0]
        idx = ((ps >> 4) & 0xF) * 2
        c1 = coefs[idx] if idx < len(coefs) else 0
        c2 = coefs[idx + 1] if idx + 1 < len(coefs) else 0
        scale = 1 << (ps & 0xF)
        for b in frame[1:]:
            for nib in (_s4(b >> 4), _s4(b)):
                if produced >= sample_count:
                    break
                s = _clamp16((((nib * scale) << 11) + 1024 + c1 * hist1 + c2 * hist2) >> 11)
                out += struct.pack("<h", s)
                hist2, hist1 = hist1, s
                produced += 1
    if produced < sample_count:
        out += b"\x00\x00" * (sample_count - produced)
    return bytes(out)


def decode_ps_adpcm(payload: bytes) -> Tuple[bytes, Dict[str, Any]]:
    """Decode PlayStation/PS2 VAG ADPCM. Returns PCM and loop block indexes."""
    out = bytearray()
    hist1 = hist2 = 0
    info: Dict[str, Any] = {"loop_start_block": None, "loop_end_block": None}
    for blk in range(len(payload) // 16):
        frame = payload[blk * 16:blk * 16 + 16]
        shift = frame[0] & 0xF
        pred = min(frame[0] >> 4, len(_PS_COEFS) - 1)
        flag = frame[1]
        if flag == 6 and info["loop_start_block"] is None:
            info["loop_start_block"] = blk
        if flag == 3:
            info["loop_end_block"] = blk
        c1, c2 = _PS_COEFS[pred]
        for b in frame[2:]:
            for nib in (b & 0xF, b >> 4):
                s = (nib << 12)
                s = (s - 0x10000 if s >= 0x8000 else s) >> shift
                s = _clamp16(s + ((c1 * hist1 + c2 * hist2) >> 6))
                out += struct.pack("<h", s)
                hist2, hist1 = hist1, s
        if flag == 1:
            break
    return bytes(out), info


def decode_pcm16be(payload: bytes) -> bytes:
    n = len(payload) // 2
    return struct.pack(f"<{n}h", *struct.unpack(f">{n}h", payload[:n * 2]))


@dataclass
class AdxHeader:
    data_offset: int
    encoding: int
    block_size: int
    bit_depth: int
    channels: int
    rate: int
    total_samples: int
    highpass: int
    version: int


def parse_adx_header(data: bytes) -> AdxHeader:
    if len(data) < 0x18 or data[0] != 0x80 or data[1] != 0x00:
        raise ValueError("Not an ADX stream")
    copyright_off = struct.unpack_from(">H", data, 2)[0]
    if data[copyright_off - 2:copyright_off + 4] != b"(c)CRI":
        raise ValueError("ADX copyright marker missing")
    return AdxHeader(
        data_offset=copyright_off + 4,
        encoding=data[4],
        block_size=data[5],
        bit_depth=data[6],
        channels=data[7],
        rate=struct.unpack_from(">I", data, 8)[0],
        total_samples=struct.unpack_from(">I", data, 12)[0],
        highpass=struct.unpack_from(">H", data, 16)[0],
        version=data[18],
    )


def decode_adx(data: bytes) -> DecodedAudio:
    h = parse_adx_header(data)
    if h.encoding != 3 or h.bit_depth != 4 or h.block_size != 18 or not 1 <= h.channels <= 8:
        raise ValueError(
            f"Unsupported ADX variant: encoding={h.encoding} bits={h.bit_depth} "
            f"block={h.block_size} channels={h.channels}"
        )
    x = h.highpass / h.rate
    a = math.sqrt(2.0) - math.cos(2.0 * math.pi * x)
    b = math.sqrt(2.0) - 1.0
    c = (a - math.sqrt((a + b) * (a - b))) / b
    c1 = math.floor(c * 8192)
    c2 = math.floor(c * c * -4096)

    nch = h.channels
    per_ch: List[List[int]] = [[] for _ in range(nch)]
    hist = [[0, 0] for _ in range(nch)]
    pos = h.data_offset
    group = 18 * nch
    while pos + group <= len(data) and len(per_ch[0]) < h.total_samples:
        for ch in range(nch):
            frame = data[pos + ch * 18:pos + ch * 18 + 18]
            raw_scale = (frame[0] << 8) | frame[1]
            if raw_scale == 0x8001 and ch == 0:
                pos = len(data)
                break
            scale = raw_scale + 1
            h1, h2 = hist[ch]
            for byte in frame[2:]:
                for nib in (_s4(byte >> 4), _s4(byte)):
                    s = _clamp16(nib * scale + ((c1 * h1 + c2 * h2) >> 12))
                    per_ch[ch].append(s)
                    h2, h1 = h1, s
            hist[ch] = [h1, h2]
        else:
            pos += group
    frames = min(len(p) for p in per_ch)
    frames = min(frames, h.total_samples) if h.total_samples else frames
    inter = [per_ch[ch][i] for i in range(frames) for ch in range(nch)]
    return DecodedAudio(struct.pack(f"<{len(inter)}h", *inter), h.rate, nch)


# ---------------------------------------------------------------------------
# DTPK (gcaxDTPK / ps2_DTPK)
# ---------------------------------------------------------------------------

@dataclass
class DtpkSample:
    index: int
    table_offset: int
    data_offset: int  # absolute file offset
    data_size: int
    rate: int
    codec: str  # "dsp-adpcm", "pcm16be", "ps-adpcm" or "unknown"
    nibble_end: int = 0
    loop_start: int = 0
    coefs: List[int] = field(default_factory=list)
    initial_ps: int = 0
    format_word: int = 0

    def decode(self, data: bytes) -> DecodedAudio:
        raw = data[self.data_offset:self.data_offset + self.data_size]
        if self.codec == "dsp-adpcm":
            count = nibble_address_to_sample_count(self.nibble_end)
            return DecodedAudio(decode_dsp_adpcm_stream(raw, self.coefs, count), self.rate)
        if self.codec == "pcm16be":
            return DecodedAudio(decode_pcm16be(raw), self.rate)
        if self.codec == "ps-adpcm":
            pcm, _ = decode_ps_adpcm(raw)
            return DecodedAudio(pcm, self.rate)
        raise ValueError(f"No decoder for sample {self.index} (format word 0x{self.format_word:X})")


@dataclass
class DtpkPack:
    platform: str  # "gcax" or "ps2"
    endian: str
    version: bytes
    declared_size: int
    table_size: int
    data_offset: int
    data_size: int
    pointers: List[int]
    samples: List[DtpkSample]
    notes: List[str]


def is_dtpk(data: bytes) -> bool:
    return data[:8] in (DTPK_GC_MAGIC, DTPK_PS2_MAGIC)


def parse_dtpk(data: bytes) -> DtpkPack:
    if not is_dtpk(data) or len(data) < 0xC0:
        raise ValueError("Not a DTPK container")
    ps2 = data[:4] == b"ps2_"
    e = "<" if ps2 else ">"

    def u32(off: int) -> int:
        if off < 0 or off + 4 > len(data):
            raise ValueError(f"DTPK u32 out of range at 0x{off:X}")
        return struct.unpack_from(e + "I", data, off)[0]

    declared, table_size, _, data_size, data_off = (u32(o) for o in range(0x0C, 0x20, 4))
    tag = b"ps2_TBLD" if ps2 else b"gcaxTBLD"
    chunk = b"ps2_VAGD" if ps2 else b"gcaxPCMD"
    notes: List[str] = []
    if data[0x60:0x68] != tag:
        raise ValueError("DTPK table chunk (TBLD) missing at 0x60")
    if data[data_off:data_off + 8] != chunk:
        raise ValueError(f"DTPK sample chunk {chunk!r} missing at 0x{data_off:X}")
    if declared != len(data):
        notes.append(f"declared size 0x{declared:X} differs from actual 0x{len(data):X}")
    if data_off + 0x10 + data_size > len(data):
        raise ValueError("DTPK sample data extends beyond the file")

    pointers = [u32(0x98 + 4 * i) for i in range(10)]
    table, table_end = pointers[8], pointers[9]
    if not 0xC0 <= table < len(data):
        raise ValueError(f"DTPK sample table pointer 0x{table:X} is invalid")

    samples: List[DtpkSample] = []
    body = data_off + 0x10
    if ps2:
        count = u32(table) + 1
        rec0 = table + 4
        if table_end > rec0:
            count = min(count, (table_end - rec0) // 16)
        for i in range(count):
            r = rec0 + i * 16
            off = u32(r)
            rate = struct.unpack_from("<H", data, r + 10)[0]
            size = u32(r + 12)
            if off + size > data_size:
                raise ValueError(f"DTPK sample {i} extends beyond the VAG data")
            samples.append(DtpkSample(i, r, body + off, size, rate, "ps-adpcm"))
    else:
        count = u32(table) + 1
        rec0 = table + 8
        if rec0 + count * 0x44 > len(data):
            raise ValueError("DTPK sample table extends beyond the file")
        start = 0x20  # chunk-relative; the first 0x10 bytes after the chunk header are null
        for i in range(count):
            r = rec0 + i * 0x44
            loop_start = u32(r)
            nib_end = u32(r + 4)
            coefs = list(struct.unpack_from(">16h", data, r + 8))
            ps = u32(r + 0x28)
            fmt = u32(r + 0x34)
            rate = u32(r + 0x38) >> 16
            nbytes = u32(r + 0x3C)
            end = u32(r + 0x40)
            if end == 0 or end < start:  # the final record stores no end offset
                end = start + (nbytes if (fmt & 0xFF) == 0 else nbytes * 2)
                end += end & 1
            size = end - start
            kind = fmt & 0xFF
            if kind == 0:
                codec = "dsp-adpcm"
            elif kind == 1:
                codec = "pcm16be"
            else:
                codec = "unknown"
            if data_off + start + size > data_off + 0x10 + data_size:
                raise ValueError(f"DTPK sample {i} extends beyond the PCM data")
            samples.append(DtpkSample(
                i, r, data_off + start, size, rate, codec,
                nibble_end=nib_end, loop_start=loop_start, coefs=coefs,
                initial_ps=ps, format_word=fmt,
            ))
            start = end
    return DtpkPack(
        "ps2" if ps2 else "gcax", e, data[8:12], declared, table_size,
        data_off, data_size, pointers, samples, notes,
    )


# ---------------------------------------------------------------------------
# CRI CSB (@UTF tables)
# ---------------------------------------------------------------------------

_UTF_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 8, 7: 8, 8: 4, 9: 8, 0xA: 4, 0xB: 8, 0xC: 16}
_UTF_FORMATS = {0: ">B", 1: ">b", 2: ">H", 3: ">h", 4: ">I", 5: ">i", 6: ">Q", 7: ">q", 8: ">f", 9: ">d"}


@dataclass(frozen=True)
class UtfBlob:
    offset: int
    size: int


def parse_utf_table(data: bytes, base: int = 0) -> Tuple[str, List[Dict[str, Any]]]:
    """Parse one CRI ``@UTF`` table. Data fields are returned as :class:`UtfBlob`."""
    if data[base:base + 4] != b"@UTF" or base + 0x20 > len(data):
        raise ValueError(f"No @UTF table at 0x{base:X}")
    _size, _ver, rows_off, str_off, data_off, name_off, nfields, row_len, nrows = struct.unpack_from(
        ">IHHIIIHHI", data, base + 4
    )
    b = base + 8
    if b + max(rows_off, str_off, data_off) > len(data) or nrows > 0x100000:
        raise ValueError("Corrupt @UTF header")

    def string(off: int) -> str:
        p = b + str_off + off
        end = data.index(b"\0", p)
        return data[p:end].decode("utf-8", "replace")

    def value(pos: int, ty: int) -> Any:
        if ty in _UTF_FORMATS:
            return struct.unpack_from(_UTF_FORMATS[ty], data, pos)[0]
        if ty == 0xA:
            return string(struct.unpack_from(">I", data, pos)[0])
        if ty == 0xB:
            off, size = struct.unpack_from(">II", data, pos)
            return UtfBlob(b + data_off + off, size)
        return data[pos:pos + 16]

    pos = b + 0x18
    columns = []
    for _ in range(nfields):
        flags = data[pos]
        storage, ty = flags >> 4, flags & 0xF
        if ty not in _UTF_SIZES:
            raise ValueError(f"Unknown @UTF column type {ty}")
        name = string(struct.unpack_from(">I", data, pos + 1)[0])
        pos += 5
        const = None
        if storage == 3:
            const = value(pos, ty)
            pos += _UTF_SIZES[ty]
        columns.append((name, storage, ty, const))
    rows: List[Dict[str, Any]] = []
    for r in range(nrows):
        p = b + rows_off + r * row_len
        row: Dict[str, Any] = {}
        for name, storage, ty, const in columns:
            if storage == 5:
                row[name] = value(p, ty)
                p += _UTF_SIZES[ty]
            else:
                row[name] = const
        rows.append(row)
    return string(name_off), rows


@dataclass
class CsbPart:
    """One row of a sound element's wrapper table (``lpflg`` 0 = intro, 1 = loop)."""
    loop_flag: int
    data: Optional[UtfBlob]
    header: Optional[UtfBlob] = None


@dataclass
class CsbSound:
    index: int
    name: str
    codec: str  # wrapper table name: ADPCM_WII (DSP), AAX (ADX) or SWLPCM
    channels: int
    rate: int
    sample_count: int
    table_offset: int
    parts: List[CsbPart] = field(default_factory=list)
    streaming: bool = False

    @property
    def data(self) -> Optional[UtfBlob]:
        return self.parts[0].data if self.parts else None

    @property
    def headers(self) -> Optional[UtfBlob]:
        return self.parts[0].header if self.parts else None

    @property
    def decodable(self) -> bool:
        if not self.parts:
            return False
        if self.codec == "ADPCM_WII":
            return all(p.data is not None and p.header is not None for p in self.parts)
        if self.codec == "AAX":
            return all(p.data is not None for p in self.parts)
        return False

    def part_label(self, part: CsbPart) -> str:
        if len(self.parts) < 2:
            return ""
        return "_loop" if part.loop_flag else "_intro"

    def decode(self, buf: bytes, part: Optional[CsbPart] = None) -> DecodedAudio:
        if not self.decodable:
            raise ValueError(f"CSB codec {self.codec!r} has no decoder")
        part = part or self.parts[0]
        raw = buf[part.data.offset:part.data.offset + part.data.size]
        if self.codec == "AAX":
            return decode_adx(raw)
        nch = max(1, self.channels)
        header = buf[part.header.offset:part.header.offset + part.header.size]
        if len(header) < 0x60 * nch:
            raise ValueError("CSB DSP header block is too short")
        channels: List[bytes] = []
        rate = self.rate
        start = 0
        for ch in range(nch):
            h = header[ch * 0x60:(ch + 1) * 0x60]
            nibbles = struct.unpack_from(">I", h, 4)[0]
            rate = struct.unpack_from(">I", h, 8)[0] or rate
            coefs = list(struct.unpack_from(">16h", h, 0x1C))
            h1, h2 = struct.unpack_from(">hh", h, 0x40)
            count = struct.unpack_from(">I", h, 0)[0] or self.sample_count
            nbytes = (nibbles + 1) // 2
            # Channels are stored back to back, each starting on a DSP frame boundary.
            channels.append(
                decode_dsp_adpcm_stream(raw[start:start + nbytes], coefs, count, h1, h2)
            )
            start += (nbytes + 7) & ~7
        if nch == 1:
            return DecodedAudio(channels[0], rate, 1)
        frames = min(len(c) for c in channels) // 2
        out = bytearray(frames * nch * 2)
        for ch, pcm in enumerate(channels):
            for i in range(frames):
                out[(i * nch + ch) * 2:(i * nch + ch) * 2 + 2] = pcm[i * 2:i * 2 + 2]
        return DecodedAudio(bytes(out), rate, nch)


@dataclass
class CsbArchive:
    tables: Dict[str, int]  # top-level table name -> offset of its @UTF table
    cue_count: int
    synth_count: int
    sounds: List[CsbSound]
    notes: List[str]


def is_csb(data: bytes) -> bool:
    if data[:4] != b"@UTF" or len(data) < 0x40:
        return False
    try:
        return parse_utf_table(data, 0)[0] == "TBLCSB"
    except (ValueError, struct.error):
        return False


def parse_csb(data: bytes) -> CsbArchive:
    name, rows = parse_utf_table(data, 0)
    if name != "TBLCSB":
        raise ValueError(f"Not a CSB archive (root table is {name!r})")
    tables: Dict[str, int] = {}
    parsed: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        blob = row.get("utf")
        if isinstance(blob, UtfBlob) and data[blob.offset:blob.offset + 4] == b"@UTF":
            tables[row["name"]] = blob.offset
            parsed[row["name"]] = parse_utf_table(data, blob.offset)[1]
    notes: List[str] = []
    sounds: List[CsbSound] = []
    sdl_base = tables.get("SOUND_ELEMENT")
    for i, row in enumerate(parsed.get("SOUND_ELEMENT", [])):
        blob = row.get("data")
        codec, parts = "unknown", []
        if isinstance(blob, UtfBlob) and data[blob.offset:blob.offset + 4] == b"@UTF":
            codec, sub_rows = parse_utf_table(data, blob.offset)
            for sub in sub_rows:
                part_data = sub.get("data")
                part_header = sub.get("header")
                parts.append(CsbPart(
                    int(sub.get("lpflg") or 0),
                    part_data if isinstance(part_data, UtfBlob) else None,
                    part_header if isinstance(part_header, UtfBlob) else None,
                ))
        elif isinstance(blob, UtfBlob):
            codec, parts = "raw", [CsbPart(0, blob)]
        sounds.append(CsbSound(
            index=i,
            name=str(row.get("name") or f"sound_{i}"),
            codec=codec,
            channels=int(row.get("nch") or 1),
            rate=int(row.get("sfreq") or 0),
            sample_count=int(row.get("nsmpl") or 0),
            table_offset=blob.offset if isinstance(blob, UtfBlob) else (sdl_base or 0),
            parts=parts,
            streaming=bool(row.get("stmflg")),
        ))
    unsupported = sorted({s.codec for s in sounds if not s.decodable and not s.streaming})
    if unsupported:
        notes.append("No WAV decoder for sound-element codec(s): " + ", ".join(unsupported))
    streaming = sum(1 for s in sounds if s.streaming)
    if streaming:
        notes.append(f"{streaming} streaming sound element(s) live in an external .cpk and are not exported.")
    return CsbArchive(
        tables, len(parsed.get("CUE", [])), len(parsed.get("SYNTH", [])), sounds, notes
    )


# ---------------------------------------------------------------------------
# MDT with big-endian offset table (ADX blocks)
# ---------------------------------------------------------------------------

def mdt_offset_table(data: bytes) -> Optional[Tuple[str, List[int]]]:
    """Return (endian, offsets) when the file starts with a valid MDT offset table."""
    if len(data) < 8:
        return None
    for fmt, endian in (("<I", "little"), (">I", "big")):
        first = struct.unpack_from(fmt, data, 0)[0]
        if first < 4 or first % 4 or first > len(data):
            continue
        offsets = [struct.unpack_from(fmt, data, i * 4)[0] for i in range(first // 4)]
        if offsets[0] != first:
            continue
        if all(
            offsets[i] < offsets[i + 1] for i in range(len(offsets) - 1)
        ) and offsets[-1] + 8 <= len(data):
            return endian, offsets
    return None


def mdt_block_payload(data: bytes, offsets: List[int], index: int, endian: str) -> bytes:
    off = offsets[index]
    end = offsets[index + 1] if index + 1 < len(offsets) else len(data)
    declared = struct.unpack_from("<I" if endian == "little" else ">I", data, off)[0]
    return data[off + 4:min(end, off + 4 + declared)]


def is_adx(payload: bytes) -> bool:
    try:
        parse_adx_header(payload)
        return True
    except (ValueError, struct.error):
        return False


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_audio(path: Path | str, out_dir: Path | str) -> List[Path]:
    """Decode every playable sound of a DTPK, CSB or ADX-MDT file to WAV."""
    path = Path(path)
    out_dir = Path(out_dir)
    data = path.read_bytes()
    written: List[Path] = []
    if is_dtpk(data):
        pack = parse_dtpk(data)
        for s in pack.samples:
            if s.codec == "unknown":
                continue
            dst = out_dir / f"{s.index:03d}_{s.codec}.wav"
            write_pcm_wav(dst, s.decode(data))
            written.append(dst)
        return written
    if is_csb(data):
        for s in parse_csb(data).sounds:
            if not s.decodable:
                continue
            base = f"{s.index:03d}_{_safe_name(s.name.rsplit('/', 1)[-1])}"
            for part in s.parts:
                dst = out_dir / f"{base}{s.part_label(part)}.wav"
                write_pcm_wav(dst, s.decode(data, part))
                written.append(dst)
        return written
    table = mdt_offset_table(data)
    if table is not None:
        endian, offsets = table
        for i in range(len(offsets)):
            payload = mdt_block_payload(data, offsets, i, endian)
            if is_adx(payload):
                dst = out_dir / f"{i:03d}_ADX.wav"
                write_pcm_wav(dst, decode_adx(payload))
                written.append(dst)
        return written
    raise ValueError("export_audio supports DTPK (.dat), CSB and ADX-based MDT files")
