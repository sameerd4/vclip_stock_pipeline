#!/bin/zsh
set -euo pipefail

REPO="${VCLIP_REPO:-/Users/sameer/Downloads/vclip_stock_pipeline}"
WORK="${VCLIP_WORK:-/Users/sameer/Desktop/vclip-work/work}"
PY_DEFAULT="$HOME/.venvs/pydjirecord/bin/python"
PY_REQUESTED="${VCLIP_PYTHON:-$PY_DEFAULT}"

python_has_pydjirecord() {
  local candidate="$1"
  [[ -x "$candidate" ]] || return 1
  "$candidate" -c 'import pydjirecord' >/dev/null 2>&1
}

if python_has_pydjirecord "$PY_REQUESTED"; then
  PY="$PY_REQUESTED"
elif python_has_pydjirecord "$PY_DEFAULT"; then
  if [[ "$PY_REQUESTED" != "$PY_DEFAULT" ]]; then
    echo "WARNING: ignoring VCLIP_PYTHON=$PY_REQUESTED (pydjirecord unavailable)." >&2
    echo "Using DJI-capable interpreter: $PY_DEFAULT" >&2
  fi
  PY="$PY_DEFAULT"
else
  echo "ERROR: no DJI-capable Python interpreter found." >&2
  echo "Expected: $PY_DEFAULT" >&2
  echo "Tested:   $PY_REQUESTED" >&2
  echo "VClip reconstruction requires 'import pydjirecord' to succeed." >&2
  exit 2
fi
DB="${VCLIP_DB:-$WORK/vclip.sqlite3}"
FLIGHTS="${VCLIP_FLIGHT_RECORDS:-$HOME/Documents/Drone Flight Records}"
RENDER_ROOT="${VCLIP_RENDER_ROOT:-/Volumes/PRO-G40 2TB/VClip Render Pool}"
VISUAL_FPS="${VCLIP_VISUAL_FPS:-1.0}"
VISUAL_WIDTH="${VCLIP_VISUAL_WIDTH:-256}"

INPUT_A="$WORK/review-shards-location-final"
INPUT_B="$WORK/review-shards-2025-global"
CORPUS="$WORK/reconstructed-corpus-v1"
RAW="$CORPUS/raw"
DEDUPED="$CORPUS/deduped"
DEDUP_REPORT="$CORPUS/reports/corpus-dedupe.json"
DEDUP_MANIFEST="$CORPUS/reconstructed-active-manifest.json"
MEDIA_CLOSURE_REPORT="$CORPUS/reports/media-closure.json"
AVAILABLE_STAGE="$WORK/reconstruction-available-inputs-v1"
AVAILABLE_REPORT="$CORPUS/reports/available-corpus.json"
PLAN_ROOT="$CORPUS/export-plans"
CURRENT_PLAN_FILE="$CORPUS/current-export-plan.txt"
TELEMETRY_CACHE="$WORK/telemetry-qc"
VISUAL_CACHE="$WORK/visual-coherence-cache-corpus-v1"
BIN_DIR="$WORK/bin"
VISION_BIN="$BIN_DIR/vclip-vision-featureprint"
FCP_AX_BIN="$BIN_DIR/vclip-fcp-ax"

export PYTHONPATH="$REPO/src:$REPO/scripts${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p "$CORPUS/reports" "$BIN_DIR"

usage() {
  cat <<EOF
Usage: ./vclip_forge.sh COMMAND [ARGS]

Commands:
  doctor             Verify Python/pydjirecord and core local tooling.
  tools              Compile local Apple Vision and Final Cut AX helpers.
  preflight          Strict archival media closure audit.
  preflight-available Available-now audit; missing/ambiguous media is deferred.
  stage-available     Build disposable FCPXML inputs containing only resolvable VClips.
  reconstruct         Strict/full reconstruction (resumable).
  reconstruct-available
                     Reconstruct all currently available VClips; failed shards are recorded.
  dedupe             Remove obvious duplicate Ready Cuts/Masters globally.
  catalog            Import the deduped reconstructed pool into vclip.sqlite3.
  plan               Build deterministic one-event FCP export batches and register them.
  prepare            Strict: preflight -> reconstruct -> dedupe -> catalog -> plan.
  prepare-available  Process mounted historical/location-final corpus NOW.
                     2025-global is intentionally skipped in available-now mode.
                     partial preflight -> stage -> reconstruct -> dedupe -> catalog.
                     Builds export plan too when the render volume is mounted.
  pilot-export       Export only the first planned batch through Final Cut.
  export             Export every planned batch through Final Cut (resumable).
  ingest             Explicitly ingest verified rendered masters into SQLite.
  stats              Show reconstructed/rendered pool statistics.
  search QUERY...    Search the reconstructed pool.
  paths              Print important paths.

Environment overrides:
  VCLIP_RENDER_ROOT   Default: $RENDER_ROOT
  VCLIP_VISUAL_FPS    Default: $VISUAL_FPS (use 2.0 for finer/slower analysis)
  VCLIP_VISUAL_WIDTH  Default: $VISUAL_WIDTH
EOF
}

require_file() {
  [[ -f "$1" ]] || { echo "Missing file: $1" >&2; exit 1; }
}

require_dir() {
  [[ -d "$1" ]] || { echo "Missing directory: $1" >&2; exit 1; }
}

ensure_key() {
  if [[ -z "${DJI_API_KEY:-}" ]]; then
    read -s "DJI_API_KEY?DJI App Key: "
    echo
    export DJI_API_KEY
  fi
}

compile_tools() {
  require_file "$REPO/scripts/vclip_vision_featureprint.swift"
  require_file "$REPO/scripts/vclip_fcp_ax.swift"
  if [[ ! -x "$VISION_BIN" || "$REPO/scripts/vclip_vision_featureprint.swift" -nt "$VISION_BIN" ]]; then
    echo "Compiling Apple Vision source-index helper..."
    xcrun swiftc -O \
      "$REPO/scripts/vclip_vision_featureprint.swift" \
      -o "$VISION_BIN" \
      -framework Vision \
      -framework ImageIO \
      -framework CoreGraphics
  fi
  if [[ ! -x "$FCP_AX_BIN" || "$REPO/scripts/vclip_fcp_ax.swift" -nt "$FCP_AX_BIN" ]]; then
    echo "Compiling Final Cut Accessibility controller..."
    xcrun swiftc -O \
      "$REPO/scripts/vclip_fcp_ax.swift" \
      -o "$FCP_AX_BIN" \
      -framework AppKit \
      -framework ApplicationServices
  fi
  echo "Vision: $VISION_BIN"
  echo "FCP AX: $FCP_AX_BIN"
}

current_plan() {
  [[ -f "$CURRENT_PLAN_FILE" ]] || {
    echo "No current export plan. Run: ./vclip_forge.sh plan" >&2
    exit 1
  }
  cat "$CURRENT_PLAN_FILE"
}

preflight_available() {
  require_dir "$INPUT_A"
  "$PY" "$REPO/scripts/vclip_media_closure.py" \
    --input-root "$INPUT_A" \
    --media-root /Volumes \
    --report "$MEDIA_CLOSURE_REPORT" \
    --allow-partial
}

stage_available() {
  require_file "$MEDIA_CLOSURE_REPORT"
  rm -rf "$AVAILABLE_STAGE"
  "$PY" "$REPO/scripts/vclip_stage_available.py" \
    --input-root "$INPUT_A" \
    --closure-report "$MEDIA_CLOSURE_REPORT" \
    --output-root "$AVAILABLE_STAGE" \
    --report "$AVAILABLE_REPORT"
}

reconstruct_available() {
  compile_tools
  echo "Reconstruction Python: $PY"
  "$PY" -c 'import pydjirecord; print("pydjirecord import: OK")'
  ensure_key
  require_dir "$AVAILABLE_STAGE/review-shards-location-final"
  require_dir "$FLIGHTS"
  "$PY" "$REPO/scripts/vclip_reconstruct_corpus.py" \
    --input-root "$AVAILABLE_STAGE/review-shards-location-final" \
    --output-root "$CORPUS" \
    --compiler "$REPO/scripts/vclip_reconstruct_shard.py" \
    --python "$PY" \
    --flight-record-root "$FLIGHTS" \
    --media-root /Volumes \
    --telemetry-cache-dir "$TELEMETRY_CACHE" \
    --visual-cache-dir "$VISUAL_CACHE" \
    --visual-helper "$VISION_BIN" \
    --visual-fps "$VISUAL_FPS" \
    --visual-width "$VISUAL_WIDTH" \
    --min-duration 3 \
    --target-duration 12 \
    --max-duration 20 \
    --max-extension-each-side 5 \
    --transition-pad 0.50 \
    --allow-failures \
    "$@"
}

cmd="${1:-}"
shift || true

case "$cmd" in
  doctor)
    echo "VClip Forge doctor"
    echo "=================="
    echo "Python: $PY"
    "$PY" - <<'PY'
import sys
import pydjirecord
print("Python version:", sys.version.split()[0])
print("pydjirecord:", getattr(pydjirecord, "__version__", "import OK"))
PY
    echo "PYTHONPATH: $PYTHONPATH"
    [[ -x "$VISION_BIN" ]] && echo "Vision helper: OK  $VISION_BIN" || echo "Vision helper: NOT COMPILED"
    [[ -x "$FCP_AX_BIN" ]] && echo "FCP AX helper: OK $FCP_AX_BIN" || echo "FCP AX helper: NOT COMPILED"
    ;;
  tools)
    compile_tools
    ;;

  preflight)
    require_dir "$INPUT_A"
    require_dir "$INPUT_B"
    "$PY" "$REPO/scripts/vclip_media_closure.py" \
      --input-root "$INPUT_A" \
      --input-root "$INPUT_B" \
      --media-root /Volumes \
      --report "$MEDIA_CLOSURE_REPORT"
    ;;

  preflight-available)
    preflight_available
    ;;

  stage-available)
    stage_available
    ;;

  reconstruct-available)
    reconstruct_available "$@"
    ;;

  reconstruct)
    compile_tools
    ensure_key
    require_dir "$INPUT_A"
    require_dir "$INPUT_B"
    require_dir "$FLIGHTS"
    "$PY" "$REPO/scripts/vclip_reconstruct_corpus.py" \
      --input-root "$INPUT_A" \
      --input-root "$INPUT_B" \
      --output-root "$CORPUS" \
      --compiler "$REPO/scripts/vclip_reconstruct_shard.py" \
      --python "$PY" \
      --flight-record-root "$FLIGHTS" \
      --media-root /Volumes \
      --telemetry-cache-dir "$TELEMETRY_CACHE" \
      --visual-cache-dir "$VISUAL_CACHE" \
      --visual-helper "$VISION_BIN" \
      --visual-fps "$VISUAL_FPS" \
      --visual-width "$VISUAL_WIDTH" \
      --min-duration 3 \
      --target-duration 12 \
      --max-duration 20 \
      --max-extension-each-side 5 \
      --transition-pad 0.50 \
      "$@"
    ;;

  dedupe)
    require_dir "$RAW"
    rm -rf "$DEDUPED"
    "$PY" "$REPO/scripts/vclip_reconstructed_dedupe.py" \
      --input-root "$RAW" \
      --output-root "$DEDUPED" \
      --report "$DEDUP_REPORT" \
      --manifest "$DEDUP_MANIFEST"
    ;;

  catalog)
    require_file "$DEDUP_MANIFEST"
    "$PY" "$REPO/scripts/vclip_pool_db.py" \
      --db "$DB" \
      import-corpus \
      --manifest "$DEDUP_MANIFEST"
    ;;

  plan)
    compile_tools
    require_dir "$DEDUPED"
    require_dir "$(dirname "$RENDER_ROOT")"
    "$PY" "$REPO/scripts/vclip_export_plan.py" \
      --db "$DB" \
      --xml-root "$DEDUPED" \
      --output-root "$PLAN_ROOT" \
      --render-root "$RENDER_ROOT" \
      --max-projects 40 \
      --share-destination "Export File (default)…"
    plan="$(find "$PLAN_ROOT" -name export-plan.json -type f -print0 | xargs -0 ls -t | head -1)"
    [[ -n "$plan" ]] || { echo "Could not locate generated export plan" >&2; exit 1; }
    echo "$plan" > "$CURRENT_PLAN_FILE"
    "$PY" "$REPO/scripts/vclip_pool_db.py" \
      --db "$DB" \
      register-plan \
      --manifest "$plan"
    echo "Current plan: $plan"
    ;;

  prepare)
    "$0" preflight
    "$0" reconstruct "$@"
    "$0" dedupe
    "$0" catalog
    "$0" plan
    ;;

  prepare-available)
    preflight_available
    stage_available
    reconstruct_available "$@"
    "$0" dedupe
    "$0" catalog
    if [[ -d "$(dirname "$RENDER_ROOT")" ]]; then
      "$0" plan
    else
      echo
      echo "Catalog is ready. Export plan skipped because render volume is offline:"
      echo "  $(dirname "$RENDER_ROOT")"
      echo "Mount it later (or set VCLIP_RENDER_ROOT), then run:"
      echo "  ./vclip_forge.sh plan"
    fi
    ;;

  pilot-export)
    compile_tools
    plan="$(current_plan)"
    "$PY" "$REPO/scripts/vclip_export_worker.py" \
      --manifest "$plan" \
      --ax-binary "$FCP_AX_BIN" \
      --db "$DB" \
      --limit-batches 1
    ;;

  export)
    compile_tools
    plan="$(current_plan)"
    "$PY" "$REPO/scripts/vclip_export_worker.py" \
      --manifest "$plan" \
      --ax-binary "$FCP_AX_BIN" \
      --db "$DB" \
      "$@"
    ;;

  ingest)
    plan="$(current_plan)"
    report="$CORPUS/reports/rendered-master-ingest.json"
    "$PY" "$REPO/scripts/vclip_pool_db.py" \
      --db "$DB" \
      ingest-exports \
      --manifest "$plan" \
      --report "$report"
    ;;

  stats)
    "$PY" "$REPO/scripts/vclip_pool_db.py" --db "$DB" stats
    ;;

  search)
    "$PY" "$REPO/scripts/vclip_pool_db.py" --db "$DB" search "$*"
    ;;

  paths)
    cat <<EOF
Repo:             $REPO
Work:             $WORK
DB:               $DB
Inputs:           $INPUT_A
                  $INPUT_B
Corpus:           $CORPUS
Raw recon:        $RAW
Deduped recon:    $DEDUPED
Active manifest:  $DEDUP_MANIFEST
Available stage:  $AVAILABLE_STAGE
Available report: $AVAILABLE_REPORT
Export plan root: $PLAN_ROOT
Render root:      $RENDER_ROOT
Current plan:     ${CURRENT_PLAN_FILE}
Telemetry cache:  $TELEMETRY_CACHE
Visual cache:     $VISUAL_CACHE
EOF
    ;;

  *)
    usage
    exit 2
    ;;
esac
