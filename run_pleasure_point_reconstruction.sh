#!/bin/zsh
set -euo pipefail

REPO="/Users/sameer/Downloads/vclip_stock_pipeline"
WORK="/Users/sameer/Desktop/vclip-work/work"
PY="$HOME/.venvs/pydjirecord/bin/python"
INPUT="$WORK/review-shards-location-final/may-2026-california/may-2026-california-restockified-review--california-pleasure-point--01.fcpxml"
OUTDIR="$WORK/reconstructed-shards/pleasure-point-telemetry-v1"
OUTPUT="$OUTDIR/pleasure-point-telemetry-v1.fcpxml"
REPORT="$OUTDIR/pleasure-point-telemetry-v1.json"
FLIGHTS="$HOME/Documents/Drone Flight Records"
MEDIA="/Volumes/PRO-G40 4TB/drone"
CACHE="$WORK/telemetry-qc"

cd "$REPO"

if [[ ! -x "$PY" ]]; then
  echo "Missing Python env: $PY" >&2
  exit 1
fi
if [[ ! -f "scripts/vclip_telemetry_qc.py" ]]; then
  echo "Missing scripts/vclip_telemetry_qc.py" >&2
  exit 1
fi
if [[ ! -f "scripts/vclip_reconstruct_shard.py" ]]; then
  echo "Missing scripts/vclip_reconstruct_shard.py" >&2
  exit 1
fi
if [[ ! -f "$INPUT" ]]; then
  echo "Missing Pleasure Point shard: $INPUT" >&2
  exit 1
fi
if [[ ! -d "$MEDIA" ]]; then
  echo "Required PRO-G40 drone archive is not mounted: $MEDIA" >&2
  exit 1
fi
if [[ -z "${DJI_API_KEY:-}" ]]; then
  read -s "DJI_API_KEY?DJI App Key: "
  echo
  export DJI_API_KEY
fi

mkdir -p "$OUTDIR"

export PYTHONPATH="$REPO/src:$REPO/scripts${PYTHONPATH:+:$PYTHONPATH}"

exec "$PY" scripts/vclip_reconstruct_shard.py \
  --input "$INPUT" \
  --output "$OUTPUT" \
  --report "$REPORT" \
  --flight-record-root "$FLIGHTS" \
  --media-root "$MEDIA" \
  --cache-dir "$CACHE" \
  --min-duration 5 \
  --target-duration 12 \
  --max-duration 20 \
  --max-extension-each-side 5 \
  --transition-pad 0.30
