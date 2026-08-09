# SQLite Catalog

The catalog is created and migrated automatically. All JSON-like metadata is stored as JSON text behind repository methods so command services do not contain SQL.

## Run tables

### `stockify_runs`

One immutable analysis execution. Stores source/output paths, source XML hash, FCPXML version, pipeline version, command options, status, and timestamps.

### `reconcile_runs`

One reviewed-XML comparison. Stores authority, scope, reviewed XML hash, counts, conflicts, status, and report path.

## Source provenance

### `source_events`

Original Final Cut events exactly as encountered during a Stockify run.

### `source_projects`

Original project name/UID/index plus its generated session, event, package label, compilation name, format settings, and accepted/rejected counts.

### `source_media`

Original asset reference, filename/path, format, duration, camera LUT, SRT match method/confidence, SRT summary, capture timestamp, and location summary.

## Analysis model

### `shoot_sessions`

Inferred real-world session used for generated Final Cut event organization and package metadata. Stores location/date/time, public label, event name, base package label, confidence, and weather state.

### `stock_candidates`

The central lifecycle row. Accepted and rejected candidates are both persisted.

Important state groups:

```text
origin
  source project/media/segment

algorithm proposal
  original timing
  proposed timing
  eligibility/rejection
  SRT and visual scores
  location/capture/time
  LUT/effects

generated Final Cut view
  event name
  project label
  compilation name
  individual project name
  timeline offset/timecode

human review
  review status
  final timing
  final treatment signature
  reconciled compilation offset/timecode
  manual-change details

physical delivery
  export status
```

The composite primary key is `(run_id, stock_clip_id)`. A stable clip ID may recur across separate Stockify runs while each run retains its own snapshot.

### `generated_occurrences`

Records the compilation and individual FCPXML representation for each accepted candidate.

### `review_occurrences`

Records occurrences found in a reviewed XML export so reconciliation remains auditable.

## Enrichment and delivery

### `geocode_cache`

Caches optional reverse-geocoder responses by rounded coordinate key.

### `weather_observations`

Caches historical weather by shoot session and provider. Stores requested capture time, matched observation time, timezone, normalized condition label, measured fields, source GPS used for the query, and the API weather-grid latitude/longitude for provenance.

### `astronomy_observations`

Caches per-session solar context: local sunrise/sunset, minute offsets, `solar_period`, weather-adjusted concept ranking signals, and an extensible `visual_analysis` slot for future frame-level sunrise/sunset/golden-light detection. Astronomical proximity is factual metadata / ranking signal — not an automatic customer search tag.

### `processed_libraries`

Minimal record of Final Cut libraries (`.fcpbundle`) successfully processed by Stockify. Stores library name/path plus first/last Stockify run IDs and timestamps. Re-runs are idempotent.

### `exports`

Maps one final physical video path to one approved candidate, including match method, size, duration, checksum, and reconciliation timestamp.

### `packages`

One generated package per source-project lineage/run. Stores title, output path, status, clip count, and public metadata.

### `package_clips`

Ordered many-to-many link between packages and candidate exports.

## Useful inspection

Table counts:

```bash
vclip db status --db /path/to/vclip.sqlite3
```

Direct SQLite inspection remains available:

```bash
sqlite3 /path/to/vclip.sqlite3
```

Examples:

```sql
.headers on
.mode column

SELECT id, status, started_at, completed_at
FROM stockify_runs
ORDER BY started_at DESC;

SELECT generated_event_name, generated_project_label,
       accepted_clip_count, skipped_clip_count
FROM source_projects
ORDER BY generated_event_name, generated_project_label;

SELECT stock_clip_id, generated_clip_project_name,
       review_status, proposed_duration, final_duration,
       manually_modified
FROM stock_candidates
WHERE eligibility_status = 'accepted'
ORDER BY generated_event_name, generated_project_label, clip_sequence;

SELECT stock_clip_id, rejection_reason, rejection_detail
FROM stock_candidates
WHERE eligibility_status = 'rejected';
```

Do not manually update catalog rows unless you understand the reconciliation/package invariants. Use the commands for lifecycle changes and SQL primarily for inspection.
