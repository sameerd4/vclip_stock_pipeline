# VClip Stock Pipeline

Turn finished Final Cut Pro libraries into organized, human-reviewed, metadata-rich stock-footage packages.

The project has four core workflow commands, plus helpers:

```text
Original Final Cut library
        ↓ Export XML
     STOCKIFY
        ↓
SQLite catalog + review FCPXML
        ↓ Import into Final Cut
Human review: trim, extend, delete, tweak
        ↓ Export reviewed XML
    RECONCILE
        ↓
Updated SQLite catalog
        ↓ Batch-export one video per approved candidate
     PACKAGE
        ↓
Finished package folders + public/internal metadata
```

Stockify proposes candidates. Final Cut remains the human quality-control workspace. Reconcile records what actually survived review. Package connects the final MP4/MOV exports to the database and turns them into coherent products.

## What is implemented

- Reads an original `.fcpxml` file or `.fcpxmld` bundle.
- Preserves the existing conservative clip-eligibility behavior.
- Resolves timeline clips back to their original media assets.
- Matches DJI MP4 files to same-stem SRT files.
- Checks for an SRT beside the media first, then scans explicitly supplied archive roots.
- Parses SRT timing, GPS, altitude, orientation availability, and capture timestamps.
- Uses the first accepted clip in each source project as that project's location/date anchor.
- Rebuilds Final Cut events around inferred shoot sessions rather than copying arbitrary old event organization.
- Replaces old music/vibe project names with concise location/time labels.
- Writes one compilation project per original project and one isolated Final Cut project per accepted clip by default.
- Persists accepted and rejected candidates, provenance, telemetry summaries, proposed trims, generated names, and output mappings in SQLite.
- Reconciles manual trims, extensions, deletions, and treatment changes from a second reviewed XML export.
- Matches exported videos back to durable `stock_clip_id` values.
- Enriches packages with historical weather by default (Open-Meteo; opt out with `--weather none`).
- Adds structured astronomical context (sunrise/sunset, solar_period, ranking signals) without treating sunrise proximity as a customer search tag.
- Writes public-safe metadata separately from internal provenance and exact location data.
- Recovers Unknown Location sessions from SRT GPS consensus and can rewrite review XML names from SQLite without full re-extraction.

## Generated Final Cut structure

An original event containing unrelated projects such as:

```text
Library
└── Event: Old Random Event
    ├── Seattle December 9th Remastered
    ├── May 2nd Evening Remastered
    ├── Hot Gunna Thug
    └── Hot Gunna Thug 1
```

can become:

```text
Library: VClip Stock Review

├── Event: Capitol Hill, Seattle — 2025-12-09
│   ├── Capitol Hill Afternoon — Stock Compilation
│   ├── Capitol Hill Afternoon — Clip 01
│   └── Capitol Hill Afternoon — Clip 02
│
├── Event: Downtown Seattle — 2026-05-02
│   ├── Downtown Seattle Evening — Stock Compilation
│   └── Downtown Seattle Evening — Clip 01
│
└── Event: South Lake Union, Seattle — 2026-05-09
    ├── South Lake Union Evening — Natural — Stock Compilation
    ├── South Lake Union Evening — Natural — Clip 01
    ├── South Lake Union Evening — Natural — Clip 02
    ├── South Lake Union Evening — Graded — Stock Compilation
    ├── South Lake Union Evening — Graded — Clip 01
    └── South Lake Union Evening — Graded — Clip 02
```

`Hot Gunna Thug` and `Hot Gunna Thug 1` remain preserved as source provenance in SQLite, but they no longer control customer-facing names. The treatment suffix is added only when multiple source projects inside the same session would otherwise have the same generated label.

## Requirements

- macOS, Linux, or another environment with Python 3.11 or newer.
- Final Cut Pro for the review/export portions of the workflow.
- The original media drives mounted while Stockify runs.
- DJI `.SRT` files beside their matching `.MP4` files, or under directories passed with `--sidecar-root`.
- `ffmpeg`/`ffprobe` only for optional visual scoring and media inspection.
- Internet access for default Open-Meteo weather enrichment during Package (disable with `--weather none`), and optionally for Nominatim location lookup during Stockify.

The core pipeline has no required third-party Python runtime dependencies.

# Installation

From the project directory:

```bash
make dev
source .venv/bin/activate
vclip --help
```

Equivalent manual setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e ".[dev,visual]"
```

The `visual` extra installs NumPy for optional frame-motion scoring. The stock pipeline itself can be installed without it:

```bash
python -m pip install -e .
```

A prebuilt wheel can be installed without a source build:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install /path/to/vclip_stock_pipeline-0.1.0-py3-none-any.whl
```

Install NumPy separately when using `--visual-score` with the wheel:

```bash
python -m pip install numpy
```

After installation, all of these forms work:

```bash
vclip stockify --help
vclip reconcile --help
vclip package --help

stockify --help
reconcile --help
vclip-package --help
```

The repository also includes convenience wrappers:

```bash
./bin/stockify --help
./bin/reconcile --help
./bin/package --help
```

# End-to-end terminal workflow

The examples below keep every generated artifact in one working directory:

```bash
mkdir -p "$HOME/VClipRuns/seattle-archive"
cd "$HOME/VClipRuns/seattle-archive"
```

Assume the original Final Cut XML is:

```text
/Users/sameer/Desktop/Seattle Finished Library.fcpxml
```

and archive drives are mounted at:

```text
/Volumes/FireCuda
/Volumes/Samsung990
```

## Step 1: Export the original Final Cut XML

In Final Cut Pro, export the finished library, event, or selected projects as XML. Extended metadata and the newest available XML version are preferred.

Do not manually reorganize the old library first. Old event and project names are treated as provenance, not as the desired stock organization.

## Step 2: Run Stockify

```bash
stockify \
  "/Users/sameer/Desktop/Seattle Finished Library.fcpxml" \
  --output "$PWD/seattle-stock-review.fcpxml" \
  --db "$PWD/vclip.sqlite3" \
  --report "$PWD/stockify-report.json" \
  --manifest "$PWD/export-manifest.json" \
  --sidecar-root "/Volumes/FireCuda" \
  --sidecar-root "/Volumes/Samsung990"
```

Default behavior:

- Creates a new SQLite database if one does not exist.
- Creates a new immutable Stockify run inside an existing database.
- Checks for same-stem SRT files beside each source media file.
- Recursively scans each `--sidecar-root` for candidate SRT stems not already resolved.
- Creates one compilation and one isolated project per accepted clip.
- Groups generated projects into location/date events.
- Saves rejected candidates and their reasons in the database too.

Useful optional flags:

```bash
--recover-short-clips
--require-srt-for-expansion
--visual-score
--require-visual-for-expansion
--require-camera-lut
--require-custom-lut
--project-name "Exact Source Project Name"
--max-segments-per-project 5
```

A practical first test run against one project is:

```bash
stockify \
  "/Users/sameer/Desktop/Seattle Finished Library.fcpxml" \
  --output "$PWD/test-review.fcpxml" \
  --db "$PWD/vclip.sqlite3" \
  --sidecar-root "/Volumes/FireCuda" \
  --project-name "Hot Gunna Thug 1" \
  --max-segments-per-project 5
```

### SRT discovery rule

For a source file such as:

```text
DJI_20260509190321_0147_D.MP4
```

Stockify searches for:

```text
DJI_20260509190321_0147_D.SRT
```

Matching is based on the normalized filename stem. A sibling SRT next to the MP4 wins over a same-name SRT found elsewhere. Duplicate archive matches are recorded as ambiguous rather than hidden.

### Location coverage

The bundled offline place catalog currently contains the locations in `src/vclip_pipeline/data/places.json`. Add your own locations by copying that file, editing it, and passing:

```bash
--places-file "/path/to/my-places.json"
```

For locations absent from the local catalog, optional cached reverse geocoding can be enabled:

```bash
--location-provider catalog+nominatim \
--nominatim-user-agent "VClip/0.1 your-email@example.com"
```

Use an identifying user agent and keep the offline catalog as the first resolver. Nominatim responses are cached in SQLite.

## Step 3: Import and review in Final Cut

Import:

```text
seattle-stock-review.fcpxml
```

into a new or test Final Cut library.

The generated clips are proposals. You are explicitly allowed to:

- shorten a clip;
- lengthen a clip within available source handles;
- move its in or out point;
- delete a clip;
- adjust preserved video treatment;
- leave a clip unchanged.

A candidate's identity does not depend on its proposed trim. Manual changes therefore do not create a new candidate or break the catalog.

The normal review model treats **individual clip projects as authoritative**. Stock Compilation is for fast browsing/reference only. **Make the final trim in the individual project you plan to batch-export.** Final Cut does not propagate a trim from the compilation into the individual project (or the reverse).

```text
compilation = fast browsing/reference (informational)
individual projects = final trims + deletions + batch export
```

If you prefer a compilation-only review/export workflow, run Reconcile with `--authority compilation` and export segmented clips from that reviewed compilation. Do not trim only the compilation under the default individual-authoritative model and then export unchanged individual projects; Package will detect the material duration mismatch.

## Step 4: Export the reviewed XML

After the human review is complete, export the reviewed Final Cut library/event back to XML once:

```text
seattle-stock-reviewed.fcpxml
```

This second XML export is not imported anywhere. It is a machine-readable record of the final human decisions.

## Step 5: Run Reconcile

```bash
reconcile \
  "$PWD/seattle-stock-reviewed.fcpxml" \
  --db "$PWD/vclip.sqlite3" \
  --report "$PWD/reconcile-report.json"
```

Reconcile reads the embedded `stock_clip_id` metadata and updates the same database:

- surviving individual project, unchanged → approved, unchanged;
- surviving individual project, changed start/duration → approved, manually modified;
- surviving individual project, changed video treatment → approved, manually modified;
- missing/deleted individual project → human-rejected;
- Stock Compilation edits alone do not approve, reject, or conflict.

No new XML is produced, and nothing needs to be imported back into Final Cut.

### Review authority

The default is:

```bash
--authority auto
```

In auto mode (normal individual-project workflow):

- individual projects are authoritative whenever Stockify generated them;
- deleting an individual project rejects that candidate;
- trimming an individual project records the final source range;
- Stock Compilation is informational and cannot conflict with an individual decision;
- compilation-only Stockify layouts fall back to compilation authority.

To force one representation:

```bash
--authority individual
```

or:

```bash
--authority compilation
```

### Reconcile scope

The safe default is:

```bash
--scope observed-projects
```

Use it when the reviewed XML contains only selected events or projects. Unseen projects remain untouched.

Use:

```bash
--scope full-run
```

only when the reviewed XML definitely contains the complete Stockify review library. In that mode, missing candidates are considered reviewed deletions.

## Step 6: Batch-export the approved clips

Export one physical video file per approved candidate.

The easiest workflow is to batch-share the individual Final Cut projects. Their project names become the exported basenames:

```text
South Lake Union Evening — Natural — Clip 01.mp4
South Lake Union Evening — Natural — Clip 02.mp4
South Lake Union Evening — Graded — Clip 01.mp4
```

You may also use a segmented compilation-export workflow. `export-manifest.json` contains the project timecode and suggested CommandPost-style filename information used by Package as a fallback matcher.

Package expects one output video per candidate. A single long compilation movie is not automatically cut into separate deliverables.

Place the exports in a folder, for example:

```text
/Users/sameer/Desktop/VClip Exports
```

## Step 7: Dry-run Package first

```bash
vclip package \
  "/Users/sameer/Desktop/VClip Exports" \
  --output "$PWD/packages" \
  --db "$PWD/vclip.sqlite3" \
  --dry-run
```

A dry run performs matching and validation without copying files or changing package records.

## Step 8: Build the packages

```bash
vclip package \
  "/Users/sameer/Desktop/VClip Exports" \
  --output "$PWD/packages" \
  --db "$PWD/vclip.sqlite3" \
  --mode copy \
  --report "$PWD/package-report.json"
```

Available transfer modes:

```text
copy      safest default; keeps the export folder untouched
move      moves exports into packages
hardlink  no duplicate file data, but source/output must share a filesystem
symlink   package points to the original exports
```

By default Package:

- uses the newest reconciled Stockify run when `--run-id` is omitted;
- matches exact generated project filenames first;
- can also match an embedded stock ID or the reconciled compilation prefix/timecode pattern;
- checks for duplicate, ambiguous, missing, and unrelated exports;
- runs `ffprobe` when available;
- blocks exports whose durations materially disagree with the reviewed database state;
- calculates SHA-256 checksums;
- writes one package per generated source-project label.

After manually verifying an intentional mismatch, it can be overridden explicitly:

```bash
--allow-duration-mismatch
```

To package one project family only:

```bash
vclip package \
  "/Users/sameer/Desktop/VClip Exports" \
  --output "$PWD/packages" \
  --db "$PWD/vclip.sqlite3" \
  --project-label "South Lake Union Evening — Natural"
```

For deliberately incomplete folders:

```bash
--allow-missing
```

The resulting manifest is marked `partial`. Unrelated videos can be ignored explicitly with:

```bash
--allow-unmatched
```

## Step 9: Historical weather enrichment

Weather enrichment is part of the normal Package flow. By default Package queries Open-Meteo's historical archive API with the shoot session GPS and capture timestamp, selects the **nearest** hourly observation (so `07:56` matches `08:00`, not `07:00`), and caches the result in SQLite.

Persisted fields include provider, requested capture time, matched weather time, timezone, temperature, precipitation, rain, cloud cover, visibility, wind speed, WMO weather code, normalized condition label (`clear`, `mainly_clear`, `partly_cloudy`, `overcast`, `fog`, `rain`, `snow`, …), and the API weather-grid latitude/longitude (kept separate from the drone/source GPS).

Package search tags derive weather terms from that normalized condition. API failures do not fail packaging unless you opt in:

```bash
--require-weather
```

To skip weather entirely:

```bash
--weather none
```

## Step 10: Astronomical context

For each packaged session with coordinates, capture time, and timezone, Package also computes local sunrise/sunset and persists structured astronomy metadata:

- `sunrise_time` / `sunset_time`
- `minutes_from_sunrise` / `minutes_from_sunset`
- `solar_period` (`pre_dawn`, `sunrise_window`, `morning`, `day`, `sunset_window`, `dusk`, `night`)

This is **factual / ranking metadata**, not an automatic customer search tag. Being in `sunrise_window` does not add a `sunrise` tag — fog, heavy overcast, or low visibility can make sunrise-timed footage look nothing like a sunrise. Weather reduces `concept_signals.*.search_confidence` while leaving the solar geometry intact. A `visual_analysis` slot is reserved for future frame-level detection of sunrise, sunset, golden light, fog, and similar concepts.

# Package output

A finished package looks like:

```text
south-lake-union-evening-natural/
├── clips/
│   ├── South Lake Union Evening — Natural — Clip 01.mp4
│   └── South Lake Union Evening — Natural — Clip 02.mp4
├── metadata/
│   └── clips/
│       ├── South Lake Union Evening — Natural — Clip 01.json
│       └── South Lake Union Evening — Natural — Clip 02.json
├── metadata.json
├── manifest.json
└── vclip-internal.json
```

`metadata.json` is public-safe package metadata and does not expose exact GPS coordinates.

`manifest.json` records packaged filenames, sizes, checksums, matching confidence, and whether the package is ready or partial.

`vclip-internal.json` and the per-clip internal sidecars retain source paths, exact location data, original project provenance, proposed/final trims, SRT status, LUT/effect information, and manual-review state.

# Database behavior

The default database is `vclip.sqlite3` beside the generated review XML. Always pass the same `--db` path to all three commands.

Each Stockify execution creates a new run snapshot. It does not overwrite an earlier run. SQLite migrations run automatically.

Inspect the catalog at any point:

```bash
vclip db status --db "$PWD/vclip.sqlite3"
```

Show Final Cut libraries Stockify has successfully processed:

```bash
vclip libraries --db "$PWD/vclip.sqlite3"
```

Compare against `.fcpbundle` libraries on a drive:

```bash
vclip libraries --db "$PWD/vclip.sqlite3" --scan /Volumes/Archive
```

```text
✓ February 2026.fcpbundle
✓ December 2023.fcpbundle
○ Berkeley.fcpbundle
○ Santa Cruz.fcpbundle

Remaining: 2
```

Also check whether exported XML exists for each scanned library:

```bash
vclip libraries \
  --db "$PWD/vclip.sqlite3" \
  --scan /Volumes \
  --xml-dir "$HOME/Desktop/vclip-work/work"
```

```text
✓ February 2026.fcpbundle      XML found
○ Client Work.fcpbundle        XML missing

Libraries: 2
XML found: 1
XML missing: 1
```

XML found/missing is separate from processed/unprocessed. A successful Stockify run is what marks a library processed (from the source XML `library location` or a `.fcpbundle` input path). Re-runs stay idempotent and keep first/last Stockify run provenance.

Recover Unknown Location sessions from SRT GPS already in the catalog (or re-readable sidecars), without rerunning Stockify extraction. With `--db`, recovery is catalog-wide across every complete Stockify run; pass `--run-id STOCKIFY_...` to limit to one run:

```bash
vclip recover-locations \
  --db "$PWD/vclip.sqlite3" \
  --rewrite-review-xml \
  --report "$PWD/location-recovery-report.json"
```

```text
Stockify runs scanned:       25
Unknown sessions before:     40
Resolved by SRT consensus:   31
Still unknown:               9
Clips recovered:             1000
Review XMLs rewritten:       18
```

Recovery prefers existing clip SRT GPS, then same-asset siblings within that run, and only uses project/event names or nearby same-shoot sessions in the same run as corroboration. Sessions are never merged across runs. Session-level consensus is written to SQLite with evidence, confidence, and contributing clip IDs. `--rewrite-review-xml` renames events/projects in every affected review FCPXML from that catalog data.

Explain remaining Unknown Location sessions without changing the catalog (checks SQLite plus currently mounted `/Volumes`):

```bash
vclip diagnose-locations --db "$PWD/vclip.sqlite3"
```

```text
LOCATION DIAGNOSTICS

Unknown sessions: 100
Clips affected:   871

WHY THEY ARE UNKNOWN

Missing original media / sidecar       43 sessions   390 clips
Original media found, SRT missing      24 sessions   201 clips
...
```

Use `--verbose` for per-session filenames. This command never assigns locations, rewrites XML, or modifies SQLite.

The database preserves:

- source XML run provenance and hashes;
- processed Final Cut library names/paths;
- old Final Cut event/project names;
- inferred shoot sessions;
- source-media and SRT matching details;
- accepted and rejected candidates;
- proposed and final reviewed timing;
- generated Final Cut names and occurrences;
- reconciliation decisions and conflicts;
- weather observations;
- physical export matches;
- package records.

See [DATABASE.md](DATABASE.md) for the complete entity map.

# Docker

Build the image:

```bash
docker build -t vclip-stock-pipeline .
```

For Stockify, mount your work directory read/write and mount media drives at the same absolute paths used by the FCPXML. Preserving `/Volumes/...` paths allows sibling MP4/SRT discovery to work normally:

```bash
docker run --rm -it \
  -v "$PWD:/work" \
  -v "/Volumes/FireCuda:/Volumes/FireCuda:ro" \
  -v "/Volumes/Samsung990:/Volumes/Samsung990:ro" \
  vclip-stock-pipeline \
  stockify "/work/Seattle Finished Library.fcpxml" \
  --output /work/seattle-stock-review.fcpxml \
  --db /work/vclip.sqlite3 \
  --report /work/stockify-report.json \
  --manifest /work/export-manifest.json \
  --sidecar-root /Volumes/FireCuda \
  --sidecar-root /Volumes/Samsung990
```

Reconcile:

```bash
docker run --rm -it \
  -v "$PWD:/work" \
  vclip-stock-pipeline \
  reconcile /work/seattle-stock-reviewed.fcpxml \
  --db /work/vclip.sqlite3
```

Package:

```bash
docker run --rm -it \
  -v "$PWD:/work" \
  -v "/Users/sameer/Desktop/VClip Exports:/exports:ro" \
  vclip-stock-pipeline \
  package /exports \
  --output /work/packages \
  --db /work/vclip.sqlite3
```

Docker Desktop must be allowed to share the mounted host directories.

# Validation and tests

Run the test suite:

```bash
make test
```

Run static checks:

```bash
make lint
```

Validate an FCPXML without generating output:

```bash
stockify input.fcpxml --validate-only
```

Inspect malformed/missing media resources:

```bash
stockify input.fcpxml --inspect-assets
```

# Operational notes

- Run Stockify while the relevant archive drives are mounted.
- Keep the database, reports, manifests, original XML, generated XML, and reviewed XML together for each archival batch.
- Import generated FCPXML into a test/new Final Cut library first.
- Do not rename generated individual projects before exporting unless you plan to embed the stock ID or use the manifest/timecode filename route.
- Re-export reviewed XML before batch export so the database reflects trims and deletions exactly.
- Review and physically export from the same Final Cut representation. The default individual-project authority matches batch-export of individual projects; use `--authority compilation` only for segmented compilation exports.
- Back up `vclip.sqlite3`; it is the durable catalog. The generated XML is a Final Cut view over that catalog, not the sole source of truth.
- When both compilation and individual representations are edited differently, resolve the Reconcile conflict deliberately rather than guessing.

See [ARCHITECTURE.md](ARCHITECTURE.md) for service boundaries and [QUICKSTART.md](QUICKSTART.md) for the shortest possible command sequence.
