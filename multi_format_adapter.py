from __future__ import annotations

import struct
import tempfile
from pathlib import Path
from typing import Optional

from MLT_Soundbank_Explorer import MLTBank
from soundbank_formats import SoundbankProbe, inspect_soundbank


def _p32be(value: int) -> bytes:
    return struct.pack(">I", int(value) & 0xFFFFFFFF)


def _standalone_gcax_mpb_declared_span(data: bytes) -> int:
    if len(data) < 16 or not data.startswith(b"gcaxMPB "):
        raise ValueError("Not a standalone gcaxMPB file")
    body_size = struct.unpack_from(">I", data, 12)[0]
    span = 16 + body_size
    if span > len(data):
        raise ValueError(
            f"gcaxMPB declared size exceeds file length (0x{span:X} > 0x{len(data):X})"
        )
    return span


def wrap_gcax_mpb_as_mlt(mpb_bytes: bytes, bank_id: int = 0) -> bytes:
    """Wrap one standalone gcaxMPB in the smallest MLT accepted by MLTBank.

    Standalone padding after the MPB's declared span is deliberately excluded
    from the synthetic container and preserved separately by
    StandaloneGCAXMPBBank.
    """
    span = _standalone_gcax_mpb_declared_span(mpb_bytes)
    mpb = mpb_bytes[:span]

    # Top-level layout:
    #   0x00 gcaxMLT header
    #   0x10 gcaxMLTM chunk (0x10-byte body / one directory record)
    #   0x30 gcaxMPB chunk
    mpb_pos = 0x30
    pointer_rel = mpb_pos - 0x20

    record = bytearray(0x10)
    record[0] = 1  # type 1 = MPB
    record[4] = int(bank_id) & 0xFF
    record[8:12] = _p32be(pointer_rel)

    mltm = bytearray()
    mltm += b"gcaxMLTM"
    mltm += b"\x00\x00\x00\x00"
    mltm += _p32be(len(record))
    mltm += record

    result = bytearray()
    result += b"gcaxMLT "
    result += b"\x00\x00\x00\x00"
    result += b"\x00\x00\x00\x00"  # file size patched below
    result += mltm
    if len(result) != mpb_pos:
        raise AssertionError(f"synthetic MPB offset mismatch: 0x{len(result):X}")
    result += mpb
    result[12:16] = _p32be(len(result))
    return bytes(result)


class StandaloneGCAXMPBBank(MLTBank):
    """Expose a standalone GameCube gcaxMPB through the existing MLT editor.

    The original editor already contains the mature MPBP/MPBW DSP-ADPCM parser,
    preview/export, replacement encoder, validation and rebuild code. This
    adapter supplies only the missing outer MLT directory, then strips that
    synthetic wrapper again when saving.
    """

    def __init__(self, path: Path):
        self.standalone_path = Path(path)
        raw = self.standalone_path.read_bytes()
        span = _standalone_gcax_mpb_declared_span(raw)
        self.standalone_suffix = raw[span:]

        wrapped = wrap_gcax_mpb_as_mlt(raw)
        tmp = tempfile.NamedTemporaryFile(
            prefix="mlt_explorer_mpb_",
            suffix=".mlt",
            delete=False,
        )
        try:
            tmp.write(wrapped)
            tmp.close()
            self._synthetic_mlt_path = Path(tmp.name)
            super().__init__(self._synthetic_mlt_path)
        except Exception:
            try:
                tmp.close()
            except Exception:
                pass
            Path(tmp.name).unlink(missing_ok=True)
            raise

        # Present the real source path to callers/UI.
        self.path = self.standalone_path

    def __del__(self) -> None:
        try:
            self._synthetic_mlt_path.unlink(missing_ok=True)
        except Exception:
            pass

    def build_repacked_mpb(self) -> bytes:
        synthetic_mlt = super().build_repacked()
        mpb_pos = synthetic_mlt.find(b"gcaxMPB ", 16)
        if mpb_pos < 0 or mpb_pos + 16 > len(synthetic_mlt):
            raise ValueError("Rebuilt synthetic MLT does not contain gcaxMPB")
        body_size = struct.unpack_from(">I", synthetic_mlt, mpb_pos + 12)[0]
        end = mpb_pos + 16 + body_size
        if end > len(synthetic_mlt):
            raise ValueError("Rebuilt gcaxMPB extends beyond synthetic MLT")
        return synthetic_mlt[mpb_pos:end] + self.standalone_suffix

    def build_repacked(self) -> bytes:
        return self.build_repacked_mpb()

    def save_as(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.build_repacked_mpb())


def open_editable_bank(path: Path | str):
    """Open an editable gcax bank, either MLT or standalone MPB."""
    path = Path(path)
    head = path.read_bytes()[:8]
    if head == b"gcaxMLT ":
        return MLTBank(path)
    if head == b"gcaxMPB ":
        return StandaloneGCAXMPBBank(path)
    probe = inspect_soundbank(path)
    raise ValueError(
        f"{probe.family} is recognised, but is currently structural-inspection-only; "
        "Dreamcast/AICA data must not be decoded with the GameCube DSP-ADPCM path."
    )


def inspect_any(path: Path | str) -> SoundbankProbe:
    return inspect_soundbank(path)
