# VClip Quick Start

## 1. Install

```bash
cd /path/to/vclip_stock_pipeline
make dev
source .venv/bin/activate
```

## 2. Export the finished Final Cut library/event as XML

Example:

```text
/Users/sameer/Desktop/Seattle Finished Library.fcpxml
```

## 3. Stockify

```bash
mkdir -p "$HOME/VClipRuns/seattle"
cd "$HOME/VClipRuns/seattle"

stockify \
  "/Users/sameer/Desktop/Seattle Finished Library.fcpxml" \
  --output "$PWD/review.fcpxml" \
  --db "$PWD/vclip.sqlite3" \
  --sidecar-root "/Volumes/FireCuda" \
  --sidecar-root "/Volumes/Samsung990"
```

Outputs:

```text
review.fcpxml
review-report.json
review-export-manifest.json
vclip.sqlite3
```

## 4. Human review

Import `review.fcpxml` into Final Cut. Trim, extend, delete, or tweak clips normally. For the simplest workflow, use the compilation for quick browsing but make final trims in the individual projects you will batch-export.

## 5. Export the reviewed XML

Export the reviewed library/event as:

```text
reviewed.fcpxml
```

## 6. Reconcile

```bash
reconcile reviewed.fcpxml \
  --db "$PWD/vclip.sqlite3"
```

Individual projects are authoritative by default: deleted projects reject, survivors approve, trims record final source ranges. Stock Compilation is informational.

Nothing is imported back into Final Cut.

## 7. Batch-export one MP4/MOV per approved candidate

Keep the generated individual-project names as export filenames.

Example folder:

```text
/Users/sameer/Desktop/VClip Exports
```

## 8. Package

Dry run:

```bash
vclip package \
  "/Users/sameer/Desktop/VClip Exports" \
  --output "$PWD/packages" \
  --db "$PWD/vclip.sqlite3" \
  --dry-run
```

Real run:

```bash
vclip package \
  "/Users/sameer/Desktop/VClip Exports" \
  --output "$PWD/packages" \
  --db "$PWD/vclip.sqlite3"
```

Historical weather is fetched by default (Open-Meteo). Use `--weather none` to skip.

## 9. Inspect DB state

```bash
vclip db status --db "$PWD/vclip.sqlite3"
```
