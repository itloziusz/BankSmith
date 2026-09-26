# MLT Soundbank Explorer

## Overview

**MLT Soundbank Explorer** is a desktop application for the inspection, auditioning, extraction, replacement, validation, and repacking of `gcaxMLT` soundbanks and their embedded `gcaxMPB` sample data. The program is implemented in Python and uses Tkinter for its graphical user interface.

The application is intended for technical analysis and controlled editing of soundbanks that employ Nintendo/GameCube DSP-ADPCM sample encoding. Its design prioritises structural preservation, reproducible output, explicit validation, and non-destructive file handling.

## Principal Capabilities

The application provides the following functions:

- opening `gcaxMLT` archives and examining embedded `gcaxMPB` structures;
- enumerating sample records, stored rate values, loop metadata, and inferred program references;
- decoding DSP-ADPCM material for real-time auditioning and PCM WAV export;
- generating continuous loop previews without an intro-to-loop playback hand-off;
- importing PCM and IEEE floating-point WAV files;
- converting imported material to mono 16-bit PCM before replacement encoding;
- resampling imported audio to the effective target rate;
- re-encoding replacement audio as compatible DSP-ADPCM data;
- rebuilding the MPBW sample-data region and updating MPBP offsets;
- writing a repacked MLT archive to a new output file;
- exporting alias tables, loop reports, validation reports, quality-control data, and raw encoded payloads;
- storing editor state and replacement references in project files.

## Safety and File-Integrity Model

MLT Soundbank Explorer does not automatically overwrite the source archive. Edited banks should be written to a distinct output path by using the **Save Repacked As** command.

A conservative workflow is strongly recommended:

1. Retain an unmodified copy of the original MLT archive.
2. Perform all edits against a duplicate or working copy.
3. Run the built-in validation procedure before deployment.
4. Compare the repacked output with the source structure where appropriate.
5. Verify the resulting bank in the intended game or runtime environment.

Although the application preserves known structures and recalculates affected offsets, the MLT family contains fields whose interpretation may vary between titles. Runtime verification therefore remains necessary.

## System Requirements

- Python 3.10 or later
- Tkinter

The standard Windows distribution of Python normally includes Tkinter. On Debian- or Ubuntu-based Linux systems, Tkinter may require a separate package:

```bash
sudo apt install python3-tk
```

The core application does not require third-party Python packages.

## Launching the Application

Start the graphical interface with:

```bash
python MLT_Soundbank_Explorer.py
```

Open a specific MLT archive at launch with:

```bash
python MLT_Soundbank_Explorer.py bank.mlt
```

## Default Playback-Rate Preset

The default preset is:

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

Because the default configuration is the stored base rate, opening or exporting a bank does not silently impose a C4- or C3-based pitch correction.

## Recommended Editing Procedure

1. Open the original `.mlt` archive.
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
python MLT_Soundbank_Explorer.py --help
```

Validate a bank and write a report:

```bash
python MLT_Soundbank_Explorer.py --validate bank.mlt report.csv
```

Perform a no-edit repacking test:

```bash
python MLT_Soundbank_Explorer.py --repack-copy bank.mlt bank_repacked.mlt
```

Export all samples as WAV files:

```bash
python MLT_Soundbank_Explorer.py --export-all bank.mlt output_folder
```

Export all samples through the cleaned-audio path:

```bash
python MLT_Soundbank_Explorer.py --export-all-clean bank.mlt output_folder 60 44100
```

Generate a loop report:

```bash
python MLT_Soundbank_Explorer.py --loop-report bank.mlt loop_report.csv
```

Export a loop-preview WAV for one sample:

```bash
python MLT_Soundbank_Explorer.py --export-loop-preview bank.mlt 0 preview.wav 20 60
```

The exact set of options available in a given build should be confirmed with `--help`.

## Project Files

The editor can store a project description in JSON format. A project may include:

- the path of the opened MLT archive;
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
- raw DSP-ADPCM payloads;
- repacking and integrity information.

Generated aliases are used because the examined soundbank structures do not necessarily contain human-readable sample names. Such aliases are descriptive editor metadata and should not be treated as original names recovered from the archive.

## Optional Analysis Modules

Some specialised menu commands may use additional Python modules located beside the main application:

- `mlt_deep_audit.py`
- `mlt_samplerate_forensics.py`
- `mlt_parameter_forensics.py`

Their absence does not prevent the principal operations of opening, auditioning, exporting, replacing, validating, or repacking a soundbank.

## Known Limitations

- The format contains partially understood and potentially title-specific fields.
- Human-readable sample names may not be present in the source archive.
- Program and root-key relationships are inferred from recognised MPBP structures.
- External players and operating-system audio facilities may behave differently across platforms.
- Successful structural validation does not guarantee acceptance by every game-specific loader.
- Audio quality after DSP-ADPCM encoding depends on the source material, coefficient table, loop placement, and target playback behaviour.

## Research and Verification Considerations

For reproducible technical work, retain the following materials together:

- the original MLT archive;
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

The original single-file implementation has been split into focused modules:

- `mlt_soundbank_explorer/core.py` — binary helpers, playback-rate logic and shared data records;
- `mlt_soundbank_explorer/dsp.py` — Nintendo/GameCube DSP-ADPCM codec;
- `mlt_soundbank_explorer/audio.py` — WAV I/O, resampling and loop-preview processing;
- `mlt_soundbank_explorer/bank.py` — gcax MLT/MPB parsing, validation, replacement and repacking;
- `mlt_soundbank_explorer/gui.py` — base Tkinter interface;
- `mlt_soundbank_explorer/clean_audio.py` — optional rendered-audio cleanup/export layer;
- `mlt_soundbank_explorer/cli.py` — command-line entry point;
- `soundbank_formats.py` — format-family probing for gcax, Dreamcast SMLT/SMPB and Sonic Shuffle MDT;
- `multi_format_adapter.py` — standalone gcaxMPB adapter;
- `Multi_Format_Soundbank_Explorer.py` — multi-format GUI launcher.

`MLT_Soundbank_Explorer.py` remains as a small compatibility launcher so existing scripts and launch commands continue to work.
