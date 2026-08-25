#!/bin/zsh
set -euo pipefail

cd /Users/sameer/Downloads/vclip_stock_pipeline

PY="$HOME/.venvs/pydjirecord/bin/python"
WORK="/Users/sameer/Desktop/vclip-work/work"
OUT="$WORK/reconstructed-vt-original-test"
CACHE="$WORK/visual-coherence-cache-vt-original-test"
VISION="$WORK/bin/vclip-vision-featureprint"

rm -rf "$OUT" "$CACHE"

echo "Pleasure Point: original 4K media + VideoToolbox"
echo "================================================"
echo "Only the decoder differs from the proven 9m53 baseline."
echo "Visual source: ORIGINAL DJI media"
echo "Sampling:      same"
echo "Resize:        same Lanczos"
echo "Vision helper: same"
echo

time "$PY" scripts/vclip_mass_reconstruct.py \
  --contains "pleasure-point" \
  --limit 1 \
  --fail-fast \
  --output-root "$OUT" \
  --visual-cache-dir "$CACHE" \
  --vision-helper "$VISION"

echo
echo "Frame-cache decoder report:"
"$PY" - "$CACHE" <<'PY'
import json, sys
from collections import Counter
from pathlib import Path

root = Path(sys.argv[1])
counts = Counter()
for manifest in sorted(root.glob("frames/*/manifest.json")):
    d = json.load(open(manifest))
    mode = d.get("decode_mode", "unknown")
    counts[mode] += 1
    print(
        f"{mode:18s} {d.get('frame_count','?'):>5} frames  "
        f"{Path(d.get('media','?')).name}"
    )
print()
print("Counts:", dict(counts))
PY
