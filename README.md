# BankSmith

**Multi-Format Soundbank Editor for GameCube gcax, Dreamcast AICA, and Sonic Shuffle audio banks.**

## Overview

**BankSmith** is a multi-format desktop soundbank editor for Sega/GameCube-era audio data. It can inspect, audition, extract, replace, validate, and repack both Nintendo/GameCube `gcax` banks and Sega Dreamcast/AICA sound-driver formats.

Current editable families include:

- GameCube `gcaxMLT` archives;
- standalone GameCube `gcaxMPB` banks;
- Dreamcast `SMLT` multi-unit archives;
- standalone Dreamcast `SMPB` / `SMDB` program banks;
- Sonic Shuffle `MDT` containers with `SMPB`, `SMDB`, and `SOSB` audio blocks.

The application is implemented in Python with a Tkinter interface. Its design prioritises structural preservation, reproducible output, explicit validation, byte-identical no-edit round trips where possible, and non-destructive file handling.

## Principal Capabilities

The application provides the following functions:

- opening and editing GameCube `gcaxMLT` and standalone `gcaxMPB` banks;
- opening and editing Dreamcast `SMLT`, `SMPB`, `SMDB`, and Sonic Shuffle `MDT` containers;
- parsing embedded Dreamcast `SOSB` one-shot audio blocks inside MDT files;
- enumerating sample/tone records, loop metadata, rate information, and program/layer/split references;
- decoding Nintendo/GameCube DSP-ADPCM;
- decoding Sega AICA 4-bit ADPCM, signed PCM8, and little-endian PCM16;
- exporting decoded audio as mono PCM WAV;
- real-time preview through the operating-system playback path;
- generating continuous loop previews without an intro-to-loop playback hand-off;
- importing PCM and IEEE floating-point WAV files;
- deterministic multichannel-to-mono conversion;
- rate-aware resampling before replacement encoding;
- re-encoding GameCube replacements as DSP-ADPCM;
- re-encoding Dreamcast replacements in their original AICA/PCM format;
- preserving raw GameCube PCM16 samples as raw PCM16 instead of silently converting them to DSP-ADPCM;
- rebuilding MPBW/MPBP, Dreamcast bank data, SMLT units, and MDT block-offset tables;
- saving edited files without overwriting the source by default;
- exporting alias tables, loop reports, validation reports, quality-control data, structural reports, and raw encoded payloads;
- CLI operation for inspection, validation, extraction, export, and repacking;
- storing editor state and replacement references in project files.

## Safety and File-Integrity Model

BankSmith does not automatically overwrite the source soundbank. Edited banks should be written to a distinct output path by using the **Save Repacked As** command. The original file extension is preserved for MLT, MPB, and MDT sources.

A conservative workflow is strongly recommended:

1. Retain an unmodified copy of the original MLT archive.
2. Perform all edits against a duplicate or working copy.
3. Run the built-in validation procedure before deployment.
4. Compare the repacked output with the source structure where appropriate.
5. Verify the resulting bank in the intended game or runtime environment.

Although the application preserves known structures and recalculates affected offsets, the MLT family contains fields whose interpretation may vary between titles. Runtime verification therefore remains necessary.

## Naming and Compatibility

The project is now named **BankSmith**. The new primary entry point is:

```bash
python BankSmith.py
```

The new public package alias is `banksmith`, so both `import banksmith` and `python -m banksmith` are supported. The historical `mlt_soundbank_explorer` package remains available for backward compatibility with existing scripts and imports. Legacy launcher filenames continue to work, but new documentation and examples use the BankSmith name.

## System Requirements

- Python 3.10 or later
- Tkinter

The standard Windows distribution of Python normally includes Tkinter. On Debian- or Ubuntu-based Linux systems, Tkinter may require a separate package:

```bash
sudo apt install python3-tk
```

The core application does not require third-party Python packages.

## Launching the Application

Start the graphical interface with either entry point:

```bash
python BankSmith.py
python -m banksmith
```

Open a specific supported soundbank at launch with:

```bash
python BankSmith.py bank.mlt
python BankSmith.py bank.mpb
python BankSmith.py sound.mdt
```

## Playback-Rate Handling

### GameCube gcax

The default gcax preset is:

```text
Base stored/2 rate
```

In this mode, the application interprets one half of the MPBP stored rate word as the base sample rate. No trigger-note-based pitch transformation is applied.

The available rate modes are:

- `Base stored/2 rate`
- `Game C4 / 60`
- `Game C3 / 48`
- `Custom`

The game-oriented presets calculate an effective playback rate from the stored rate, the inferred root key, and the selected trigger note. The general relation is:

```text
base_rate = stored_rate / 2
effective_rate = base_rate × 2^((trigger_note − root_key) / 12)
```

Because the default configuration is the stored base rate, opening or exporting a gcax bank does not silently impose a C4- or C3-based pitch correction.

### Dreamcast AICA

Dreamcast MPB/OSB data does not contain the same explicit stored-Hz field as gcax MPBP entries. Playback rate is derived from the tone's base-note metadata using the Sega convention observed in the supported banks. Base note 60 maps to 44100 Hz, with semitone steps applied exponentially around that reference.

The Dreamcast codec and rate path is intentionally separate from the GameCube DSP path.

## Recommended Editing Procedure

1. Open the original supported soundbank (`.mlt`, `.mpb`, or `.mdt`).
2. Inspect sample metadata, program usage, loop state, and rate information.
3. Audition or export the relevant samples.
4. Replace selected samples with suitable WAV sources where required.
5. Review the imported sample rate and replacement diagnostics.
6. Run validation.
7. Save the edited bank under a new filename by using **Save Repacked As**.
8. Test the resulting archive in the target environment.

## Supported WAV Import Formats

The internal WAV reader accepts the following common formats:

- 8-bit PCM;
- 16-bit PCM;
- 24-bit PCM;
- 32-bit PCM;
- 32-bit IEEE floating-point PCM;
- 64-bit IEEE floating-point PCM;
- `WAVE_FORMAT_EXTENSIBLE` variants of supported PCM or floating-point data;
- multichannel WAV files, which are deterministically mixed to mono.

Imported data are converted to signed 16-bit mono PCM before DSP-ADPCM encoding. Non-finite floating-point values are replaced with silence, and values outside the valid range are constrained before conversion.

When the target rate is lower than the source rate, the application uses band-limited resampling in order to reduce spectral folding and other aliasing artefacts. Upsampling uses a smooth interpolation path suitable for replacement preparation.

## DSP-ADPCM Encoding

Replacement samples are encoded with a dependency-free DSP-ADPCM encoder. The encoder uses the coefficient table associated with the original sample and evaluates predictor/scale combinations on a frame-by-frame basis.

For looped material, the replacement process also reconstructs the relevant loop predictor and history state. The resulting encoded payload, nibble count, sample count, loop addresses, and MPBP record are used for both preview and final repacking so that the auditioned replacement corresponds to the data intended for storage.

The encoder is designed for compatibility and preservation-oriented editing. For archival, production, or research use, the encoded result should still be compared with reference tools and verified in the target runtime.

## Dreamcast AICA Audio

Dreamcast audio is handled by a dedicated backend and is never passed through the Nintendo DSP-ADPCM decoder.

Supported tone formats are:

- AICA/Yamaha-style 4-bit ADPCM;
- signed PCM8;
- little-endian PCM16.

Replacement WAVs are converted to mono PCM16, resampled to the target playback rate when needed, and re-encoded in the tone's original format. Shared-tone pointers, split references, loop boundaries, file-size fields, checksums, SMLT unit offsets, and MDT block offsets are rebuilt during save.

SMPB/SMDB program banks use the documented Program → Layer → Split hierarchy. SOSB one-shot banks inside Sonic Shuffle MDT files are parsed and rebuilt through their own program/tone layout.

## Loop Preview and Boundary Processing

Looped previews are generated as a single PCM stream containing the introductory section followed by repeated loop material. This design avoids the audible pause that can occur when a player switches between separate introduction and loop files.

The following optional operations may be applied to preview or cleaned export paths:

- nearby zero-crossing selection;
- removal of unintended silence at the beginning of the loop region;
- short boundary crossfading;
- DC-offset removal;
- attenuation of isolated discontinuities;
- limiting;
- short edge fades.

These operations are primarily intended to improve audition quality. Preview-only boundary corrections do not implicitly rewrite the original loop points unless a replacement is explicitly prepared and saved.

## Scrollable Interface Layout

The detailed control panel on the right-hand side is vertically scrollable. This arrangement prevents lower controls from becoming clipped or visually compressed when the application is used with a reduced window height, elevated display scaling, or unusually extensive sample metadata.

- The panel may be navigated with its vertical scrollbar.
- Mouse-wheel input is supported while the pointer is positioned over the panel.
- The sample table, status area, and audio quick controls remain stationary while the detailed controls are scrolled.
- The **Sample Layer / Hex Details** window provides an independent vertical scrollbar for lengthy diagnostic material.

## User-Interface Stability

Operations that involve decoding, encoding, project restoration, validation, or extensive export may run outside the Tkinter event loop. The application separates background computation from graphical-interface access according to the following rules:

- Tkinter variables and widgets are accessed only by the main thread.
- Current settings are captured before a background operation begins.
- Worker threads return results through a thread-safe message queue.
- Worker threads do not invoke `root.after(...)` or perform direct widget updates.
- Only one major background task may run at a time.
- Rapid preset changes are coalesced before the interface is refreshed.
- Active preview playback is stopped when a rate preset is changed.
- External playback processes are terminated during application shutdown.
- Error paths restore the interface to an operational state and report the failure on the main thread.

This arrangement prevents rate or processing settings from being read in a partially updated state and reduces the risk of interface stalls during repeated configuration changes.

## Command-Line Interface

Display command-line help:

```bash
python BankSmith.py --help
```

Inspect any supported container:

```bash
python BankSmith.py --inspect bank.mlt report.json
```

Extract Sonic Shuffle MDT blocks:

```bash
python BankSmith.py --extract-mdt sound.mdt output_folder
```

Validate an editable bank and write a report:

```bash
python BankSmith.py --validate bank.mlt report.csv
```

Perform a no-edit repacking test:

```bash
python BankSmith.py --repack-copy bank.mlt bank_repacked.mlt
```

Export all samples as WAV files:

```bash
python BankSmith.py --export-all bank.mlt output_folder
```

Export all samples through the cleaned-audio path:

```bash
python BankSmith.py --export-all-clean bank.mlt output_folder 60 44100
```

Generate a loop report:

```bash
python BankSmith.py --loop-report bank.mlt loop_report.csv
```

Export a loop-preview WAV for one sample:

```bash
python BankSmith.py --export-loop-preview bank.mlt 0 preview.wav 20 60
```

The exact set of options available in a given build should be confirmed with `--help`.

## Project Files

The editor can store a project description in JSON format. A project may include:

- the path of the opened soundbank;
- preview and export settings;
- sample aliases;
- paths of replacement WAV files;
- replacement-related configuration required to reconstruct the editing session.

When a project is restored, replacement encoding may be performed in a background task. Graphical updates remain confined to the main thread.

Project files refer to external source material by path. Moving or deleting replacement WAV files can therefore prevent a project from being reconstructed completely.

## Reports and Exported Metadata

Depending on the selected operation, the application can produce:

- sample alias tables;
- loop-point reports;
- structural validation reports;
- sample-rate and pitch diagnostics;
- replacement quality measurements;
- raw encoded payloads;
- repacking and integrity information.

Generated aliases are used because the examined soundbank structures do not necessarily contain human-readable sample names. Such aliases are descriptive editor metadata and should not be treated as original names recovered from the archive.

## Optional Analysis Modules

Some specialised menu commands may use additional Python modules located beside the main application:

- `mlt_deep_audit.py`
- `mlt_samplerate_forensics.py`
- `mlt_parameter_forensics.py`

Their absence does not prevent the principal operations of opening, auditioning, exporting, replacing, validating, or repacking a soundbank.

## Known Limitations

- Some fields in both the gcax and Dreamcast families remain partially understood or title-specific.
- Human-readable sample names may not be present in the source archive.
- Some gcax program/root-key relationships are inferred from recognised MPBP structures.
- Dreamcast playback-rate reconstruction relies on base-note metadata and the observed Sega AICA convention.
- External players and operating-system audio facilities may behave differently across platforms.
- Successful structural validation does not guarantee acceptance by every game-specific loader.
- Audio quality after lossy ADPCM encoding depends on the source material, codec state, loop placement, and target runtime.

## Research and Verification Considerations

For reproducible technical work, retain the following materials together:

- the original source soundbank;
- the edited archive;
- the project JSON file;
- replacement WAV sources;
- exported validation and loop reports;
- the exact version of the application used for repacking.

Hashing the original and final files is recommended when documenting experiments or maintaining an auditable modification history.

## Final Operational Note

Before a modified archive is distributed or integrated into a project, preserve the source file, run validation, inspect the reported loop and rate data, and test the bank in the intended runtime. Binary correctness, perceptual audio quality, and game-specific compatibility are related but distinct verification requirements.

## Dynamic Right-Hand Panel

The vertical scrollbar of the right-hand operations panel is displayed only when the panel content exceeds the currently available window height. It is removed automatically when the window becomes sufficiently tall and restored immediately when the window is reduced. Mouse-wheel navigation is supported while the pointer is positioned over the panel.

The `Program filter` control restricts the sample list according to program usage. Its default value, `All programs`, disables program-specific filtering.

## Modular Source Layout

The original ~4,400-line single-file implementation has been split into focused modules.

### Shared / audio

- `mlt_soundbank_explorer/core.py` — shared records, binary helpers, rate logic;
- `mlt_soundbank_explorer/audio.py` — WAV I/O, resampling, loop-preview processing;
- `mlt_soundbank_explorer/render_audio.py` — clean-render and quality-processing helpers.

### GameCube gcax

- `mlt_soundbank_explorer/dsp.py` — Nintendo/GameCube DSP-ADPCM codec;
- `mlt_soundbank_explorer/gcax_chunks.py` — chunk traversal and padding handling;
- `mlt_soundbank_explorer/gcax_parse.py` — MLT/MPB parsing;
- `mlt_soundbank_explorer/gcax_metadata.py` — sample/program metadata and playback helpers;
- `mlt_soundbank_explorer/gcax_edit.py` — replacement and repacking;
- `mlt_soundbank_explorer/gcax_validation.py` — structural validation and batch operations;
- `mlt_soundbank_explorer/gcax_render.py` — rendered export layer;
- `mlt_soundbank_explorer/bank.py` — public gcax bank composition.

### Dreamcast / Sonic Shuffle

- `mlt_soundbank_explorer/dreamcast_codec.py` — AICA ADPCM, PCM8, and PCM16 codecs;
- `mlt_soundbank_explorer/dreamcast_images.py` — SMPB/SMDB/SOSB binary images and rebuilders;
- `mlt_soundbank_explorer/dreamcast_bank.py` — editable standalone MPB, SMLT, and MDT bank API.

### Dispatch / UI

- `mlt_soundbank_explorer/formats.py` — format-family probing;
- `mlt_soundbank_explorer/adapters.py` — editable-backend dispatch and standalone gcaxMPB adapter;
- `mlt_soundbank_explorer/gui_base.py`, `gui_actions.py`, `gui_inspection.py`, `gui_project.py`, `gui_render.py` — modular GUI layers;
- `mlt_soundbank_explorer/multi_gui.py` — multi-format GUI;
- `mlt_soundbank_explorer/cli.py` — command-line interface.

`BankSmith.py` is the primary launcher. The historical `MLT_Soundbank_Explorer.py`, `Multi_Format_Soundbank_Explorer.py`, `soundbank_formats.py`, and `multi_format_adapter.py` entry points remain as compatibility wrappers.

## Major Fixes Included in the Multi-Format Rewrite

The rewrite also fixes a number of issues discovered while running real game assets through the editor:

- restored the missing `ChildChunkInfo` dataclass construction after the monolith split;
- added correct handling for real-world `0x55` and `0x00` inter-chunk/tail padding;
- made no-edit gcax saves byte-identical instead of unnecessarily reconstructing unchanged banks;
- fixed standalone gcaxMPB identity validation against the real source bytes rather than the temporary synthetic MLT wrapper;
- added standalone gcaxMPB editing without forcing output back into an MLT container;
- preserved the original file extension when saving MLT/MPB/MDT sources;
- fixed raw GameCube PCM16 sample-length derivation where DSP-style count fields are zero;
- fixed raw PCM16 loop interpretation to use direct sample indices;
- preserved raw PCM16 encoding during replacement instead of converting it to DSP-ADPCM;
- added separate validation for raw PCM replacement payloads;
- corrected staged raw-PCM replacement preview/decode behaviour;
- moved cubic replacement resampling into the shared audio layer to remove the old cross-module dependency;
- removed the old clean-audio runtime monkey-patching and replaced it with normal mixin composition;
- added Dreamcast SMPB/SMDB Program → Layer → Split parsing;
- added Dreamcast SOSB parsing for Sonic Shuffle MDT data;
- added AICA ADPCM decode/encode;
- added Dreamcast signed PCM8 and PCM16LE decode/encode;
- added Dreamcast base-note playback-rate reconstruction;
- added SMLT unit rebuilding with updated file offsets and sizes;
- added MDT offset-table rebuilding when embedded blocks change size;
- added Dreamcast checksum/file-size regeneration;
- added CLI dispatch across all editable format families;
- separated gcax-only reverse-engineering diagnostics from Dreamcast/AICA views;
- added Python 3.10/3.12 CI with compile and smoke tests.

## Corpus Acceptance Results

The version promoted to `main` was tested against the supplied mixed GameCube/Dreamcast/Sonic Shuffle corpus before merge.

Final acceptance results:

- **176 / 176 files recognised**;
- **176 / 176 files opened through an editable backend**;
- **176 / 176 no-edit repacks byte-identical**;
- **7,515 total sample/tone slots enumerated**;
- **7,511 real sample/tone payloads decoded successfully**;
- **4 known E-102 null/placeholder slots handled as empty records**;
- GameCube DSP-ADPCM decode/export/replacement/repack/reopen: **passed**;
- GameCube raw PCM16 decode/export/replacement/repack/reopen: **passed**;
- standalone GameCube gcaxMPB: **passed**;
- Dreamcast AICA ADPCM: **passed**;
- Dreamcast PCM8: **passed**;
- Dreamcast PCM16LE: **passed**;
- standalone Dreamcast SMPB/SMDB: **passed**;
- Dreamcast SMLT replacement/rebuild/reopen: **passed**;
- Sonic Shuffle MDT SMPB/SMDB replacement/rebuild/reopen: **passed**;
- Sonic Shuffle MDT SOSB replacement/rebuild/reopen: **passed**;
- representative WAV generation and loop-preview generation from every bank family: **passed**;
- actual WAV replacement → validation → save → reopen → decode → re-validation on **176 / 176 banks**: **passed**;
- CLI inspection/validation/repack/export paths: **passed**;
- Python 3.10 CI: **passed**;
- Python 3.12 CI: **passed**.

The corpus tests validate parser, codec, editor, repacker, reopen, and WAV-generation behaviour. Game-specific runtime testing is still recommended before distributing modified assets.
