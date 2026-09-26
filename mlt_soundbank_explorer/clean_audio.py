from __future__ import annotations

import csv
import math
import os
import queue
import re
import struct
import tempfile
from pathlib import Path
from typing import Dict, List

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .core import *
from .audio import *
from .bank import *
from .gui import *

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


def _bank_render_sample_for_wav(
    self: MLTBank,
    index: int,
    *,
    pitch_correct: bool = False,
    trigger_note: int = DEFAULT_TRIGGER_NOTE,
    clean: bool = True,
    fixed_output_rate: bool = True,
    output_rate: int = DEFAULT_CLEAN_RENDER_RATE,
    dc_filter: bool = True,
    decrackle: bool = True,
    decrackle_strength: str = "Light",
    limiter: bool = True,
    edge_fade: bool = True,
) -> Tuple[bytes, int, Dict[str, float | int | str]]:
    pcm = self.decode_sample(index)
    src_rate_exact = self.audition_sample_rate_exact(index, pitch_correct=pitch_correct, trigger_note=trigger_note)
    src_rate_header = self.audition_sample_rate(index, pitch_correct=pitch_correct, trigger_note=trigger_note)
    if not clean:
        return pcm, src_rate_header, {"input_rate_exact": f"{src_rate_exact:.6f}", "input_rate_header": src_rate_header, "output_rate": src_rate_header, "clean": 0, **audio_stats_from_pcm(pcm)}
    pcm2, out_rate, diag = clean_pcm16_for_audition(
        pcm,
        src_rate_exact,
        output_rate=output_rate,
        fixed_output_rate=fixed_output_rate,
        dc_filter=dc_filter,
        decrackle=decrackle,
        decrackle_strength=decrackle_strength,
        limiter=limiter,
        edge_fade=edge_fade,
    )
    diag["clean"] = 1
    return pcm2, out_rate, diag


def _bank_render_loop_preview_for_wav(
    self: MLTBank,
    index: int,
    preview_seconds: int = 20,
    *,
    pitch_correct: bool = False,
    trigger_note: int = DEFAULT_TRIGGER_NOTE,
    loop_declick: bool = True,
    crossfade_ms: float = 3.0,
    zero_cross: bool = True,
    trim_silence: bool = True,
    clean: bool = True,
    fixed_output_rate: bool = True,
    output_rate: int = DEFAULT_CLEAN_RENDER_RATE,
    dc_filter: bool = True,
    decrackle: bool = True,
    decrackle_strength: str = "Light",
    limiter: bool = True,
    edge_fade: bool = True,
) -> Tuple[bytes, int, Dict[str, float | int | str]]:
    src_rate_exact = self.audition_sample_rate_exact(index, pitch_correct=pitch_correct, trigger_note=trigger_note)
    src_rate_header = self.audition_sample_rate(index, pitch_correct=pitch_correct, trigger_note=trigger_note)
    pcm = self.build_loop_preview_pcm(
        index,
        preview_seconds,
        pitch_correct=pitch_correct,
        trigger_note=trigger_note,
        declick=loop_declick,
        crossfade_ms=crossfade_ms,
        zero_cross=zero_cross,
        trim_silence=trim_silence,
    )
    if not clean:
        return pcm, src_rate_header, {"input_rate_exact": f"{src_rate_exact:.6f}", "input_rate_header": src_rate_header, "output_rate": src_rate_header, "clean": 0, **audio_stats_from_pcm(pcm)}
    pcm2, out_rate, diag = clean_pcm16_for_audition(
        pcm,
        src_rate_exact,
        output_rate=output_rate,
        fixed_output_rate=fixed_output_rate,
        dc_filter=dc_filter,
        decrackle=decrackle,
        decrackle_strength=decrackle_strength,
        limiter=limiter,
        edge_fade=edge_fade,
    )
    diag["clean"] = 1
    return pcm2, out_rate, diag


def _bank_export_sample_rendered(self: MLTBank, index: int, path: Path, **kwargs) -> None:
    pcm, rate, _diag = self.render_sample_for_wav(index, **kwargs)
    write_wav(path, pcm, rate)


def _bank_export_loop_preview_rendered(self: MLTBank, index: int, path: Path, preview_seconds: int = 20, **kwargs) -> None:
    pcm, rate, _diag = self.render_loop_preview_for_wav(index, preview_seconds=preview_seconds, **kwargs)
    write_wav(path, pcm, rate)


def _bank_export_all_rendered(self: MLTBank, out_dir: Path, **kwargs) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for s in self.samples:
        safe_alias = self.safe_alias(s.index)
        pcm, rate, diag = self.render_sample_for_wav(s.index, **kwargs)
        name = f"{s.index:03d}_{safe_alias}_{rate}Hz"
        if s.loop_flag:
            name += "_loop"
        wav_path = out_dir / f"{name}.wav"
        write_wav(wav_path, pcm, rate)
        row = {
            "index": s.index,
            "filename": wav_path.name,
            "alias": s.alias,
            "stored_rate_word_x2": s.current_sample_rate,
            "base_rate_exact_hz": f"{s.current_base_sample_rate_exact:.6f}",
            "render_rate": rate,
            "root_key": self.sample_root_key(s.index) if self.sample_root_key(s.index) is not None else "",
            "trigger_note": kwargs.get("trigger_note", DEFAULT_TRIGGER_NOTE),
            "loop_flag": int(s.loop_flag),
            "usage": ";".join(s.usage),
        }
        row.update(diag)
        rows.append(row)
    if rows:
        fields = sorted({k for r in rows for k in r.keys()})
        with (out_dir / f"{self.path.stem}_clean_audio_metadata.csv").open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader(); writer.writerows(rows)
    self.write_alias_csv(out_dir / f"{self.path.stem}_aliases.csv")


def _bank_write_audio_quality_report_csv(self: MLTBank, path: Path, **kwargs) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for s in self.samples:
        _pcm, rate, diag = self.render_sample_for_wav(s.index, **kwargs)
        row = {
            "index": s.index,
            "alias": s.alias,
            "stored_rate_word_x2": s.current_sample_rate,
            "base_rate_exact_hz": f"{s.current_base_sample_rate_exact:.6f}",
            "audition_or_render_rate": rate,
            "loop_flag": int(s.loop_flag),
            "usage": ";".join(s.usage),
            "root_key": self.sample_root_key(s.index) if self.sample_root_key(s.index) is not None else "",
        }
        row.update(diag)
        # Warnings written to the quality report.
        warnings = []
        if int(diag.get("pre_clipped_samples", 0)) > 0:
            warnings.append("source clips/rasps possible")
        if int(diag.get("pre_big_jump_count", 0)) > 0:
            warnings.append("large sample jumps")
        if int(diag.get("post_clipped_samples", 0)) > 0:
            warnings.append("post still clips")
        row["warnings"] = "; ".join(warnings)
        rows.append(row)
    fields = sorted({k for r in rows for k in r.keys()}) or ["empty"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)


# Add clean-render methods without changing the repacker.
MLTBank.render_sample_for_wav = _bank_render_sample_for_wav  # type: ignore[attr-defined]
MLTBank.render_loop_preview_for_wav = _bank_render_loop_preview_for_wav  # type: ignore[attr-defined]
MLTBank.export_sample_rendered = _bank_export_sample_rendered  # type: ignore[attr-defined]
MLTBank.export_loop_preview_rendered = _bank_export_loop_preview_rendered  # type: ignore[attr-defined]
MLTBank.export_all_rendered = _bank_export_all_rendered  # type: ignore[attr-defined]
MLTBank.write_audio_quality_report_csv = _bank_write_audio_quality_report_csv  # type: ignore[attr-defined]


# GUI additions
_OLD_APP_INIT = MLTExplorerApp.__init__
_OLD_BUILD_UI = MLTExplorerApp._build_ui
_OLD_PREVIEW_SELECTED = MLTExplorerApp.preview_selected
_OLD_PREVIEW_LOOP_SELECTED = MLTExplorerApp.preview_loop_selected
_OLD_EXPORT_SELECTED = MLTExplorerApp.export_selected
_OLD_EXPORT_LOOP_PREVIEW_SELECTED = MLTExplorerApp.export_loop_preview_selected
_OLD_EXPORT_ALL = MLTExplorerApp.export_all
_OLD_SAVE_PROJECT_JSON = MLTExplorerApp.save_project_json
_OLD_LOAD_PROJECT_JSON = MLTExplorerApp.load_project_json


def _app_init(self: MLTExplorerApp, root: tk.Tk) -> None:
    self.root = root
    self.root.title("MLT Soundbank Explorer")
    self.root.geometry("1220x760")
    self.bank = None
    self.current_temp_wav = None
    self._play_generation = 0
    self._preview_process = None
    self._background_active = False
    self._closing = False
    self._settings_refresh_job = None
    self._ui_queue = queue.Queue()
    self._ui_queue_job = None
    self.status_var = tk.StringVar(value="Open an MLT file to begin.")
    self.filter_var = tk.StringVar()
    self.loop_preview_seconds_var = tk.IntVar(value=20)
    self.loop_declick_var = tk.BooleanVar(value=True)
    self.loop_zero_cross_var = tk.BooleanVar(value=True)
    self.loop_trim_silence_var = tk.BooleanVar(value=True)
    self.loop_crossfade_ms_var = tk.DoubleVar(value=3.0)
    self.pitch_correct_var = tk.BooleanVar(value=False)
    self.trigger_note_var = tk.IntVar(value=DEFAULT_TRIGGER_NOTE)
    self.preserve_bank_rate_var = tk.BooleanVar(value=True)
    self.detail_var = tk.StringVar(value="No sample selected.")
    self.view_filter_var = tk.StringVar(value="All samples")
    self.rate_filter_var = tk.StringVar(value="Any stored rate word")
    self.program_filter_var = tk.StringVar(value="All programs")
    self.pitch_preset_var = tk.StringVar(value="Base stored/2 rate")
    # Audio cleanup controls
    self.clean_audition_var = tk.BooleanVar(value=True)
    self.fixed_render_rate_var = tk.BooleanVar(value=True)
    self.render_rate_var = tk.IntVar(value=DEFAULT_CLEAN_RENDER_RATE)
    self.dc_filter_var = tk.BooleanVar(value=True)
    self.decrackle_filter_var = tk.BooleanVar(value=True)
    self.decrackle_strength_var = tk.StringVar(value="Light")
    self.limiter_var = tk.BooleanVar(value=True)
    self.edge_fade_var = tk.BooleanVar(value=True)
    self._build_ui()
    self.root.protocol("WM_DELETE_WINDOW", self._on_close)
    self._start_ui_queue_pump()


def _build_ui_with_audio_tools(self: MLTExplorerApp) -> None:
    _OLD_BUILD_UI(self)
    # Add the audio cleanup menu.
    try:
        menubar = self.root.nametowidget(self.root.cget("menu"))
        clean_menu = tk.Menu(menubar, tearoff=False)
        clean_menu.add_checkbutton(label="Clean audition/render WAVs", variable=self.clean_audition_var)
        clean_menu.add_checkbutton(label="Render to fixed 44.1/48 kHz", variable=self.fixed_render_rate_var)
        clean_menu.add_separator()
        for rate in CLEAN_RENDER_RATES:
            clean_menu.add_radiobutton(label=f"Render rate: {rate} Hz", variable=self.render_rate_var, value=rate)
        clean_menu.add_separator()
        clean_menu.add_checkbutton(label="Remove DC offset", variable=self.dc_filter_var)
        clean_menu.add_checkbutton(label="De-crackle isolated spikes", variable=self.decrackle_filter_var)
        for value in ("Light", "Medium", "Strong"):
            clean_menu.add_radiobutton(label=f"De-crackle strength: {value}", variable=self.decrackle_strength_var, value=value)
        clean_menu.add_checkbutton(label="Safety limiter / headroom", variable=self.limiter_var)
        clean_menu.add_checkbutton(label="Tiny edge fade", variable=self.edge_fade_var)
        clean_menu.add_separator()
        clean_menu.add_command(label="Export All Clean WAV...", command=self.export_all_clean)
        clean_menu.add_command(label="Save Audio Quality Report CSV...", command=self.save_audio_quality_report_csv)
        menubar.add_cascade(label="Audio Clean", menu=clean_menu)
    except Exception:
        pass
    # Add quick cleanup controls above the status line.
    try:
        strip = ttk.Frame(self.root, padding=(8, 2))
        strip.pack(side=tk.BOTTOM, fill=tk.X, before=self.root.children.get('!label'))
    except Exception:
        strip = ttk.Frame(self.root, padding=(8, 2))
        strip.pack(side=tk.BOTTOM, fill=tk.X)
    ttk.Checkbutton(strip, text="Clean preview/export", variable=self.clean_audition_var).pack(side=tk.LEFT, padx=(0, 8))
    ttk.Checkbutton(strip, text="Fixed render Hz", variable=self.fixed_render_rate_var).pack(side=tk.LEFT, padx=(0, 4))
    ttk.Combobox(strip, textvariable=self.render_rate_var, state="readonly", width=8, values=CLEAN_RENDER_RATES).pack(side=tk.LEFT, padx=(0, 12))
    ttk.Checkbutton(strip, text="Limiter", variable=self.limiter_var).pack(side=tk.LEFT, padx=(0, 8))
    ttk.Checkbutton(strip, text="De-crackle", variable=self.decrackle_filter_var).pack(side=tk.LEFT, padx=(0, 4))
    ttk.Combobox(strip, textvariable=self.decrackle_strength_var, state="readonly", width=8, values=("Light", "Medium", "Strong")).pack(side=tk.LEFT, padx=(0, 12))
    ttk.Button(strip, text="Audio Quality Report", command=self.save_audio_quality_report_csv).pack(side=tk.LEFT, padx=(0, 6))


def _clean_render_options(self: MLTExplorerApp) -> Dict[str, object]:
    return {
        "clean": bool(self.clean_audition_var.get()),
        "fixed_output_rate": bool(self.fixed_render_rate_var.get()),
        "output_rate": int(self.render_rate_var.get() or DEFAULT_CLEAN_RENDER_RATE),
        "dc_filter": bool(self.dc_filter_var.get()),
        "decrackle": bool(self.decrackle_filter_var.get()),
        "decrackle_strength": str(self.decrackle_strength_var.get() or "Light"),
        "limiter": bool(self.limiter_var.get()),
        "edge_fade": bool(self.edge_fade_var.get()),
    }


def _preview_selected_clean(self: MLTExplorerApp) -> None:
    if not self.bank:
        return
    idx = self.selected_index()
    if idx is None:
        return
    try:
        self.stop_preview(silent=True)
        s = self.bank.samples[idx]
        pcm, rate, diag = self.bank.render_sample_for_wav(
            idx,
            pitch_correct=self.pitch_correct_var.get(),
            trigger_note=int(self.trigger_note_var.get()),
            **_clean_render_options(self),
        )
        tmp = Path(tempfile.gettempdir()) / f"mlt_preview_{os.getpid()}_{idx:03d}.wav"
        write_wav(tmp, pcm, rate)
        self.current_temp_wav = tmp
        self._play_wav(tmp)
        self.status_var.set(f"Previewing sample {idx}: {s.alias} | {rate} Hz | peak {diag.get('post_peak', diag.get('peak', 0))}")
    except Exception as exc:
        messagebox.showerror("Preview failed", str(exc))


def _preview_loop_selected_clean(self: MLTExplorerApp) -> None:
    if not self.bank:
        return
    idx = self.selected_index()
    if idx is None:
        return
    try:
        s = self.bank.samples[idx]
        if not self.bank.loop_points_samples(idx):
            messagebox.showinfo("Loop preview", "This sample has no loop flag / loop points.")
            return
        self.stop_preview(silent=True)
        seconds = int(self.loop_preview_seconds_var.get() or 20)
        pcm, rate, diag = self.bank.render_loop_preview_for_wav(
            idx,
            preview_seconds=seconds,
            pitch_correct=self.pitch_correct_var.get(),
            trigger_note=int(self.trigger_note_var.get()),
            loop_declick=self.loop_declick_var.get(),
            crossfade_ms=float(self.loop_crossfade_ms_var.get() or 0.0),
            zero_cross=self.loop_zero_cross_var.get(),
            trim_silence=self.loop_trim_silence_var.get(),
            **_clean_render_options(self),
        )
        tmp = Path(tempfile.gettempdir()) / f"mlt_preview_{os.getpid()}_{idx:03d}_loop_clean_{seconds}s.wav"
        write_wav(tmp, pcm, rate)
        self.current_temp_wav = tmp
        self._play_wav(tmp)
        self.status_var.set(f"Loop preview sample {idx}: {s.alias} | {rate} Hz | clean={bool(self.clean_audition_var.get())} | {seconds}s")
    except Exception as exc:
        messagebox.showerror("Loop preview failed", str(exc))


def _export_selected_clean(self: MLTExplorerApp) -> None:
    if not self.bank:
        return
    idx = self.selected_index()
    if idx is None:
        return
    s = self.bank.samples[idx]
    # Include the final WAV rate in the filename.
    _pcm, rate, _diag = self.bank.render_sample_for_wav(
        idx,
        pitch_correct=self.pitch_correct_var.get(),
        trigger_note=int(self.trigger_note_var.get()),
        **_clean_render_options(self),
    )
    default = f"{idx:03d}_{self.bank.safe_alias(idx)}_{rate}Hz_clean.wav"
    path = filedialog.asksaveasfilename(title="Export selected WAV", initialfile=default, defaultextension=".wav", filetypes=[("WAV", "*.wav")])
    if not path:
        return
    try:
        self.bank.export_sample_rendered(
            idx,
            Path(path),
            pitch_correct=self.pitch_correct_var.get(),
            trigger_note=int(self.trigger_note_var.get()),
            **_clean_render_options(self),
        )
        self.status_var.set(f"Exported {Path(path).name}")
    except Exception as exc:
        messagebox.showerror("Export failed", str(exc))


def _export_loop_preview_selected_clean(self: MLTExplorerApp) -> None:
    if not self.bank:
        return
    idx = self.selected_index()
    if idx is None:
        return
    s = self.bank.samples[idx]
    if not self.bank.loop_points_samples(idx):
        messagebox.showinfo("Export loop preview", "This sample has no loop flag / loop points.")
        return
    seconds = int(self.loop_preview_seconds_var.get() or 20)
    _pcm, rate, _diag = self.bank.render_loop_preview_for_wav(
        idx,
        preview_seconds=seconds,
        pitch_correct=self.pitch_correct_var.get(),
        trigger_note=int(self.trigger_note_var.get()),
        loop_declick=self.loop_declick_var.get(),
        crossfade_ms=float(self.loop_crossfade_ms_var.get() or 0.0),
        zero_cross=self.loop_zero_cross_var.get(),
        trim_silence=self.loop_trim_silence_var.get(),
        **_clean_render_options(self),
    )
    default = f"{idx:03d}_{self.bank.safe_alias(idx)}_{rate}Hz_loopPreview_{seconds}s_clean.wav"
    path = filedialog.asksaveasfilename(title="Export loop preview WAV", initialfile=default, defaultextension=".wav", filetypes=[("WAV", "*.wav")])
    if not path:
        return
    try:
        self.bank.export_loop_preview_rendered(
            idx,
            Path(path),
            preview_seconds=seconds,
            pitch_correct=self.pitch_correct_var.get(),
            trigger_note=int(self.trigger_note_var.get()),
            loop_declick=self.loop_declick_var.get(),
            crossfade_ms=float(self.loop_crossfade_ms_var.get() or 0.0),
            zero_cross=self.loop_zero_cross_var.get(),
            trim_silence=self.loop_trim_silence_var.get(),
            **_clean_render_options(self),
        )
        self.status_var.set(f"Exported loop preview {Path(path).name}")
    except Exception as exc:
        messagebox.showerror("Export loop preview failed", str(exc))


def _export_all_rendered_gui(self: MLTExplorerApp) -> None:
    if not self.bank:
        return
    out = filedialog.askdirectory(title="Export all WAV files")
    if not out:
        return
    bank = self.bank
    pitch_correct = bool(self.pitch_correct_var.get())
    trigger_note = int(self.trigger_note_var.get())
    clean_kwargs = _clean_render_options(self)
    self._run_background("Export all clean", lambda: bank.export_all_rendered(
        Path(out),
        pitch_correct=pitch_correct,
        trigger_note=trigger_note,
        **clean_kwargs,
    ))


def _export_all_clean_gui(self: MLTExplorerApp) -> None:
    _export_all_rendered_gui(self)


def _save_audio_quality_report_csv(self: MLTExplorerApp) -> None:
    if not self.bank:
        return
    default = self.bank.path.with_name(self.bank.path.stem + "_audio_quality.csv").name
    path = filedialog.asksaveasfilename(title="Save audio quality report CSV", initialfile=default, defaultextension=".csv", filetypes=[("CSV", "*.csv")])
    if not path:
        return
    try:
        self.bank.write_audio_quality_report_csv(
            Path(path),
            pitch_correct=self.pitch_correct_var.get(),
            trigger_note=int(self.trigger_note_var.get()),
            **_clean_render_options(self),
        )
        self.status_var.set(f"Saved audio quality report to {Path(path).name}")
    except Exception as exc:
        messagebox.showerror("Audio quality report failed", str(exc))


MLTExplorerApp.__init__ = _app_init  # type: ignore[assignment]
MLTExplorerApp._build_ui = _build_ui_with_audio_tools  # type: ignore[assignment]
MLTExplorerApp.preview_selected = _preview_selected_clean  # type: ignore[assignment]
MLTExplorerApp.preview_loop_selected = _preview_loop_selected_clean  # type: ignore[assignment]
MLTExplorerApp.export_selected = _export_selected_clean  # type: ignore[assignment]
MLTExplorerApp.export_loop_preview_selected = _export_loop_preview_selected_clean  # type: ignore[assignment]
MLTExplorerApp.export_all = _export_all_rendered_gui  # type: ignore[assignment]
MLTExplorerApp.export_all_clean = _export_all_clean_gui  # type: ignore[attr-defined]
MLTExplorerApp.save_audio_quality_report_csv = _save_audio_quality_report_csv  # type: ignore[attr-defined]
