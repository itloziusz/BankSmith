from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict

from .core import *
from .audio import *
from .render_audio import *

class GCAXRenderMixin:
    def render_sample_for_wav(
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


    def render_loop_preview_for_wav(
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


    def export_sample_rendered(self: MLTBank, index: int, path: Path, **kwargs) -> None:
        pcm, rate, _diag = self.render_sample_for_wav(index, **kwargs)
        write_wav(path, pcm, rate)


    def export_loop_preview_rendered(self: MLTBank, index: int, path: Path, preview_seconds: int = 20, **kwargs) -> None:
        pcm, rate, _diag = self.render_loop_preview_for_wav(index, preview_seconds=preview_seconds, **kwargs)
        write_wav(path, pcm, rate)


    def export_all_rendered(self: MLTBank, out_dir: Path, **kwargs) -> None:
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


    def write_audio_quality_report_csv(self: MLTBank, path: Path, **kwargs) -> None:
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
