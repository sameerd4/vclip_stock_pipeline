# Architecture

## Core rule

SQLite is the durable source of truth. FCPXML is an import/review/export representation for Final Cut Pro. Physical exported videos become package assets only after they are matched back to database candidates.

## Service boundaries

### Stockify

Inputs:

- original FCPXML;
- original media paths referenced by FCPXML;
- DJI SRT files beside media or under scan roots;
- optional local/online location resolution;
- optional visual-motion scoring.

Responsibilities:

1. Parse the original Final Cut hierarchy.
2. Resolve primary-storyline video segments to source assets.
3. Persist accepted and rejected eligibility decisions.
4. Match and parse SRT telemetry.
5. Infer project anchor location/date/time from the first accepted clip.
6. Group source projects into shoot sessions.
7. Generate concise event and project names.
8. Build one compilation plus isolated clip projects.
9. Embed stable candidate IDs in generated clips.
10. Persist the complete run snapshot atomically.

Outputs:

- review FCPXML;
- SQLite rows;
- JSON analysis report;
- export/package manifest.

### Reconcile

Inputs:

- reviewed FCPXML exported after human Final Cut changes;
- SQLite catalog.

Responsibilities:

1. Read embedded candidate IDs from reviewed clips.
2. Determine which generated projects were actually present in the XML scope.
3. Compare proposed and reviewed source start, duration, and video-treatment signatures.
4. Record approval, manual modification, deletion, or conflict.
5. Preserve both the algorithm proposal and the final human decision.

Outputs:

- updated SQLite state;
- JSON reconciliation report.

Reconcile deliberately does not generate an XML file.

### Package

Inputs:

- directory of individually exported MP4/MOV/M4V files;
- reconciled SQLite catalog;
- historical-weather provider (Open-Meteo archive by default; `--weather none` to opt out).

Responsibilities:

1. Match physical exports to approved candidate IDs.
2. Reject ambiguous duplicates by default.
3. Detect missing and unrelated files.
4. Probe media and calculate checksums when enabled.
5. Fetch/cache historical weather (nearest hourly Open-Meteo observation; non-fatal unless `--require-weather`).
6. Compute astronomical context (local sunrise/sunset, solar_period, weather-adjusted concept ranking signals).
7. Build package folders.
8. Separate public-safe metadata from internal provenance.
9. Persist export and package records.

Outputs:

- package directories;
- public metadata;
- internal metadata;
- package manifests;
- updated SQLite state.

## Identity model

`stock_clip_id` represents the candidate's origin in the source edit. It is intentionally independent of the proposed trim.

The durable origin is based on stable source facts such as:

- source event/project identity;
- source project index/UID;
- timeline segment index;
- referenced source asset;
- original source name.

The following are mutable state, not identity:

- proposed start/duration;
- human-reviewed start/duration;
- final video treatment;
- approval/rejection state;
- exported path;
- package membership.

This lets a human shorten or extend a clip without creating a new logical candidate.

## Hierarchy model

### Original hierarchy

```text
Library
└── Event
    └── Project
        └── Timeline clips
```

Original event/project names are preserved as provenance.

### Generated hierarchy

```text
Review Library
└── Shoot-session Event: location + date
    ├── Source-project Compilation: generated descriptive label
    ├── Candidate Clip 01
    ├── Candidate Clip 02
    └── ...
```

A generated event groups projects that share inferred location/date and fall within the configured session time gap. A generated project family remains tied to one original source project so alternate edits or grades are not silently merged.

## First-clip project inference

For efficiency, the first accepted primary-storyline video candidate is the project anchor. Its SRT provides the project's initial location/date/time classification.

Every accepted candidate still stores its own location/capture metadata. This permits later outlier detection without requiring project organization to wait on exhaustive cross-clip inference.

## Reconcile authority model

Each accepted candidate may appear twice in generated XML:

- once in the source-project compilation;
- once in its isolated clip project.

Both occurrences carry the same `stock_clip_id`.

The normal review model is **individual-project authoritative**. Stockify creates one individual Final Cut project per candidate. During manual review, deleting that project rejects the clip; leaving it unchanged approves it; trimming its source range/duration marks it approved/modified.

Stock Compilation is informational convenience output. Compilation leftovers or compilation-only edits must not create conflicts against an individual-project decision.

Auto reconciliation rules (`--authority auto`, the default):

1. When Stockify generated an individual project for the candidate, that individual occurrence is authoritative.
2. Missing/deleted individual project → rejected.
3. Surviving individual with unchanged source range/duration → approved.
4. Surviving individual with changed source range/duration → approved, manually modified.
5. Compilation presence, absence, or edits are ignored for the decision.
6. Compilation-only Stockify layouts (no generated individual projects) fall back to compilation authority.

`--authority individual` and `--authority compilation` force a single representation when needed.


## Review/export representation invariant

The compilation and individual project are two Final Cut representations of one candidate, not linked editable instances. Final Cut does not propagate a trim made in one representation into the other.

Therefore the representation selected as reconciliation authority must also be the representation used for the physical export:

```text
individual authority → batch-export individual projects
compilation authority → segmented export from the reviewed compilation
```

Package compares probed export duration with the reviewed duration and blocks material mismatches by default. This prevents a compilation-only trim from being reconciled while an old individual-project version is accidentally packaged.

## Privacy boundary

Exact GPS and source filesystem paths are internal metadata.

Public package metadata contains only resolved public location labels such as neighborhood, city, state, and country. Exact coordinates remain in SQLite and `vclip-internal.json`.

## Module map

```text
src/vclip_pipeline/
├── cli.py                  command-line composition
├── geo.py                  offline/cached location resolution
├── db/                     schema, connections, repository
├── stockify/               source analysis and review XML generation
├── reconcile/              reviewed XML comparison
└── packaging/              export matching, weather, astronomy, package creation
```

The services depend on repository and provider interfaces rather than embedding SQL or network calls throughout the pipeline.
