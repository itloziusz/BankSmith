from __future__ import annotations

import tempfile
from pathlib import Path

import mlt_soundbank_explorer as pkg
import banksmith as banksmith_pkg
from multi_format_adapter import wrap_gcax_mpb_as_mlt
from soundbank_formats import inspect_soundbank


def test_public_api_imports() -> None:
    assert pkg.MLTBank is not None
    assert pkg.MLTExplorerApp is not None
    assert hasattr(pkg.MLTBank, "render_sample_for_wav")
    assert hasattr(pkg.MLTBank, "build_repacked")
    assert hasattr(pkg.MLTExplorerApp, "preview_selected")
    assert hasattr(pkg.MLTExplorerApp, "save_project_json")


def test_minimal_smlt_probe() -> None:
    data = bytearray(0x20)
    data[0:4] = b"SMLT"
    data[8:12] = (0).to_bytes(4, "little")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "empty.mlt"
        path.write_bytes(data)
        probe = inspect_soundbank(path)
        assert probe.family == "Dreamcast SMLT"
        assert probe.endian == "little"
        assert probe.entries == []


def test_standalone_gcax_mpb_wrapper() -> None:
    mpb = bytearray(16)
    mpb[0:8] = b"gcaxMPB "
    mpb[12:16] = (0).to_bytes(4, "big")
    wrapped = wrap_gcax_mpb_as_mlt(bytes(mpb), bank_id=3)
    assert wrapped.startswith(b"gcaxMLT ")
    assert b"gcaxMLTM" in wrapped
    assert wrapped[0x30:0x38] == b"gcaxMPB "
    assert int.from_bytes(wrapped[12:16], "big") == len(wrapped)


def test_aica_adpcm_codec_shape() -> None:
    import math
    import struct
    from mlt_soundbank_explorer.dreamcast_codec import encode_aica_adpcm, decode_aica_adpcm

    samples = [int(12000 * math.sin(i * 0.19)) for i in range(257)]
    pcm = struct.pack("<" + "h" * len(samples), *samples)
    encoded = encode_aica_adpcm(pcm)
    assert len(encoded) == (len(samples) + 1) // 2
    decoded = decode_aica_adpcm(encoded, len(samples))
    assert len(decoded) == len(pcm)
    assert any(decoded)


def test_dreamcast_dispatch_minimal_smlt() -> None:
    from mlt_soundbank_explorer.formats import inspect_soundbank

    data = bytearray(0x20)
    data[0:4] = b"SMLT"
    data[8:12] = (0).to_bytes(4, "little")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "empty.mlt"
        path.write_bytes(data)
        probe = inspect_soundbank(path)
        assert probe.family == "Dreamcast SMLT"
        assert probe.editable_audio is True


def test_dreamcast_public_backends() -> None:
    assert pkg.DreamcastStandaloneMPBBank is not None
    assert pkg.DreamcastSMLTBank is not None
    assert pkg.DreamcastMDTBank is not None


def test_banksmith_brand_api() -> None:
    assert banksmith_pkg.BankSmithApp is pkg.BankSmithApp
    assert banksmith_pkg.BankSmithApp.__name__ == "BankSmithApp"
    assert banksmith_pkg.APP_NAME == "BankSmith"
    assert banksmith_pkg.APP_TITLE.startswith("BankSmith")
    assert banksmith_pkg.MLTBank is pkg.MLTBank
    assert banksmith_pkg.DreamcastMDTBank is pkg.DreamcastMDTBank


def test_banksmith_primary_launcher_exists() -> None:
    launcher = Path(__file__).resolve().parents[1] / "BankSmith.py"
    assert launcher.exists()
    text = launcher.read_text(encoding="utf-8")
    assert "BankSmith" in text
    assert "from banksmith import" in text


def test_no_legacy_product_branding() -> None:
    root = Path(__file__).resolve().parents[1]
    forbidden = "MLT Soundbank" + " Explorer"
    extensions = {".py", ".md", ".yml", ".yaml", ".txt", ".toml"}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        if any(part in {".git", ".pytest_cache", "__pycache__"} for part in path.parts):
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        assert forbidden not in content, f"legacy product branding remains in {path}"


def test_gcax_55_padding_and_standalone_mpb_open() -> None:
    from mlt_soundbank_explorer.adapters import open_editable_bank, StandaloneGCAXMPBBank

    def chunk(magic: bytes, body: bytes) -> bytes:
        assert len(magic) == 8
        return magic + b"\x00\x00\x00\x00" + len(body).to_bytes(4, "big") + body

    # Minimal valid gcaxMPB with the exact 0x55 ('U') padding pattern reported
    # in issues #1/#2 between MPBW and MPBP.
    mpbw = chunk(b"gcaxMPBW", b"")
    mpbp_body = bytearray(0x20)  # zero samples/programs is a valid empty bank
    mpbp = chunk(b"gcaxMPBP", bytes(mpbp_body))
    mpb_body = mpbw + (b"\x55" * 0x20) + mpbp
    standalone = chunk(b"gcaxMPB ", mpb_body) + (b"\x55" * 0x10)

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "issue_fixture.mpb"
        path.write_bytes(standalone)

        bank = open_editable_bank(path)
        assert isinstance(bank, StandaloneGCAXMPBBank)
        assert bank.samples == []
        assert bank.build_repacked() == standalone
