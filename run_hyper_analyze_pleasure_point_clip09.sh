#!/bin/zsh
set -euo pipefail

REPO="/Users/sameer/Downloads/vclip_stock_pipeline"
WORK="/Users/sameer/Desktop/vclip-work/work"
PY="$HOME/.venvs/pydjirecord/bin/python"

RANKED="$WORK/reconstructed-shards/pleasure-point-telemetry-v2/ready-motion-audit/ready-cuts-ranked.csv"
OUT="$WORK/reconstructed-shards/pleasure-point-telemetry-v2/hyper-analysis/graded-2-clip-09"
PROJECT="Pleasure Point Evening — Graded 2 — Clip 09"

cd "$REPO"

"$PY" "$REPO/scripts/vclip_hyper_analyze_ready_cut.py" \
  --ranked-csv "$RANKED" \
  --project-name "$PROJECT" \
  --output-dir "$OUT"

echo
echo "Hyper analysis complete:"
echo "$OUT/summary.txt"
