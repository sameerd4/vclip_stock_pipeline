# VClip Card AutoCut

`vclip_card_autocut.py` turns a fresh DJI card into an importable Final Cut Pro
best-of project without transcoding the source footage.

## What it does

1. Recursively scans a card/copy for MP4/MOV files.
2. Pairs each video with a same-stem DJI SRT when available.
3. Probes duration, resolution, frame rate, and capture time.
4. Proposes overlapping 5–14 second source ranges while trimming the beginning
   and end of each recording by default.
5. Scores ranges from SRT GPS/altitude motion evidence. Fast vertical movement,
   near-ground footage, very high translation speed, and abrupt speed changes are
   penalized.
6. Optionally runs the existing Apple Vision visual-coherence analyzer on the
   strongest ranges from each source.
7. Greedily selects a non-overlapping, source-diverse set until the requested
   target reel duration is reached.
8. Writes one FCPXML event/project with the chosen ranges in capture order.
9. References original media directly and applies a Camera LUT through the
   FCPXML asset `customLUTOverride` when the LUT can be resolved safely.
10. Writes JSON and CSV evidence so every selection remains inspectable.

This command does **not** mint canonical VClip IDs or ingest media into the
canonical library. It is an editorial front door. The resulting selects can
later feed the normal VClip stock/reconstruction workflow.

## D-Log M / Camera LUT

Final Cut Camera LUT identity is stored as an opaque `customLUTOverride` value.
AutoCut deliberately does not invent this value.

The preferred way to bootstrap an Air 3 D-Log M workflow is to point AutoCut at
an older FCPXML exported from Final Cut that already contains the correct Air 3
Camera LUT:

```bash
PY="$HOME/.venvs/pydjirecord/bin/python"

PYTHONPATH="$PWD/src:$PWD/scripts" \
"$PY" scripts/vclip_card_autocut.py \
  --media-root "/Volumes/DJI SD CARD/DCIM" \
  --output "$HOME/Desktop/2026-08-29-air3-best-of.fcpxml" \
  --target-seconds 120 \
  --lut-template-fcpxml "/path/to/known-air3-dlogm.fcpxml" \
  --require-camera-lut
```

Alternatively pass the exact Final Cut override explicitly:

```bash
--camera-lut '...exact customLUTOverride value...'
```

`--db /path/to/vclip.sqlite3` can also auto-resolve the LUT when the database has
exactly one plausible DJI/D-Log Camera LUT value. Ambiguous databases fail
closed when `--require-camera-lut` is supplied.

## Visual coherence

SRT-only ranking works without any additional helper. To add the existing
on-device Vision coherence gate:

```bash
--visual-helper "/path/to/vclip-vision-feature-print" \
--visual-cache "$HOME/Desktop/vclip-card-autocut-cache" \
--require-visual
```

The visual pass runs only on the strongest telemetry candidates from each source
instead of every overlapping window.

## Useful controls

- `--target-seconds 120`: approximate total compilation duration.
- `--min-seconds 5`: minimum candidate shot length.
- `--max-seconds 14`: maximum candidate shot length.
- `--stride-seconds 4`: spacing between candidate start points.
- `--edge-trim-seconds 2`: avoid the first/last seconds of raw recordings.
- `--max-per-source 2`: diversity cap per raw video file.
- `--event-name` / `--project-name`: override generated Final Cut names.

The generated JSON report contains the selected ranges, LUT resolution method,
visual status, and VClip validation result. The adjacent CSV contains every
candidate with its score and reasons.
