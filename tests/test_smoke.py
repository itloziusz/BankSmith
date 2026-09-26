from __future__ import annotations

import tempfile
from pathlib import Path

import mlt_soundbank_explorer as pkg
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
