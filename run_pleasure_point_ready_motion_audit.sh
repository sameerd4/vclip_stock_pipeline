#!/bin/zsh
set -euo pipefail

REPO="/Users/sameer/Downloads/vclip_stock_pipeline"
WORK="/Users/sameer/Desktop/vclip-work/work"
PY="$HOME/.venvs/pydjirecord/bin/python"

REPORT="$WORK/reconstructed-shards/pleasure-point-telemetry-v2/pleasure-point-telemetry-v2.json"
OUT="$WORK/reconstructed-shards/pleasure-point-telemetry-v2/ready-motion-audit"
CACHE="$WORK/telemetry-qc"

cd "$REPO"

if [[ ! -f "$REPORT" ]]; then
  echo "Missing v2 report: $REPORT" >&2
  exit 1
fi

if [[ -z "${DJI_API_KEY:-}" ]]; then
  read -s "DJI_API_KEY?DJI App Key: "
  echo
  export DJI_API_KEY
fi

export PYTHONPATH="$REPO/src:$REPO/scripts${PYTHONPATH:+:$PYTHONPATH}"

"$PY" "$REPO/scripts/vclip_ready_motion_audit.py" \
  --report "$REPORT" \
  --output-dir "$OUT" \
  --cache-dir "$CACHE"

echo
echo "Motion audit complete."
echo "Summary:  $OUT/summary.txt"
echo "Ranked:   $OUT/ready-cuts-ranked.csv"
echo "Hotspots: $OUT/hotspots.csv"
echo "Traces:   $OUT/traces"
