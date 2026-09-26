from __future__ import annotations

import csv
import math
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .audio import (
    build_gapless_loop_preview_pcm,
    read_wav_as_mono_pcm16,
    resample_pcm16_for_replacement,
    write_wav,
)
from .render_audio import clean_pcm16_for_audition
from .dreamcast_codec import decode_tone, encode_tone
from .dreamcast_images import (
    DreamcastReplacement, ToneRecord, DreamcastToneImage,
    DreamcastMPBImage, DreamcastOSBImage,
    u32le, p32le, align,
)


@dataclass
class DreamcastSampleInfo:
    index: int
    alias: str
    usage: List[str]
    root_keys: List[int]
    loop_flag: int
    loop_start: int
    loop_end: int
    sample_count: int
    sample_rate: int
    data_offset: int
    format: str
    original_extent: int
    source_image: DreamcastToneImage = field(repr=False)
    tone_index: int = field(repr=False)
    replacement: Optional[DreamcastReplacement] = None
    fmt: int = 0
    type_byte: int = 0

    @property
    def current_sample_count(self) -> int:
        return self.replacement.sample_count if self.replacement else self.sample_count

    @property
    def current_sample_rate(self) -> int:
        return self.replacement.sample_rate if self.replacement else self.sample_rate

    @property
    def current_base_sample_rate_exact(self) -> float:
        return float(self.current_sample_rate)

    @property
    def current_duration_seconds(self) -> float:
        return self.current_sample_count / self.current_sample_rate if self.current_sample_rate else 0.0

    @property
    def replacement_label(self) -> str:
        return self.replacement.wav_path.name if self.replacement else ''


class DreamcastAudioBankBase:
    family = 'Dreamcast'

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        self.samples: List[DreamcastSampleInfo] = []
        self._images: List[Tuple[str, DreamcastToneImage]] = []
        self._parse_container()
        self._rebuild_sample_index()

    def _parse_container(self) -> None:
        raise NotImplementedError

    def _build_container(self) -> bytes:
        raise NotImplementedError

    def _rebuild_sample_index(self) -> None:
        samples: List[DreamcastSampleInfo] = []
        stem = self.path.stem
        for source_label, image in self._images:
            for ti, tone in enumerate(image.tones):
                refs = tone.refs
                primary = refs[0]
                roots: List[int] = []
                for ref in refs:
                    if ref.base_note not in roots:
                        roots.append(ref.base_note)
                loop_refs = [ref for ref in refs if ref.loop]
                loop_ref = loop_refs[0] if loop_refs else primary
                idx = len(samples)
                fmt_id = {'adpcm': 0, 'pcm8': 1, 'pcm16': 2}[tone.format]
                alias = f'{stem}_{source_label}_tone_{ti:03d}_{tone.format}'
                base_rate = max(1, int(round(44100.0 * (2 ** ((60 - primary.base_note) / 12.0)))))
                samples.append(DreamcastSampleInfo(
                    index=idx,
                    alias=alias,
                    usage=[f'{source_label}.{ref.usage}' for ref in refs],
                    root_keys=roots,
                    loop_flag=1 if loop_refs else 0,
                    loop_start=loop_ref.loop_start,
                    loop_end=loop_ref.loop_end,
                    sample_count=tone.sample_count,
                    sample_rate=base_rate,
                    data_offset=tone.ptr,
                    format=tone.format,
                    original_extent=len(tone.raw_payload),
                    source_image=image,
                    tone_index=ti,
                    replacement=tone.replacement,
                    fmt=fmt_id,
                    type_byte=0xD0 + fmt_id,
                ))
        self.samples = samples

    def _tone(self, index: int) -> ToneRecord:
        sample = self.samples[index]
        return sample.source_image.tones[sample.tone_index]

    def sample_root_key(self, index: int) -> Optional[int]:
        roots = self.samples[index].root_keys
        return roots[0] if roots else None

    def audition_sample_rate_exact(self, index: int, pitch_correct: bool=False, trigger_note: int=60) -> float:
        sample = self.samples[index]
        base = float(sample.current_sample_rate)
        if pitch_correct and trigger_note != 60:
            base *= 2 ** ((int(trigger_note) - 60) / 12.0)
        return base

    def audition_sample_rate(self, index: int, pitch_correct: bool=False, trigger_note: int=60) -> int:
        return int(round(self.audition_sample_rate_exact(index, pitch_correct, trigger_note)))

    def rate_correction(self, index: int, trigger_note: int=60) -> Tuple[int, int, str]:
        sample = self.samples[index]
        root = self.sample_root_key(index)
        semis = int(trigger_note) - 60
        rate = int(round(float(sample.current_sample_rate) * (2 ** (semis / 12.0))))
        note = f'Dreamcast AICA base-note rate; root={root if root is not None else "?"}, trigger={trigger_note}'
        return rate, semis, note

    def rate_correction_exact(self, index: int, trigger_note: int=60) -> Tuple[float, int, str]:
        rate, semis, note = self.rate_correction(index, trigger_note)
        return float(rate), semis, note

    def decode_sample(self, index: int) -> bytes:
        sample = self.samples[index]
        tone = self._tone(index)
        return decode_tone(tone.current_payload, tone.format, sample.current_sample_count)

    def sample_payload(self, sample: DreamcastSampleInfo) -> bytes:
        return self._tone(sample.index).current_payload

    def loop_points_samples(self, index: int) -> Optional[Tuple[int, int]]:
        sample = self.samples[index]
        if not sample.loop_flag or sample.current_sample_count <= 0:
            return None
        start = max(0, min(sample.current_sample_count - 1, int(sample.loop_start)))
        end = int(sample.loop_end)
        if not (start < end <= sample.current_sample_count):
            end = sample.current_sample_count
        return start, end

    def loop_point_report(self, index: int, trigger_note: int=60):
        points = self.loop_points_samples(index)
        if not points:
            return None
        sample = self.samples[index]
        start, end = points
        return {
            'loop_start_addr_hex': f'0x{start:X}',
            'loop_end_addr_hex': f'0x{max(start, end - 1):X}',
            'loop_start_sample': start,
            'loop_end_sample_inclusive': end - 1,
            'loop_end_sample_exclusive': end,
            'loop_length_samples': end - start,
            'loop_length_seconds': (end - start) / max(1, sample.current_sample_rate),
        }

    def render_sample_for_wav(
        self,
        index: int,
        *,
        pitch_correct: bool=False,
        trigger_note: int=60,
        clean: bool=True,
        fixed_output_rate: bool=True,
        output_rate: int=44100,
        dc_filter: bool=True,
        decrackle: bool=True,
        decrackle_strength: str='Light',
        limiter: bool=True,
        edge_fade: bool=True,
    ):
        pcm = self.decode_sample(index)
        rate = self.audition_sample_rate(index, pitch_correct=pitch_correct, trigger_note=trigger_note)
        if not clean:
            return pcm, rate, {'clean': 0, 'output_rate': rate}
        pcm2, out_rate, diag = clean_pcm16_for_audition(
            pcm,
            float(rate),
            output_rate=output_rate,
            fixed_output_rate=fixed_output_rate,
            dc_filter=dc_filter,
            decrackle=decrackle,
            decrackle_strength=decrackle_strength,
            limiter=limiter,
            edge_fade=edge_fade,
        )
        diag['clean'] = 1
        return pcm2, out_rate, diag

    def render_loop_preview_for_wav(
        self,
        index: int,
        preview_seconds: int=20,
        *,
        pitch_correct: bool=False,
        trigger_note: int=60,
        loop_declick: bool=True,
        crossfade_ms: float=3.0,
        zero_cross: bool=True,
        trim_silence: bool=True,
        clean: bool=True,
        fixed_output_rate: bool=True,
        output_rate: int=44100,
        dc_filter: bool=True,
        decrackle: bool=True,
        decrackle_strength: str='Light',
        limiter: bool=True,
        edge_fade: bool=True,
    ):
        points = self.loop_points_samples(index)
        if not points:
            return self.render_sample_for_wav(
                index,
                pitch_correct=pitch_correct,
                trigger_note=trigger_note,
                clean=clean,
                fixed_output_rate=fixed_output_rate,
                output_rate=output_rate,
                dc_filter=dc_filter,
                decrackle=decrackle,
                decrackle_strength=decrackle_strength,
                limiter=limiter,
                edge_fade=edge_fade,
            )
        pcm = self.decode_sample(index)
        rate = self.audition_sample_rate(index, pitch_correct=pitch_correct, trigger_note=trigger_note)
        preview, diag = build_gapless_loop_preview_pcm(
            pcm,
            points[0],
            points[1],
            rate,
            preview_seconds,
            declick=loop_declick,
            crossfade_ms=crossfade_ms,
            zero_cross=zero_cross,
            trim_silence=trim_silence,
        )
        if not clean:
            diag['clean'] = 0
            diag['output_rate'] = rate
            return preview, rate, diag
        pcm2, out_rate, clean_diag = clean_pcm16_for_audition(
            preview,
            float(rate),
            output_rate=output_rate,
            fixed_output_rate=fixed_output_rate,
            dc_filter=dc_filter,
            decrackle=decrackle,
            decrackle_strength=decrackle_strength,
            limiter=limiter,
            edge_fade=edge_fade,
        )
        diag.update(clean_diag)
        diag['clean'] = 1
        return pcm2, out_rate, diag

    def build_loop_preview_pcm(
        self,
        index: int,
        preview_seconds: int=20,
        *,
        pitch_correct: bool=False,
        trigger_note: int=60,
        declick: bool=True,
        crossfade_ms: float=3.0,
        zero_cross: bool=True,
        trim_silence: bool=True,
    ) -> bytes:
        points = self.loop_points_samples(index)
        if not points:
            return self.decode_sample(index)
        pcm = self.decode_sample(index)
        rate = self.audition_sample_rate(index, pitch_correct=pitch_correct, trigger_note=trigger_note)
        preview, _ = build_gapless_loop_preview_pcm(
            pcm,
            points[0],
            points[1],
            rate,
            preview_seconds,
            declick=declick,
            crossfade_ms=crossfade_ms,
            zero_cross=zero_cross,
            trim_silence=trim_silence,
        )
        return preview

    def loop_preview_diagnostics(
        self,
        index: int,
        preview_seconds: int=20,
        *,
        pitch_correct: bool=False,
        trigger_note: int=60,
        declick: bool=True,
        crossfade_ms: float=3.0,
        zero_cross: bool=True,
        trim_silence: bool=True,
    ):
        points = self.loop_points_samples(index)
        if not points:
            return {
                'preview_start': 0,
                'preview_end_exclusive': self.samples[index].current_sample_count,
                'crossfade_samples': 0,
            }
        pcm = self.decode_sample(index)
        rate = self.audition_sample_rate(index, pitch_correct=pitch_correct, trigger_note=trigger_note)
        _preview, diag = build_gapless_loop_preview_pcm(
            pcm,
            points[0],
            points[1],
            rate,
            preview_seconds,
            declick=declick,
            crossfade_ms=crossfade_ms,
            zero_cross=zero_cross,
            trim_silence=trim_silence,
        )
        return diag

    def export_sample(self, index: int, path: Path, pitch_correct: bool=False, trigger_note: int=60) -> None:
        write_wav(
            path,
            self.decode_sample(index),
            self.audition_sample_rate(index, pitch_correct=pitch_correct, trigger_note=trigger_note),
        )

    def export_sample_rendered(self, index: int, path: Path, **kwargs) -> None:
        pcm, rate, _diag = self.render_sample_for_wav(index, **kwargs)
        write_wav(path, pcm, rate)

    def export_loop_preview(
        self,
        index: int,
        path: Path,
        preview_seconds: int=20,
        trigger_note: int=60,
        **kwargs,
    ) -> None:
        pcm, rate, _diag = self.render_loop_preview_for_wav(
            index,
            preview_seconds,
            trigger_note=trigger_note,
            **kwargs,
        )
        write_wav(path, pcm, rate)

    def export_loop_preview_rendered(self, index: int, path: Path, preview_seconds: int=20, **kwargs) -> None:
        self.export_loop_preview(index, path, preview_seconds, **kwargs)

    def export_all(self, out_dir: Path, pitch_correct: bool=False, trigger_note: int=60) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        for sample in self.samples:
            self.export_sample(
                sample.index,
                out_dir / f'{sample.index:03d}_{self.safe_alias(sample.index)}.wav',
                pitch_correct=pitch_correct,
                trigger_note=trigger_note,
            )

    def export_all_rendered(self, out_dir: Path, **kwargs) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        for sample in self.samples:
            pcm, rate, _diag = self.render_sample_for_wav(sample.index, **kwargs)
            write_wav(
                out_dir / f'{sample.index:03d}_{self.safe_alias(sample.index)}_{rate}Hz.wav',
                pcm,
                rate,
            )

    def replace_from_wav(
        self,
        index: int,
        wav_path: Path,
        preserve_loop_ratio: bool=True,
        preserve_bank_rate: bool=True,
        auto_resample_to_audition: bool=True,
        pitch_correct: bool=False,
        trigger_note: int=60,
    ) -> None:
        sample = self.samples[index]
        tone = self._tone(index)
        source_pcm, source_rate = read_wav_as_mono_pcm16(Path(wav_path))
        target_rate = sample.sample_rate if preserve_bank_rate else source_rate
        pcm = source_pcm
        if auto_resample_to_audition and source_rate != target_rate:
            pcm = resample_pcm16_for_replacement(pcm, source_rate, target_rate)
        count = max(1, len(pcm) // 2)
        if count >= 65535:
            raise ValueError('Dreamcast tone replacements are limited to 65534 samples')
        payload = encode_tone(pcm, tone.format)
        replacement = DreamcastReplacement(
            Path(wav_path),
            pcm,
            payload,
            count,
            int(target_rate),
            tone.format,
            source_wav_rate=int(source_rate),
            loop_start_sample=int(sample.loop_start if sample.loop_flag else 0),
            loop_end_sample_exclusive=int(sample.loop_end if sample.loop_flag else count),
        )
        tone.replacement = replacement
        sample.replacement = replacement
        if sample.loop_flag:
            old_count = max(1, sample.sample_count)
            if preserve_loop_ratio:
                sample.loop_start = max(
                    0,
                    min(count - 1, round((sample.loop_start / old_count) * count)),
                )
                sample.loop_end = (
                    max(
                        sample.loop_start + 1,
                        min(count, round((sample.loop_end / old_count) * count)),
                    )
                    if sample.loop_end else count
                )
            else:
                sample.loop_start = 0
                sample.loop_end = count
            replacement.loop_start_sample = sample.loop_start
            replacement.loop_end_sample_exclusive = sample.loop_end

    def clear_replacement(self, index: int) -> None:
        sample = self.samples[index]
        self._tone(index).replacement = None
        sample.replacement = None

    def build_repacked(self) -> bytes:
        return self._build_container()

    def save_as(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.build_repacked())

    def replacement_count(self) -> int:
        return sum(1 for sample in self.samples if sample.replacement)

    def safe_alias(self, index: int) -> str:
        return ''.join(c if c.isalnum() or c in '._-' else '_' for c in self.samples[index].alias)

    def export_raw_payload(self, index: int, path: Path, include_padding: bool=False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self._tone(index).current_payload)

    def export_all_raw_payloads(self, out_dir: Path, include_padding: bool=False) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        for sample in self.samples:
            self.export_raw_payload(
                sample.index,
                out_dir / f'{sample.index:03d}_{self.safe_alias(sample.index)}.{sample.format}',
            )

    def validate_repack_plan(self, trigger_note: int=60) -> List[Dict[str, object]]:
        rows: List[Dict[str, object]] = []

        def add(severity, area, index, message, detail=''):
            rows.append({
                'severity': severity,
                'area': area,
                'index': index,
                'message': message,
                'detail': detail,
            })

        for sample in self.samples:
            tone = self._tone(sample.index)
            if sample.current_sample_count <= 0:
                add('error', 'tone', sample.index, 'tone has no samples')
            if sample.current_sample_count >= 65535:
                add('error', 'tone', sample.index, 'tone exceeds Dreamcast 16-bit sample-length limit')
            expected = {
                'adpcm': math.ceil(sample.current_sample_count / 2),
                'pcm8': sample.current_sample_count,
                'pcm16': sample.current_sample_count * 2,
            }[sample.format]
            if sample.replacement and len(tone.current_payload) != expected:
                add(
                    'error',
                    'tone',
                    sample.index,
                    'replacement payload length mismatch',
                    f'{len(tone.current_payload)} != {expected}',
                )
            if sample.loop_flag and not self.loop_points_samples(sample.index):
                add('error', 'loop', sample.index, 'invalid loop points')

        try:
            built = self.build_repacked()
            if not built:
                add('error', 'repack', '-', 'empty repack result')
            else:
                add('ok', 'repack', '-', 'repack completed', f'{len(built)} bytes')
        except Exception as exc:
            add('error', 'repack', '-', 'repack failed', str(exc))

        errors = sum(1 for row in rows if row['severity'] == 'error')
        add(
            'ok' if not errors else 'error',
            'summary',
            '-',
            'validation summary',
            f'errors={errors}',
        )
        return rows

    def write_validation_report_csv(self, path: Path, trigger_note: int=60) -> None:
        rows = self.validate_repack_plan(trigger_note)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(
                f,
                fieldnames=['severity', 'area', 'index', 'message', 'detail'],
            )
            writer.writeheader()
            writer.writerows(rows)

    def write_alias_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['index', 'alias'])
            writer.writeheader()
            writer.writerows({'index': sample.index, 'alias': sample.alias} for sample in self.samples)

    def load_alias_csv(self, path: Path) -> int:
        by_index = {sample.index: sample for sample in self.samples}
        count = 0
        with Path(path).open('r', encoding='utf-8-sig', newline='') as f:
            for row in csv.DictReader(f):
                try:
                    index = int(row.get('index', ''))
                except Exception:
                    continue
                if index in by_index and row.get('alias'):
                    by_index[index].alias = row['alias']
                    count += 1
        return count

    def write_loop_report_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for sample in self.samples:
            report = self.loop_point_report(sample.index)
            if report:
                rows.append({'index': sample.index, 'alias': sample.alias, **report})
        fields = sorted({key for row in rows for key in row}) or ['index']
        with path.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def write_loop_preview_seam_report_csv(
        self,
        path: Path,
        preview_seconds: int=20,
        trigger_note: int=60,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for sample in self.samples:
            if not sample.loop_flag:
                continue
            _pcm, rate, diag = self.render_loop_preview_for_wav(
                sample.index,
                preview_seconds,
                clean=False,
                trigger_note=trigger_note,
            )
            rows.append({'index': sample.index, 'alias': sample.alias, 'rate': rate, **diag})
        fields = sorted({key for row in rows for key in row}) or ['index']
        with path.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def write_audio_quality_report_csv(self, path: Path, **kwargs) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for sample in self.samples:
            _pcm, rate, diag = self.render_sample_for_wav(sample.index, **kwargs)
            rows.append({
                'index': sample.index,
                'alias': sample.alias,
                'format': sample.format,
                'rate': rate,
                **diag,
            })
        fields = sorted({key for row in rows for key in row}) or ['index']
        with path.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def write_replacement_manifest_template(self, path: Path, trigger_note: int=60) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = [
            'index', 'alias', 'replacement_wav', 'expected_preview_hz', 'format',
            'root_key', 'loop_flag', 'loop_start_sample', 'loop_end_sample_exclusive',
            'usage', 'notes',
        ]
        with path.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for sample in self.samples:
                points = self.loop_points_samples(sample.index)
                writer.writerow({
                    'index': sample.index,
                    'alias': sample.alias,
                    'replacement_wav': '',
                    'expected_preview_hz': self.audition_sample_rate(
                        sample.index,
                        pitch_correct=True,
                        trigger_note=trigger_note,
                    ),
                    'format': sample.format,
                    'root_key': (
                        self.sample_root_key(sample.index)
                        if self.sample_root_key(sample.index) is not None
                        else ''
                    ),
                    'loop_flag': int(sample.loop_flag),
                    'loop_start_sample': points[0] if points else '',
                    'loop_end_sample_exclusive': points[1] if points else '',
                    'usage': ';'.join(sample.usage),
                    'notes': 'Filename may start with the numeric index or match the alias.',
                })

    def batch_replace_from_folder(
        self,
        folder: Path,
        *,
        preserve_bank_rate: bool=True,
        pitch_correct: bool=False,
        trigger_note: int=60,
    ):
        folder = Path(folder)
        by_index = {}
        for wav in folder.glob('*.wav'):
            stem = wav.stem.lower()
            for sample in self.samples:
                if stem.startswith(f'{sample.index:03d}_') or stem == sample.alias.lower():
                    by_index.setdefault(sample.index, wav)
                    break
        rows = []
        for index, wav in sorted(by_index.items()):
            try:
                self.replace_from_wav(
                    index,
                    wav,
                    preserve_loop_ratio=True,
                    preserve_bank_rate=preserve_bank_rate,
                    auto_resample_to_audition=True,
                    pitch_correct=pitch_correct,
                    trigger_note=trigger_note,
                )
                rows.append({
                    'index': index,
                    'status': 'ok',
                    'wav': str(wav),
                    'alias': self.samples[index].alias,
                })
            except Exception as exc:
                rows.append({
                    'index': index,
                    'status': 'error',
                    'wav': str(wav),
                    'alias': self.samples[index].alias,
                    'error': str(exc),
                })
        return rows

    def no_embedded_names_report(self) -> str:
        return 'Dreamcast bank: aliases are generated from container/program/layer/split references.'


class DreamcastStandaloneMPBBank(DreamcastAudioBankBase):
    family = 'Dreamcast SMPB'

    def _parse_container(self) -> None:
        self.image = DreamcastMPBImage(self.data, 'MPB')
        self._images = [('MPB', self.image)]

    def _build_container(self) -> bytes:
        return self.image.build()


class DreamcastSMLTBank(DreamcastAudioBankBase):
    family = 'Dreamcast SMLT'

    def _parse_container(self) -> None:
        data = self.data
        if len(data) < 32 or data[:4] != b'SMLT':
            raise ValueError('Not a Dreamcast SMLT file')
        self.version = u32le(data, 4)
        self.num_units = u32le(data, 8)
        if 32 + self.num_units * 32 > len(data):
            raise ValueError('SMLT unit table is truncated')
        self.units = []
        self._images = []
        for i in range(self.num_units):
            off = 32 + i * 32
            kind = data[off:off + 4]
            bank = struct.unpack_from('<b', data, off + 4)[0]
            aica_ptr = u32le(data, off + 8)
            aica_size = u32le(data, off + 12)
            file_ptr = u32le(data, off + 16)
            file_size = u32le(data, off + 20)
            payload = None
            image = None
            if file_ptr != 0xFFFFFFFF and file_size != 0xFFFFFFFF and file_size:
                if file_ptr + file_size > len(data):
                    raise ValueError(f'SMLT unit {i} data outside file')
                payload = data[file_ptr:file_ptr + file_size]
                if kind in (b'SMPB', b'SMDB'):
                    label = f'U{i:02d}B{bank}'
                    image = DreamcastMPBImage(payload, label)
                    self._images.append((label, image))
            self.units.append({
                'offset': off,
                'kind': kind,
                'bank': bank,
                'aica_ptr': aica_ptr,
                'aica_size': aica_size,
                'file_ptr': file_ptr,
                'file_size': file_size,
                'payload': payload,
                'image': image,
            })

    def _build_container(self) -> bytes:
        if not any(image.modified for _label, image in self._images):
            return self.data
        header_end = 32 + self.num_units * 32
        out = bytearray(self.data[:header_end])
        for i, unit in enumerate(self.units):
            off = 32 + i * 32
            payload = unit['payload']
            if unit['image'] is not None:
                payload = unit['image'].build()
            if payload is None:
                out[off + 16:off + 20] = p32le(0xFFFFFFFF)
                out[off + 20:off + 24] = p32le(0xFFFFFFFF)
                continue
            if len(payload) > unit['aica_size'] and unit['aica_size'] not in (0, 0xFFFFFFFF):
                raise ValueError(f'SMLT unit {i} rebuilt data exceeds reserved AICA size')
            pos = len(out)
            out[off + 16:off + 20] = p32le(pos)
            out[off + 20:off + 24] = p32le(len(payload))
            out += payload
        out += b'\x00' * (align(len(out), 32) - len(out))
        return bytes(out)


class DreamcastMDTBank(DreamcastAudioBankBase):
    family = 'Sonic Shuffle MDT'

    def _parse_container(self) -> None:
        data = self.data
        if len(data) < 8:
            raise ValueError('MDT is too short')
        first = u32le(data, 0)
        if first < 4 or first % 4 or first > len(data):
            raise ValueError('Invalid MDT offset table')
        self.count = first // 4
        self.offsets = [u32le(data, i * 4) for i in range(self.count)]
        self.blocks = []
        self._images = []
        for i, off in enumerate(self.offsets):
            end = self.offsets[i + 1] if i + 1 < self.count else len(data)
            if off + 4 > end:
                raise ValueError('Invalid MDT block')
            declared = u32le(data, off)
            payload = data[off + 4:end]
            if declared > len(payload):
                raise ValueError(f'MDT block {i} truncated')
            payload = payload[:declared]
            kind = payload[:4] if len(payload) >= 4 else b''
            image = None
            label = f'B{i:03d}'
            if kind in (b'SMPB', b'SMDB'):
                image = DreamcastMPBImage(payload, label)
            elif kind == b'SOSB':
                image = DreamcastOSBImage(payload, label)
            if image is not None:
                self._images.append((label, image))
            self.blocks.append({'kind': kind, 'payload': payload, 'image': image})

    def _build_container(self) -> bytes:
        if not any(image.modified for _label, image in self._images):
            return self.data
        out = bytearray(b'\x00' * (self.count * 4))
        for i, block in enumerate(self.blocks):
            out[i * 4:i * 4 + 4] = p32le(len(out))
            payload = block['image'].build() if block['image'] is not None else block['payload']
            out += p32le(len(payload))
            out += payload
        return bytes(out)
