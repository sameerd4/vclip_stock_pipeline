#!/bin/zsh
set -euo pipefail

REPO="${REPO:-$PWD}"
WORK="${WORK:-$HOME/Desktop/vclip-work/work}"
VENV="${VENV:-$HOME/.venvs/pydjirecord}"
FLIGHTS="${FLIGHTS:-$HOME/Documents/Drone Flight Records}"
SCRIPT="$REPO/scripts/vclip_telemetry_qc.py"
OUT="$WORK/telemetry-qc"

if [[ ! -f "$SCRIPT" ]]; then
  echo "Missing scorer: $SCRIPT" >&2
  exit 2
fi
if [[ ! -x "$VENV/bin/python" ]]; then
  echo "Missing pydjirecord venv: $VENV" >&2
  exit 2
fi
if [[ ! -f "$WORK/vclip.sqlite3" ]]; then
  echo "Missing DB: $WORK/vclip.sqlite3" >&2
  exit 2
fi
if [[ ! -d "$WORK/review-shards-location-final" ]]; then
  echo "Missing historical review root" >&2
  exit 2
fi
if [[ ! -d "$WORK/review-shards-2025-global" ]]; then
  echo "Missing 2025 review root" >&2
  exit 2
fi
if [[ ! -d "$FLIGHTS" ]]; then
  echo "Missing flight-record folder: $FLIGHTS" >&2
  exit 2
fi
if [[ -z "${DJI_API_KEY:-}" ]]; then
  echo "DJI_API_KEY is not loaded in this shell." >&2
  echo 'Run: read -s "DJI_API_KEY?DJI App Key: "; echo; export DJI_API_KEY' >&2
  exit 2
fi

echo "VClip telemetry QC"
echo "  repo:     $REPO"
echo "  work:     $WORK"
echo "  flights:  $FLIGHTS"
echo "  output:   $OUT"
echo "  volumes:"
ls -1 /Volumes | sed 's/^/    - /'
echo

PYTHONPATH="$REPO/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$VENV/bin/python" "$SCRIPT" \
  --review-root "$WORK/review-shards-location-final" \
  --review-root "$WORK/review-shards-2025-global" \
  --db "$WORK/vclip.sqlite3" \
  --flight-record-root "$FLIGHTS" \
  --media-root /Volumes \
  --output-dir "$OUT" \
  "$@"

echo
echo "=== SUMMARY ==="
cat "$OUT/telemetry-qc-summary.txt"
