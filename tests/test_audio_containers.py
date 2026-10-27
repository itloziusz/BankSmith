from __future__ import annotations

import struct
import wave
from pathlib import Path
from typing import List, Tuple

import pytest

from mlt_soundbank_explorer.adapters import open_editable_bank
from mlt_soundbank_explorer.audio_containers import (
    decode_adx,
    decode_dsp_adpcm_stream,
    decode_ps_adpcm,
    export_audio,
    parse_csb,
    parse_dtpk,
)
from mlt_soundbank_explorer.formats import extract_mdt_blocks, inspect_soundbank

COEFS = [0x0800, 0] + [0] * 14


def _dsp_frames(count: int) -> bytes:
    # predictor 0, scale 0, nibbles ramp: decodes to nibble * 1 with coef 0x0800 on hist1.
    return b"".join(bytes([0x00]) + bytes([0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x11]) for _ in range(count))


def _gc_dtpk(frames: int = 2) -> bytes:
    data = _dsp_frames(frames)
    data_off = 0x1000
    body = bytearray(0x20)  # chunk header (0x10) + null block (0x10)
    body += data
    body += b"\x00" * ((-len(body)) % 0x20)
    tbld = bytearray(0x1000 - 0x60)
    tbld[0:8] = b"gcaxTBLD"
    ptrs = [0xC0, 0xF8, 0x170, 0x274, 0x300, 0, 0, 0, 0x670, 0x700]
    for i, p in enumerate(ptrs):
        struct.pack_into(">I", tbld, 0x98 - 0x60 + 4 * i, p)
    rec = 0x670 - 0x60
    struct.pack_into(">II", tbld, rec, 0, 1)  # count-1 = 0 -> one record
    r = rec + 8
    nibbles = frames * 16
    struct.pack_into(">II", tbld, r, 2, nibbles - 1)
    struct.pack_into(">16h", tbld, r + 8, *COEFS)
    struct.pack_into(">I", tbld, r + 0x34, 0x200)
    struct.pack_into(">I", tbld, r + 0x38, 22050 << 16)
    struct.pack_into(">I", tbld, r + 0x3C, len(data))
    struct.pack_into(">I", tbld, r + 0x40, 0x20 + len(data))
    head = bytearray(0x60)
    head[0:8] = b"gcaxDTPK"
    head[8:12] = bytes([2, 0x4B, 0, 0])
    total = data_off + len(body)
    struct.pack_into(">IIIII", head, 0x0C, total, 0x940, 0, len(body) - 0x10, data_off)
    chunk = bytearray(b"gcaxPCMD" + bytes([2, 0x4B, 0, 0]) + struct.pack(">I", len(body) - 0x10))
    body[0:16] = chunk
    return bytes(head + tbld + body)


def _ps2_dtpk() -> bytes:
    block0 = bytes(16)
    block1 = bytes([0x00, 0x00]) + bytes([0x11] * 14)
    block2 = bytes([0x00, 0x01]) + bytes([0x00] * 14)
    sample = block1 + block2
    data_off = 0x800
    body = bytearray(0x10) + block0 + sample
    body += b"\x00" * ((-len(body)) % 0x10)
    tbld = bytearray(0x800 - 0x60)
    tbld[0:8] = b"ps2_TBLD"
    ptrs = [0xC0, 0xF8, 0x150, 0x2D4, 0x334, 0, 0, 0, 0x5AC, 0x5C0]
    for i, p in enumerate(ptrs):
        struct.pack_into("<I", tbld, 0x98 - 0x60 + 4 * i, p)
    t = 0x5AC - 0x60
    struct.pack_into("<I", tbld, t, 0)
    struct.pack_into("<IIHHI", tbld, t + 4, 0x10, 0, 0, 11025, len(sample))
    head = bytearray(0x60)
    head[0:8] = b"ps2_DTPK"
    head[8:12] = bytes([2, 0x4C, 1, 0])
    total = data_off + len(body)
    struct.pack_into("<IIIII", head, 0x0C, total, 0x640, 0, len(body) - 0x10, data_off)
    body[0:16] = b"ps2_VAGD" + bytes([2, 0x4C, 1, 0]) + struct.pack("<I", len(body) - 0x10)
    return bytes(head + tbld + body)


def _adx_stream(frames: int = 3, channels: int = 1) -> bytes:
    header = bytearray(0x24)
    header[0:2] = b"\x80\x00"
    struct.pack_into(">H", header, 2, 0x20)
    header[4:8] = bytes([3, 18, 4, channels])
    struct.pack_into(">II", header, 8, 22050, frames * 32)
    struct.pack_into(">H", header, 16, 500)
    header[18] = 3
    header[0x1E:0x24] = b"(c)CRI"
    frame = struct.pack(">H", 0) + bytes([0x11] * 16)
    return bytes(header) + frame * frames * channels


def _be_mdt(blocks: List[bytes]) -> bytes:
    table = bytearray(len(blocks) * 4)
    out = bytearray()
    pos = len(table)
    for i, b in enumerate(blocks):
        struct.pack_into(">I", table, i * 4, pos)
        entry = struct.pack(">I", len(b)) + b
        out += entry
        pos += len(entry)
    return bytes(table + out)


def _utf_table(name: str, columns: List[Tuple[str, str]], rows: List[List[object]]) -> bytes:
    """Minimal @UTF writer: every column is stored per row (s=string, I=u32, H=u16, B=u8, b=blob)."""
    strings = bytearray(b"<NULL>\0")
    pool = {}

    def sref(text: str) -> int:
        if text not in pool:
            pool[text] = len(strings)
            strings.extend(text.encode() + b"\0")
        return pool[text]

    table_name = sref(name)
    type_code = {"B": 0, "H": 2, "I": 4, "s": 0xA, "b": 0xB}
    size = {"B": 1, "H": 2, "I": 4, "s": 4, "b": 8}
    schema = bytearray()
    for col, kind in columns:
        schema += bytes([0x50 | type_code[kind]]) + struct.pack(">I", sref(col))
    blob_data = bytearray()
    row_bytes = bytearray()
    for row in rows:
        for (_, kind), value in zip(columns, row):
            if kind == "s":
                row_bytes += struct.pack(">I", sref(str(value)))
            elif kind == "b":
                row_bytes += struct.pack(">II", len(blob_data), len(value))  # type: ignore[arg-type]
                blob_data += value  # type: ignore[operator]
                blob_data += b"\0" * ((-len(blob_data)) % 4)
            else:
                row_bytes += struct.pack({"B": ">B", "H": ">H", "I": ">I"}[kind], value)
    row_len = sum(size[k] for _, k in columns)
    rows_off = 0x18 + len(schema)
    str_off = rows_off + len(row_bytes)
    data_off = str_off + len(strings)
    body = (
        struct.pack(">HHIIIHHI", 1, rows_off, str_off, data_off, table_name,
                    len(columns), row_len, len(rows))
        + schema + row_bytes + bytes(strings) + bytes(blob_data)
    )
    body += b"\0" * ((-len(body)) % 4)
    return b"@UTF" + struct.pack(">I", len(body)) + body


def _csb(dsp_pcm_frames: int = 2) -> bytes:
    data = _dsp_frames(dsp_pcm_frames)
    header = bytearray(0x60)
    struct.pack_into(">II", header, 0, dsp_pcm_frames * 14, dsp_pcm_frames * 16)
    struct.pack_into(">I", header, 8, 22050)
    struct.pack_into(">16h", header, 0x1C, *COEFS)
    adpcm = _utf_table(
        "ADPCM_WII",
        [("sfreq", "I"), ("nsmpl", "I"), ("nch", "B"), ("data", "b"), ("header", "b")],
        [[22050, dsp_pcm_frames * 14, 1, data, bytes(header)]],
    )
    sdl = _utf_table(
        "TBLSDL",
        [("name", "s"), ("data", "b"), ("fmt", "B"), ("nch", "B"), ("sfreq", "I"), ("nsmpl", "I")],
        [["Synth/Test/00000-beep_aif", adpcm, 4, 1, 22050, dsp_pcm_frames * 14]],
    )
    cue = _utf_table("TBLCUE", [("name", "s")], [["beep"]])
    return _utf_table(
        "TBLCSB",
        [("name", "s"), ("ttype", "B"), ("utf", "b")],
        [["CUE", 1, cue], ["SOUND_ELEMENT", 4, sdl]],
    )


def _wav_info(path: Path) -> Tuple[int, int, int]:
    with wave.open(str(path), "rb") as wf:
        return wf.getnchannels(), wf.getframerate(), wf.getnframes()


def test_dsp_decoder_follows_nibbles() -> None:
    pcm = decode_dsp_adpcm_stream(_dsp_frames(1), COEFS, 14)
    samples = struct.unpack("<14h", pcm)
    assert samples[0] == 1 and samples[1] == 2  # 0x800 coefficient carries history forward


def test_ps_adpcm_stops_at_end_flag() -> None:
    blocks = bytes(2) + bytes([0x11] * 14) + bytes([0, 1]) + bytes(14) + bytes([0, 0]) + bytes(14)
    pcm, _ = decode_ps_adpcm(blocks)
    assert len(pcm) == 2 * 28 * 2  # the block after the end flag is ignored


def test_gcax_dtpk_probe_and_export(tmp_path: Path) -> None:
    path = tmp_path / "pack.dat"
    path.write_bytes(_gc_dtpk(2))
    pack = parse_dtpk(path.read_bytes())
    assert pack.platform == "gcax" and len(pack.samples) == 1
    assert pack.samples[0].rate == 22050 and pack.samples[0].codec == "dsp-adpcm"
    probe = inspect_soundbank(path)
    assert probe.family == "GameCube/Wii gcaxDTPK" and not probe.editable_audio
    files = export_audio(path, tmp_path / "out")
    assert len(files) == 1
    assert _wav_info(files[0])[1] == 22050


def test_ps2_dtpk_probe_and_export(tmp_path: Path) -> None:
    path = tmp_path / "pack.dat"
    path.write_bytes(_ps2_dtpk())
    probe = inspect_soundbank(path)
    assert probe.family == "PS2 DTPK" and probe.endian == "little"
    assert probe.entries[0].kind == "ps-adpcm"
    files = export_audio(path, tmp_path / "out")
    assert _wav_info(files[0]) == (1, 11025, 56)


def test_dtpk_rejects_truncated_sample_data() -> None:
    with pytest.raises(ValueError):
        parse_dtpk(_gc_dtpk(2)[:0x1020])


def test_adx_decode_and_big_endian_mdt(tmp_path: Path) -> None:
    audio = decode_adx(_adx_stream(3, channels=2))
    assert audio.channels == 2 and audio.rate == 22050
    path = tmp_path / "bgm.mdt"
    path.write_bytes(_be_mdt([_adx_stream(3), _adx_stream(2, channels=2)]))
    probe = inspect_soundbank(path)
    assert probe.family == "CRI ADX MDT" and probe.endian == "big"
    assert [e.kind for e in probe.entries] == ["ADX", "ADX"]
    with pytest.raises(ValueError, match="no editable backend"):
        open_editable_bank(path)
    blocks = extract_mdt_blocks(path, tmp_path / "blocks")
    assert [b.suffix for b in blocks] == [".adx", ".adx"]
    assert blocks[0].read_bytes() == _adx_stream(3)
    files = export_audio(path, tmp_path / "wav")
    assert [_wav_info(f)[0] for f in files] == [1, 2]


def test_csb_parse_and_export(tmp_path: Path) -> None:
    path = tmp_path / "bank.csb"
    path.write_bytes(_csb(2))
    csb = parse_csb(path.read_bytes())
    assert csb.cue_count == 1 and len(csb.sounds) == 1
    assert csb.sounds[0].codec == "ADPCM_WII" and csb.sounds[0].decodable
    probe = inspect_soundbank(path)
    assert probe.family == "CRI CSB" and probe.entries[0].kind == "ADPCM_WII"
    files = export_audio(path, tmp_path / "wav")
    assert files[0].name == "000_00000-beep_aif.wav"
    assert _wav_info(files[0]) == (1, 22050, 28)


def test_csb_aax_wrapper_exports_intro_and_loop(tmp_path: Path) -> None:
    aax = _utf_table(
        "AAX",
        [("data", "b"), ("lpflg", "B")],
        [[_adx_stream(2), 0], [_adx_stream(3), 1]],
    )
    sdl = _utf_table(
        "TBLSDL",
        [("name", "s"), ("data", "b"), ("fmt", "B"), ("nch", "B"), ("sfreq", "I"), ("nsmpl", "I")],
        [["Synth/Test/bgm", aax, 0, 1, 22050, 96]],
    )
    path = tmp_path / "bgm.csb"
    path.write_bytes(_utf_table("TBLCSB", [("name", "s"), ("ttype", "B"), ("utf", "b")],
                                [["SOUND_ELEMENT", 4, sdl]]))
    sound = parse_csb(path.read_bytes()).sounds[0]
    assert sound.codec == "AAX" and len(sound.parts) == 2 and sound.decodable
    files = export_audio(path, tmp_path / "wav")
    assert [f.name for f in files] == ["000_bgm_intro.wav", "000_bgm_loop.wav"]
    assert [_wav_info(f)[2] for f in files] == [64, 96]
