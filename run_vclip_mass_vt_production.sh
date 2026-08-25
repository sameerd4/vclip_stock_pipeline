#!/bin/zsh
set -euo pipefail

REPO="/Users/sameer/Downloads/vclip_stock_pipeline"
WORK="/Users/sameer/Desktop/vclip-work/work"
PY="$HOME/.venvs/pydjirecord/bin/python"

INPUT="$WORK/reconstruction-available-inputs-v1/review-shards-location-final"
OUT="$WORK/reconstructed-vt-v1"
CACHE="$WORK/visual-coherence-cache-vt-v1"
VISION="$WORK/bin/vclip-vision-featureprint"

cd "$REPO"

echo "VClip production reconstruction — original DJI media + VideoToolbox"
echo "=================================================================="
echo "Python: $PY"
echo "Input:  $INPUT"
echo "Output: $OUT"
echo "Cache:  $CACHE"
echo

[[ -x "$PY" ]] || { echo "ERROR: missing Python: $PY" >&2; exit 2; }
"$PY" -c 'import pydjirecord' >/dev/null 2>&1 || {
  echo "ERROR: pydjirecord is not importable from $PY" >&2
  exit 2
}

[[ -d "$INPUT" ]] || { echo "ERROR: missing staged input: $INPUT" >&2; exit 2; }
[[ -x "$VISION" ]] || { echo "ERROR: missing Vision helper: $VISION" >&2; exit 2; }
[[ -f scripts/vclip_mass_reconstruct.py ]] || {
  echo "ERROR: missing scripts/vclip_mass_reconstruct.py" >&2
  exit 2
}
[[ -f scripts/vclip_reconstruct_shard.py ]] || {
  echo "ERROR: missing scripts/vclip_reconstruct_shard.py" >&2
  exit 2
}
[[ -f scripts/vclip_visual_coherence.py ]] || {
  echo "ERROR: missing scripts/vclip_visual_coherence.py" >&2
  exit 2
}

# Fail closed if the currently installed visual module is not the validated
# original-media VideoToolbox implementation.
grep -q '"videotoolbox"' scripts/vclip_visual_coherence.py || {
  echo "ERROR: installed visual module does not contain the validated VideoToolbox path." >&2
  exit 2
}
if grep -q 'dji_lrf' scripts/vclip_visual_coherence.py; then
  echo "ERROR: installed visual module still contains the experimental LRF path." >&2
  exit 2
fi

echo "Preflight: PASS"
echo "Mode: original DJI source media; VideoToolbox decode; proven Vision thresholds."
echo "Resume: enabled. Ctrl-C is safe; rerun this same script to continue."
echo

exec "$PY" scripts/vclip_mass_reconstruct.py \
  --input-root "$INPUT" \
  --output-root "$OUT" \
  --visual-cache-dir "$CACHE" \
  --vision-helper "$VISION"
