# VClip post-Stockify workflow

This layer finishes the migration from legacy Final Cut libraries without making
Stockify responsible for search, visual semantics, or merchandising.

```text
Stockify review XML
  -> review-shard
  -> Final Cut review
  -> existing reconcile
  -> 4K batch export
  -> exports-ingest
  -> enrich
  -> catalog search
  -> collections suggest/publish
  -> collections materialize
```

`vclip.sqlite3` remains the canonical catalog. The new workflow tables are
created additively by `vclip-workflow`; existing Stockify/Reconcile/Package
schema and commands are unchanged.

## 1. Split a large review XML without re-running Stockify

```bash
vclip-workflow review-shard \
  /work/stockified-review-xml/november-2025-review.fcpxml \
  --db /work/vclip.sqlite3 \
  --output /work/review-shards/november-2025 \
  --group-by market \
  --max-projects 125 \
  --max-megabytes 8
```

The default output contains:

- individual clip projects, which remain authoritative for review/export;
- one tiny `Stock Compilation` scope marker per source project;
- only the transitive resource closure required by those projects;
- one manifest per shard and an index manifest.

The scope marker is a one-frame gap, not duplicate footage. Keep it until the
reviewed shard XML is exported. It lets `reconcile --scope observed-projects`
record rejection even when every individual clip from a source project was
deleted.

A source project is atomic during sharding. It is not split across shard files,
even if it contains more than the target project count. This keeps partial
reconciliation unambiguous.

Import each shard into Final Cut, delete or trim individual projects, then
export the reviewed event/library XML and reconcile it normally:

```bash
vclip reconcile reviewed-november-seattle-01.fcpxml \
  --db /work/vclip.sqlite3 \
  --run-id STOCKIFY_... \
  --authority auto \
  --scope observed-projects
```

## 2. Batch export the approved individual projects

Use the same individual-project representation that Reconcile treated as
authoritative. Preserve the generated project name as the output filename.

## 3. Ingest final exports before packaging

```bash
vclip-workflow exports-ingest /work/final-exports/november \
  --db /work/vclip.sqlite3 \
  --run-id STOCKIFY_... \
  --allow-missing \
  --report /work/reports/november-export-ingest.json
```

This command performs the export-matching/checksum/media-probe half of the old
Package flow, but does not create package folders. Export rows become the stable
boundary for visual analysis and collections.

## 4. Extract representative frames

Run this first to create a resumable local review queue without API cost:

```bash
vclip-workflow enrich \
  --db /work/vclip.sqlite3 \
  --cache /work/vclip-frame-cache \
  --provider frames-only \
  --limit 100 \
  --report /work/reports/frame-pass.json \
  --html /work/reports/frame-pass.html
```

Each clip gets six JPEGs at 10%, 25%, 40%, 60%, 75%, and 90% of final exported
duration. Cache identity includes the export checksum and sampler version.
There are no persistent proxy videos.

## 5. Add pixel-grounded visual semantics

```bash
export OPENAI_API_KEY='...'

vclip-workflow enrich \
  --db /work/vclip.sqlite3 \
  --cache /work/vclip-frame-cache \
  --provider openai \
  --model gpt-5-mini \
  --limit 100 \
  --report /work/reports/visual-pass.json \
  --html /work/reports/visual-pass.html
```

One request analyzes all six low-detail frames as one clip. The controlled
v1 taxonomy includes:

- scene: city/urban, residential, industrial, coastal, nature;
- subject: road, waterfront, architecture, skyline, bridge, mountain, campus;
- style: golden hour, blue hour, clear skies, cloudy, night;
- use: establishing, background, transition, detail.

Objective fields remain objective:

- orientation comes from the exported file dimensions;
- capture time comes from Final Cut/SRT metadata;
- market/location comes from the camera-position catalog;
- visual subjects/styles come from pixels;
- named landmarks remain unverified suggestions until a human confirms them.

GPS is never treated as proof of visible subject. This matters for telephoto
Air 3 footage where the drone may be in San Francisco while the camera is aimed
at Treasure Island.

## 6. Search the canonical clip catalog

```bash
vclip-workflow catalog reindex --db /work/vclip.sqlite3

vclip-workflow catalog search 'san francisco waterfront golden hour' \
  --db /work/vclip.sqlite3
```

The local search document combines public location, market membership, visual
tags, caption, orientation, and named-subject suggestions. SQLite FTS5 is used
when available, with a portable `LIKE` fallback.

## 7. Suggest and freeze a small collection

Example rule:

```json
{
  "markets": ["san-francisco"],
  "required_tags": ["waterfront"],
  "preferred_tags": ["golden_hour", "establishing"],
  "minimum_clips": 6,
  "maximum_clips": 10,
  "maximum_per_source_media": 2,
  "maximum_per_session": 4
}
```

```bash
vclip-workflow collections suggest \
  --db /work/vclip.sqlite3 \
  --title 'San Francisco Waterfront — Golden Hour' \
  --rule /work/rules/sf-waterfront-golden.json \
  --output /work/suggestions/sf-waterfront-golden.json
```

Inspect the explicit clip list, then publish it as an immutable snapshot:

```bash
vclip-workflow collections publish \
  /work/suggestions/sf-waterfront-golden.json \
  --db /work/vclip.sqlite3
```

A later model/taxonomy change cannot silently change a published version.

## 8. Materialize delivery folders

```bash
vclip-workflow collections materialize sf-waterfront-golden-hour \
  --db /work/vclip.sqlite3 \
  --output /work/packages \
  --mode hardlink
```

The materializer writes a stable manifest and metadata alongside ordered clips.
Hard links avoid duplication on the same filesystem and fall back to a copy if
the link cannot be created.

## What is intentionally not built

- no vector database;
- no Postgres migration;
- no browser application;
- no GIS viewing-frustum engine;
- no automatic landmark verification;
- no cross-run FCPXML mega-merge;
- no automatic publication of query results without a frozen clip snapshot.
