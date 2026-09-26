from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import wave
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .core import *
from .dsp import *
from .audio import *

class GCAXEditMixin:
    def replace_from_wav(
        self,
        index: int,
        wav_path: Path,
        preserve_loop_ratio: bool = True,
        preserve_bank_rate: bool = True,
        auto_resample_to_audition: bool = True,
        pitch_correct: bool = False,
        trigger_note: int = DEFAULT_TRIGGER_NOTE,
    ) -> None:
        s = self.samples[index]
        source_pcm, source_sr = read_wav_as_mono_pcm16(wav_path)

        # Keep the bank rate by default and resample the imported WAV to match it.
        bank_sr = s.sample_rate if preserve_bank_rate else int(round(source_sr * MLT_RATE_WORD_DIVISOR))
        target_content_sr_exact = self.audition_sample_rate_exact(index, pitch_correct=pitch_correct, trigger_note=trigger_note) if preserve_bank_rate else float(source_sr)
        target_content_sr_header = self.audition_sample_rate(index, pitch_correct=pitch_correct, trigger_note=trigger_note) if preserve_bank_rate else int(source_sr)
        pcm = source_pcm
        if auto_resample_to_audition and abs(float(source_sr) - float(target_content_sr_exact)) > 1e-9:
            pcm = resample_pcm16_for_replacement(source_pcm, float(source_sr), float(target_content_sr_exact))
        content_sr = int(round(target_content_sr_header if auto_resample_to_audition else source_sr))
        new_count = max(1, len(pcm) // 2)

        if s.loop_flag:
            old_points = self.loop_points_samples(index)
            old_start, old_end_excl = old_points if old_points else (0, s.sample_count)
            if preserve_loop_ratio and s.sample_count > 1:
                # Scale both loop edges instead of forcing the loop to the file end.
                start_ratio = old_start / s.sample_count
                tail_ratio = (s.sample_count - old_end_excl) / s.sample_count
                loop_start_sample = max(0, min(new_count - 1, round(start_ratio * new_count)))
                tail_samples = round(tail_ratio * new_count)
                if old_end_excl < s.sample_count:
                    tail_samples = max(1, tail_samples)
                loop_end_sample_exclusive = max(
                    loop_start_sample + 1,
                    min(new_count, new_count - tail_samples),
                )
            else:
                loop_start_sample = 0
                loop_end_sample_exclusive = new_count
        else:
            loop_start_sample = 0
            loop_end_sample_exclusive = new_count

        if s.type_byte == 0x0A:
            # Preserve linear/raw banks as big-endian PCM16 instead of silently
            # converting the entry to DSP-ADPCM.
            values = pcm16_bytes_to_list(pcm)
            payload = b"".join(struct.pack(">h", clamp16(v)) for v in values)
            entry = bytearray(self.data[s.entry_abs:s.entry_abs + 0x50])
            entry[0x00:0x08] = b"\x00" * 8
            entry[0x08:0x0C] = p32be(bank_sr)
            entry[0x0C:0x0E] = p16be(1 if s.loop_flag else 0)
            entry[0x0E:0x10] = p16be(s.fmt)
            entry[0x10:0x14] = p32be(loop_start_sample if s.loop_flag else 0)
            entry[0x14:0x18] = p32be(max(0, loop_end_sample_exclusive - 1))
            entry[0x18:0x1C] = p32be(0)
            entry[0x4A] = 0x0A

            s.replacement = Replacement(
                wav_path=Path(wav_path),
                pcm_le_i16=pcm,
                sample_rate=bank_sr,
                encoded_payload=payload,
                new_entry=bytes(entry),
                loop_start_sample=loop_start_sample,
                source_wav_rate=source_sr,
                content_sample_rate=content_sr,
                nibble_count=0,
                loop_end_sample_exclusive=loop_end_sample_exclusive,
                encode_peak_error=0,
                encode_rms_error=0.0,
            )
            return

        payload, initial_ps, loop_ps, loop_hist1, loop_hist2, encoded_count = encode_dsp_adpcm(
            pcm, s.coefficients, loop_start_sample=loop_start_sample
        )
        frame_count = math.ceil(encoded_count / 14)
        nibble_count = encoded_count + frame_count * 2
        loop_start = sample_to_nibble_address(loop_start_sample if s.loop_flag else 0)
        # DSP loop end is stored as an inclusive nibble address.
        loop_end = sample_to_nibble_address(loop_end_sample_exclusive - 1)

        entry = bytearray(self.data[s.entry_abs:s.entry_abs + 0x50])
        entry[0x00:0x04] = p32be(encoded_count)
        entry[0x04:0x08] = p32be(nibble_count)
        entry[0x08:0x0C] = p32be(bank_sr)
        entry[0x0C:0x0E] = p16be(1 if s.loop_flag else 0)
        entry[0x0E:0x10] = p16be(0)  # DSP ADPCM
        entry[0x10:0x14] = p32be(loop_start)
        entry[0x14:0x18] = p32be(loop_end)
        entry[0x18:0x1C] = p32be(2)
        # Keep the original coefficient table. The encoder used it.
        entry[0x3C:0x3E] = p16be(0)
        entry[0x3E:0x40] = p16be(initial_ps)
        entry[0x40:0x42] = ps16be(0)
        entry[0x42:0x44] = ps16be(0)
        entry[0x44:0x46] = p16be(loop_ps if s.loop_flag else initial_ps)
        entry[0x46:0x48] = ps16be(loop_hist1 if s.loop_flag else 0)
        entry[0x48:0x4A] = ps16be(loop_hist2 if s.loop_flag else 0)
        entry[0x4A] = 0
        # 0x4C data offset is filled in during save/repack.

        # Decode once now so bad encoder state is caught before saving.
        check_info = SampleInfo(
            index=s.index,
            entry_rel=s.entry_rel,
            entry_abs=s.entry_abs,
            sample_count=encoded_count,
            nibble_count=nibble_count,
            sample_rate=bank_sr,
            loop_flag=s.loop_flag,
            fmt=0,
            loop_start=loop_start,
            loop_end=loop_end,
            current_address=2,
            coefficients=s.coefficients[:],
            gain=0,
            initial_ps=initial_ps,
            initial_hist1=0,
            initial_hist2=0,
            loop_ps=loop_ps,
            loop_hist1=loop_hist1,
            loop_hist2=loop_hist2,
            type_byte=0,
            data_offset=0,
            byte_count=len(payload),
        )
        decoded_check = decode_dsp_adpcm(payload, check_info)
        encoded_values = pcm16_bytes_to_list(pcm)
        decoded_values = pcm16_bytes_to_list(decoded_check)
        errors = [a - b for a, b in zip(encoded_values, decoded_values)]
        encode_peak_error = max((abs(v) for v in errors), default=0)
        encode_rms_error = math.sqrt(sum(v * v for v in errors) / len(errors)) if errors else 0.0

        s.replacement = Replacement(
            wav_path=Path(wav_path),
            pcm_le_i16=pcm,
            sample_rate=bank_sr,
            encoded_payload=payload,
            new_entry=bytes(entry),
            loop_start_sample=loop_start_sample,
            source_wav_rate=source_sr,
            content_sample_rate=content_sr,
            nibble_count=nibble_count,
            loop_end_sample_exclusive=loop_end_sample_exclusive,
            encode_peak_error=encode_peak_error,
            encode_rms_error=encode_rms_error,
        )

    def clear_replacement(self, index: int) -> None:
        self.samples[index].replacement = None

    def build_repacked(self) -> bytes:
        # A no-edit save must be a true byte-for-byte copy. This also preserves
        # title-specific padding/alignment that does not need reconstruction.
        if not any(s.replacement for s in self.samples):
            return bytes(self.data)

        mpbp_body = bytearray(self.data[self.mpbp_body:self.mpbp_body + self.mpbp_size])
        mpbw_body_new = bytearray()

        for s in self.samples:
            # Align each sample start to 0x20, matching the observed MPBW layout.
            pad_len = align32(len(mpbw_body_new)) - len(mpbw_body_new)
            if pad_len:
                mpbw_body_new += bytes([self.mpbw_padding_byte]) * pad_len
            new_offset = len(mpbw_body_new)

            if s.replacement:
                payload = bytearray(s.replacement.encoded_payload)
                payload += bytes([self.mpbw_padding_byte]) * (align32(len(payload)) - len(payload))
                entry = bytearray(s.replacement.new_entry)
            else:
                payload = bytearray(self.sample_payload(s))
                entry = bytearray(self.data[s.entry_abs:s.entry_abs + 0x50])

            entry[0x4C:0x50] = p32be(new_offset)
            mpbp_body[s.entry_rel:s.entry_rel + 0x50] = entry
            mpbw_body_new += payload

        # MPBW chunk.
        mpbw_chunk = bytearray()
        mpbw_chunk += b"gcaxMPBW"
        mpbw_chunk += self.data[self.mpbw_pos + 8:self.mpbw_pos + 12]
        mpbw_chunk += p32be(len(mpbw_body_new))
        mpbw_chunk += mpbw_body_new

        # MPBP keeps the same body size; only sample entries are changed.
        mpbp_chunk = bytearray()
        mpbp_chunk += b"gcaxMPBP"
        mpbp_chunk += self.data[self.mpbp_pos + 8:self.mpbp_pos + 12]
        mpbp_chunk += p32be(len(mpbp_body))
        mpbp_chunk += mpbp_body

        def padding_bytes(original: bytes, needed: int, fallback: int = 0x55) -> bytes:
            if needed <= 0:
                return b""
            if len(original) == needed:
                return original
            if original:
                return (original * math.ceil(needed / len(original)))[:needed]
            return bytes([fallback]) * needed

        # Keep every child chunk in its original order, including unknown extensions.
        mpb_body_builder = bytearray()
        for child_index, child in enumerate(self.mpb_child_chunks):
            if child.header == self.mpbw_pos:
                child_bytes = bytes(mpbw_chunk)
            elif child.header == self.mpbp_pos:
                child_bytes = bytes(mpbp_chunk)
            else:
                child_bytes = self.data[child.header:child.body_end]
            mpb_body_builder += child_bytes

            originally_padded = child.next_header > child.body_end
            needs_next_alignment = child_index < len(self.mpb_child_chunks) - 1
            if needs_next_alignment or originally_padded:
                needed = align16(len(mpb_body_builder)) - len(mpb_body_builder)
                original_padding = self.data[child.body_end:child.next_header]
                mpb_body_builder += padding_bytes(original_padding, needed)

        mpb_body_new = bytes(mpb_body_builder)
        mpb_chunk = bytearray()
        mpb_chunk += b"gcaxMPB "
        mpb_chunk += self.data[self.mpb_pos + 8:self.mpb_pos + 12]
        mpb_chunk += p32be(len(mpb_body_new))
        mpb_chunk += mpb_body_new

        # Move later MLTM bank pointers when the rebuilt MPB changes size.
        old_after_mpb = self.mpb_container_span_end
        raw_new_after_mpb = self.mpb_pos + len(mpb_chunk)
        new_after_mpb = align16(raw_new_after_mpb) if (self.after_mpb or self.mpb_container_padding) else raw_new_after_mpb
        later_entry_delta = new_after_mpb - old_after_mpb
        prefix = bytearray(self.data[:self.mpb_pos])
        if later_entry_delta:
            for directory_entry in self.mlt_directory_entries:
                if directory_entry.is_dummy or directory_entry.pointer_abs < old_after_mpb:
                    continue
                record_abs = self.mltm_body + directory_entry.index * MLTM_RECORD_SIZE
                pointer_field_abs = record_abs + 8
                if pointer_field_abs + 4 > len(prefix):
                    raise ValueError(
                        f"Cannot update MLTM pointer for later entry {directory_entry.index}"
                    )
                new_pointer_rel = directory_entry.pointer_rel + later_entry_delta
                if not 0 <= new_pointer_rel <= 0xFFFFFFFF:
                    raise ValueError(
                        f"MLTM pointer overflow for later entry {directory_entry.index}: {new_pointer_rel}"
                    )
                prefix[pointer_field_abs:pointer_field_abs + 4] = p32be(new_pointer_rel)

        result = bytearray()
        result += prefix
        result += mpb_chunk
        top_padding_needed = new_after_mpb - len(result)
        result += padding_bytes(self.mpb_container_padding, top_padding_needed)
        result += self.after_mpb
        result[12:16] = p32be(len(result))
        return bytes(result)

    def save_as(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.build_repacked())

    def no_embedded_names_report(self) -> str:
        return (
            "Nem találtam beágyazott, emberi olvasásra szánt sample-name táblát. "
            "A nem-audio tartományokban csak chunk magic-ek és egy ABC teszt/blokk látszik, "
            "ezért a nevek generált aliasok."
        )


# Desktop interface

