from __future__ import annotations

import json
import struct
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List


def _u32le(data: bytes, off: int) -> int:
    if off < 0 or off + 4 > len(data):
        raise ValueError(f"u32le out of range at 0x{off:X}")
    return struct.unpack_from("<I", data, off)[0]


def _u32be(data: bytes, off: int) -> int:
    if off < 0 or off + 4 > len(data):
        raise ValueError(f"u32be out of range at 0x{off:X}")
    return struct.unpack_from(">I", data, off)[0]


def _tag(data: bytes, off: int, n: int = 4) -> str:
    raw = data[off:off + n]
    return "".join(chr(c) if 32 <= c < 127 else "." for c in raw)


def _safe_range(offset: int, size: int, file_size: int) -> bool:
    return 0 <= offset <= file_size and 0 <= size <= file_size - offset


@dataclass
class ProbeEntry:
    index: int
    kind: str
    offset: int
    size: int
    bank_id: int | None = None
    aux_offset: int | None = None
    aux_size: int | None = None
    notes: str = ""


@dataclass
class SoundbankProbe:
    path: str
    family: str
    endian: str
    file_size: int
    editable_audio: bool
    entries: List[ProbeEntry]
    notes: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


def _probe_gcax_mlt(path: Path, data: bytes) -> SoundbankProbe:
    declared = _u32be(data, 12) if len(data) >= 16 else 0
    entries: List[ProbeEntry] = []
    notes: List[str] = []
    if declared != len(data):
        notes.append(f"declared file size 0x{declared:X} differs from actual 0x{len(data):X}")
    pos = 16
    limit = min(len(data), declared or len(data))
    idx = 0
    while pos + 16 <= limit:
        magic = data[pos:pos + 8]
        size = _u32be(data, pos + 12)
        body_end = pos + 16 + size
        if body_end > limit:
            notes.append(f"truncated child at 0x{pos:X}: {_tag(data, pos, 8)} size=0x{size:X}")
            break
        if magic == b"gcaxMPB ":
            entries.append(
                ProbeEntry(
                    idx,
                    "gcaxMPB",
                    pos,
                    16 + size,
                    notes="embedded editable program/sample bank",
                )
            )
            idx += 1
        pos = (body_end + 15) & ~15
    return SoundbankProbe(str(path), "gcaxMLT", "big", len(data), True, entries, notes)


def _probe_gcax_mpb(path: Path, data: bytes) -> SoundbankProbe:
    declared = _u32be(data, 12) if len(data) >= 16 else 0
    body_end = min(len(data), 16 + declared)
    entries: List[ProbeEntry] = []
    notes: List[str] = []
    pos = 16
    i = 0
    while pos + 16 <= body_end:
        magic = data[pos:pos + 8]
        size = _u32be(data, pos + 12)
        end = pos + 16 + size
        if end > body_end:
            notes.append(f"truncated child at 0x{pos:X}: {_tag(data, pos, 8)} size=0x{size:X}")
            break
        entries.append(ProbeEntry(i, _tag(data, pos, 8), pos, 16 + size))
        i += 1
        pos = (end + 15) & ~15
    suffix = len(data) - body_end
    if suffix:
        notes.append(f"preserved trailing padding: 0x{suffix:X} bytes")
    return SoundbankProbe(str(path), "gcaxMPB", "big", len(data), True, entries, notes)


def _probe_smlt(path: Path, data: bytes) -> SoundbankProbe:
    if len(data) < 0x20:
        raise ValueError("SMLT is shorter than 0x20 bytes")
    count = _u32le(data, 8)
    max_count = max(0, (len(data) - 0x20) // 0x20)
    if count > max_count:
        raise ValueError(f"SMLT directory count {count} exceeds file bounds")
    entries: List[ProbeEntry] = []
    notes = [
        "Dreamcast Sound Library multi-unit; structural inspection is enabled, "
        "audio editing is not yet enabled for this family."
    ]
    for i in range(count):
        off = 0x20 + i * 0x20
        kind = _tag(data, off, 4)
        bank_id = data[off + 4] if off + 5 <= len(data) else None
        target_addr = _u32le(data, off + 8)
        target_size = _u32le(data, off + 12)
        file_off = _u32le(data, off + 16)
        file_size = _u32le(data, off + 20)
        embedded = (
            file_off != 0xFFFFFFFF
            and file_size != 0xFFFFFFFF
            and _safe_range(file_off, file_size, len(data))
        )
        if embedded:
            entry_notes = (
                f"load-address=0x{target_addr:X}/0x{target_size:X}; embedded source range"
            )
            entries.append(
                ProbeEntry(
                    i,
                    kind,
                    file_off,
                    file_size,
                    bank_id,
                    target_addr,
                    target_size,
                    entry_notes,
                )
            )
        else:
            entry_notes = (
                f"load-address=0x{target_addr:X}/0x{target_size:X}; no embedded source range"
            )
            entries.append(
                ProbeEntry(
                    i,
                    kind,
                    0,
                    0,
                    bank_id,
                    target_addr,
                    target_size,
                    entry_notes,
                )
            )
    return SoundbankProbe(str(path), "Dreamcast SMLT", "little", len(data), False, entries, notes)


def _probe_smpb(path: Path, data: bytes) -> SoundbankProbe:
    if len(data) < 0x20:
        raise ValueError("SMPB is shorter than 0x20 bytes")
    version = _u32le(data, 4)
    declared_hint = _u32le(data, 8)
    entries: List[ProbeEntry] = []
    notes = [
        f"Dreamcast MIDI program bank (SMPB), version/id word=0x{version:X}.",
        "Structural inspection is enabled; Dreamcast/AICA sample decoding and write-back "
        "are intentionally not treated as gcax DSP-ADPCM.",
    ]
    if declared_hint not in (
        0,
        len(data),
        len(data) - 2,
        len(data) - 4,
        len(data) - 8,
        len(data) - 16,
    ):
        notes.append(f"header size-like field: 0x{declared_hint:X}")

    vals = []
    for off in range(0x10, min(len(data), 0x400), 4):
        v = _u32le(data, off)
        if 0x20 <= v < len(data) and v % 4 == 0:
            vals.append((off, v))
    seen = set()
    for field_off, v in vals:
        if v in seen:
            continue
        seen.add(v)
        entries.append(
            ProbeEntry(
                len(entries),
                "internal-offset",
                v,
                0,
                notes=f"pointer field @0x{field_off:X}",
            )
        )
        if len(entries) >= 128:
            break
    return SoundbankProbe(str(path), "Dreamcast SMPB", "little", len(data), False, entries, notes)


def _probe_mdt(path: Path, data: bytes) -> SoundbankProbe:
    if len(data) < 8:
        raise ValueError("MDT is too short")
    first = _u32le(data, 0)
    if first < 4 or first > len(data) or first % 4:
        raise ValueError(f"MDT first block offset/header size is invalid: 0x{first:X}")
    count = first // 4
    entries: List[ProbeEntry] = []
    notes = [
        "Sonic Shuffle MDT container; blocks are size-prefixed Dreamcast sound-driver banks."
    ]
    offsets = [_u32le(data, i * 4) for i in range(count)]
    prev = -1
    for i, off in enumerate(offsets):
        if off <= prev or off + 8 > len(data):
            raise ValueError(f"MDT block offset {i} is invalid: 0x{off:X}")
        prev = off
        declared = _u32le(data, off)
        kind = _tag(data, off + 4, 4)
        next_off = offsets[i + 1] if i + 1 < len(offsets) else len(data)
        extent = next_off - off
        note = f"size-prefix=0x{declared:X}; payload magic @+4"
        if declared and abs(declared - extent) > 0x20:
            note += f"; table extent=0x{extent:X}"
        entries.append(ProbeEntry(i, kind, off, extent, notes=note))
    return SoundbankProbe(str(path), "Sonic Shuffle MDT", "little", len(data), False, entries, notes)


def inspect_soundbank(path: Path | str) -> SoundbankProbe:
    path = Path(path)
    data = path.read_bytes()
    if data.startswith(b"gcaxMLT "):
        return _probe_gcax_mlt(path, data)
    if data.startswith(b"gcaxMPB "):
        return _probe_gcax_mpb(path, data)
    if data.startswith(b"SMLT"):
        return _probe_smlt(path, data)
    if data.startswith(b"SMPB"):
        return _probe_smpb(path, data)
    if path.suffix.lower() == ".mdt":
        return _probe_mdt(path, data)
    raise ValueError(f"Unsupported soundbank/container signature: {data[:8]!r}")


def format_summary_text(probe: SoundbankProbe, max_entries: int = 80) -> str:
    lines = [
        f"Format: {probe.family}",
        f"Endian: {probe.endian}",
        f"File size: {probe.file_size:,} bytes (0x{probe.file_size:X})",
        (
            "Audio editing in current explorer: "
            + ("yes" if probe.editable_audio else "structural inspection only")
        ),
        f"Entries: {len(probe.entries)}",
    ]
    if probe.notes:
        lines += ["", "Notes:"] + [f"- {n}" for n in probe.notes]
    if probe.entries:
        lines += ["", "Entries:"]
        for e in probe.entries[:max_entries]:
            bank = "" if e.bank_id is None else f" bank={e.bank_id}"
            aux = (
                ""
                if e.aux_offset is None
                else f" aux=0x{e.aux_offset:X}/0x{(e.aux_size or 0):X}"
            )
            note = "" if not e.notes else f" | {e.notes}"
            lines.append(
                f"[{e.index:03d}] {e.kind:<12} off=0x{e.offset:X} "
                f"size=0x{e.size:X}{bank}{aux}{note}"
            )
        if len(probe.entries) > max_entries:
            lines.append(f"... {len(probe.entries) - max_entries} more entries")
    return "\n".join(lines)


def extract_mdt_blocks(path: Path | str, out_dir: Path | str) -> List[Path]:
    path = Path(path)
    out_dir = Path(out_dir)
    probe = inspect_soundbank(path)
    if probe.family != "Sonic Shuffle MDT":
        raise ValueError("extract_mdt_blocks requires an MDT file")
    data = path.read_bytes()
    out_dir.mkdir(parents=True, exist_ok=True)
    out: List[Path] = []
    ext_by_kind = {"SMSB": ".msb", "SMPB": ".mpb", "SMDB": ".mdb"}
    for entry in probe.entries:
        raw = data[entry.offset:entry.offset + entry.size]
        payload = raw[4:] if raw[4:8].decode("ascii", "ignore") == entry.kind else raw
        ext = ext_by_kind.get(entry.kind, ".bin")
        dst = out_dir / f"{entry.index:03d}_{entry.kind}{ext}"
        dst.write_bytes(payload)
        out.append(dst)
    return out
