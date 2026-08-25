#!/bin/zsh
set -euo pipefail

REPO="/Users/sameer/Downloads/vclip_stock_pipeline"
WORK="/Users/sameer/Desktop/vclip-work/work"
PY="$HOME/.venvs/pydjirecord/bin/python"

INPUT="$WORK/review-shards-location-final/may-2026-california/may-2026-california-restockified-review--california-pleasure-point--01.fcpxml"
OUTDIR="$WORK/reconstructed-shards/pleasure-point-reconstruction-v3"
OUTPUT="$OUTDIR/pleasure-point-reconstruction-v3.fcpxml"
REPORT="$OUTDIR/pleasure-point-reconstruction-v3.json"

FLIGHTS="$HOME/Documents/Drone Flight Records"
MEDIA="/Volumes/PRO-G40 4TB/drone"
CACHE="$WORK/telemetry-qc"
VISUAL_CACHE="$WORK/visual-coherence-cache-v3"
BIN_DIR="$WORK/bin"
VISION_SRC="$REPO/scripts/vclip_vision_featureprint.swift"
VISION_BIN="$BIN_DIR/vclip-vision-featureprint"

CLIP07="Pleasure Point Evening — Graded 1 — Clip 07"
CLIP09="Pleasure Point Evening — Graded 2 — Clip 09"
CLIP05="Pleasure Point Evening — Graded 1 — Clip 05"

cd "$REPO"

if [[ ! -x "$PY" ]]; then
  echo "Missing Python env: $PY" >&2
  exit 1
fi
for required in \
  scripts/vclip_telemetry_qc.py \
  scripts/vclip_visual_coherence.py \
  scripts/vclip_reconstruct_shard.py \
  scripts/vclip_vision_featureprint.swift
do
  if [[ ! -f "$required" ]]; then
    echo "Missing $required" >&2
    exit 1
  fi
done

if [[ ! -f "$INPUT" ]]; then
  echo "Missing Pleasure Point shard: $INPUT" >&2
  exit 1
fi
if [[ ! -d "$MEDIA" ]]; then
  echo "Required PRO-G40 drone archive is not mounted: $MEDIA" >&2
  exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg is required." >&2
  exit 1
fi
if ! command -v xcrun >/dev/null 2>&1; then
  echo "xcrun / Apple command line tools are required for the local Vision helper." >&2
  exit 1
fi

mkdir -p "$OUTDIR" "$BIN_DIR" "$VISUAL_CACHE"

if [[ ! -x "$VISION_BIN" || "$VISION_SRC" -nt "$VISION_BIN" ]]; then
  echo "Compiling on-device Apple Vision feature-print helper..."
  xcrun swiftc -O \
    "$VISION_SRC" \
    -o "$VISION_BIN" \
    -framework Vision \
    -framework ImageIO \
    -framework CoreGraphics
fi

if [[ -z "${DJI_API_KEY:-}" ]]; then
  read -s "DJI_API_KEY?DJI App Key: "
  echo
  export DJI_API_KEY
fi

export PYTHONPATH="$REPO/src:$REPO/scripts${PYTHONPATH:+:$PYTHONPATH}"

"$PY" scripts/vclip_reconstruct_shard.py \
  --input "$INPUT" \
  --output "$OUTPUT" \
  --report "$REPORT" \
  --flight-record-root "$FLIGHTS" \
  --media-root "$MEDIA" \
  --cache-dir "$CACHE" \
  --visual-helper "$VISION_BIN" \
  --visual-cache-dir "$VISUAL_CACHE" \
  --visual-fps 2 \
  --visual-width 320 \
  --min-duration 5 \
  --target-duration 12 \
  --max-duration 20 \
  --max-extension-each-side 5 \
  --transition-pad 0.50

"$PY" - "$REPORT" "$CLIP07" "$CLIP09" "$CLIP05" <<'PY'
import csv
import sys
from pathlib import Path

report = Path(sys.argv[1])
clip07, clip09, clip05 = sys.argv[2:5]
ready_csv = report.with_suffix(".ready.csv")

with ready_csv.open(newline="") as f:
    rows = list(csv.DictReader(f))

def related(name):
    return [r for r in rows if name in (r.get("project_name") or "")]

def show(name):
    print()
    print(name)
    for r in related(name):
        print(
            f"  bucket={r.get('bucket','?'):16s} "
            f"dur={r.get('duration_s','?'):>8s} "
            f"action={r.get('action','?'):22s} "
            f"visual={r.get('visual_status','?'):10s} "
            f"basis={r.get('readiness_basis','?')}"
        )
        if r.get("visual_reasons"):
            print(f"    visual reasons: {r['visual_reasons']}")
        if r.get("visual_suggested_boundary_s"):
            print(f"    visual boundary: {r['visual_suggested_boundary_s']}s")
        if r.get("operator_reasons"):
            print(f"    operator reasons: {r['operator_reasons']}")

print()
print("PLEASURE POINT V3 REGRESSION CHECK")
print("==================================")

# Clip 07: no generated repair may be promoted automatically.
r07 = related(clip07)
bad07 = [
    r for r in r07
    if r.get("bucket") == "ready"
    and r.get("action") != "historical-ready"
]
if bad07:
    raise SystemExit(
        "REGRESSION FAILED: Clip 07 generated repair became Ready: "
        + ", ".join(r.get("project_name", "?") for r in bad07)
    )
print("Clip 07 generated repair blocked: PASS")

# Clip 09: the full historical ten-second interval is human-labeled FALSE POSITIVE.
r09 = related(clip09)
bad09 = [
    r for r in r09
    if r.get("bucket") == "ready"
    and float(r.get("duration_s") or 0) >= 9.5
]
if bad09:
    show(clip09)
    raise SystemExit(
        "REGRESSION FAILED: full Clip 09 is still Ready. "
        "Visual coherence did not catch the known repositioning."
    )
print("Full 10s Clip 09 not Ready: PASS")

# Clip 05 is a human positive control: the original clip should recover to Ready.
r05 = related(clip05)
good05 = [
    r for r in r05
    if r.get("bucket") == "ready"
    and r.get("project_name") == clip05
]
if not good05:
    show(clip05)
    raise SystemExit(
        "REGRESSION FAILED: good Clip 05 did not recover to Ready. "
        "Use the printed visual evidence to calibrate v3."
    )
print("Good Clip 05 recovered to Ready: PASS")

show(clip09)
show(clip05)
show(clip07)
PY

echo
echo "Open the v3 XML with:"
echo "open -a \"Final Cut Pro\" \"$OUTPUT\""
