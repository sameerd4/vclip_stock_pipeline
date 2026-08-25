#!/bin/zsh
set -euo pipefail

REPO="/Users/sameer/Downloads/vclip_stock_pipeline"
WORK="/Users/sameer/Desktop/vclip-work/work"
PY="$HOME/.venvs/pydjirecord/bin/python"
INPUT="$WORK/review-shards-location-final/may-2026-california/may-2026-california-restockified-review--california-pleasure-point--01.fcpxml"
OUTDIR="$WORK/reconstructed-shards/pleasure-point-telemetry-v2"
OUTPUT="$OUTDIR/pleasure-point-telemetry-v2.fcpxml"
REPORT="$OUTDIR/pleasure-point-telemetry-v2.json"
FLIGHTS="$HOME/Documents/Drone Flight Records"
MEDIA="/Volumes/PRO-G40 4TB/drone"
CACHE="$WORK/telemetry-qc"
CLIP07_PARENT="VCLIP_FE4BE3EACB57202A4C19"

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

"$PY" scripts/vclip_reconstruct_shard.py \
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
  --transition-pad 0.50

# Regression fixture discovered by human visual review:
# Graded 1 Clip 07's old auto trim still visibly turns. v2 must not promote
# any generated repair derived from this parent directly into Ready Cuts.
"$PY" - "$REPORT" "$CLIP07_PARENT" <<'PY'
import csv
import sys
from pathlib import Path

report = Path(sys.argv[1])
parent = sys.argv[2]
ready_csv = report.with_suffix('.ready.csv')

with ready_csv.open(newline='') as f:
    rows = list(csv.DictReader(f))

related = [r for r in rows if r.get('parent_id') == parent]
if not related:
    raise SystemExit(f"REGRESSION CHECK FAILED: no rows for {parent}")

bad = [
    r for r in related
    if r.get('stock_clip_id') != parent and r.get('bucket') == 'ready'
]
if bad:
    names = ', '.join(r.get('project_name', '?') for r in bad)
    raise SystemExit(
        'REGRESSION CHECK FAILED: Clip 07 generated repair was promoted to Ready Cuts: '
        + names
    )

print()
print('PLEASURE POINT REGRESSION CHECK')
print('===============================')
print('Clip 07 generated repair not auto-promoted: PASS')
for row in related:
    print(
        f"  {row.get('bucket','?'):16s}  "
        f"{row.get('project_name','?')}  "
        f"operator={row.get('operator_status','?')}  "
        f"reasons={row.get('operator_reasons','')}"
    )
PY

print ""
print "Open the v2 XML with:"
print "open -a \"Final Cut Pro\" \"$OUTPUT\""
