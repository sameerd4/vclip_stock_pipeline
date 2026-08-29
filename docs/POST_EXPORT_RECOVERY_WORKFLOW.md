# VClip Post-Export Recovery Workflow

VClip's post-export workflow turns rendered Final Cut projects into durable,
searchable canonical stock footage.

A clip is not canonical merely because Final Cut successfully exported a file.

## High-level flow

Final Cut export
→ physical verification
→ location truth
→ rendered-master catalog ingest
→ canonical master materialization
→ browse-view materialization
→ canonical catalog
→ visual enrichment and search metadata

## 1. Render with deterministic export plans

Export plans define exactly which VClip IDs should be rendered.

The export worker drives Final Cut, waits for the expected outputs, validates the
media, and writes receipts. The supervisor can resume completed work.

Because Final Cut's Share UI can occasionally wedge, the watchdog runs the
underlying worker in a fresh process and retries a batch when the accessibility
or Share phase stalls.

The compact export-plan builder can combine compatible projects from multiple
source FCPXML shards into fewer batches. This reduces the number of Final Cut
imports and Share operations without changing VClip identity.

## 2. Physically verify rendered masters

Receipts are not considered proof that the corpus is complete.

The physical audit joins the expected active VClip IDs to the actual rendered
files and verifies that every expected file exists and probes correctly.

Only when the expected and physical sets match exactly does the tranche move
forward.

## 3. Establish location truth

Location is evidence-driven rather than inferred from project names.

The strongest source is exact GPS from DJI SRT data covering the generated
master's source-time window.

If a reconstructed master has no usable direct SRT GPS, VClip falls back to
historical parent clips used to construct it. Parent locations must agree before
they are used as fallback truth.

When a direct-GPS master has conflicting historical parent labels, the master's
GPS can resolve which parent geography is closest.

Public browse geography stays deliberately less precise than private GPS truth.

## 4. Ingest rendered masters into SQLite

The audited render is registered in the reconstructed VClip pool.

The database records:

- stable VClip ID
- actual export plan and batch
- rendered path and filename
- duration, dimensions, frame rate, and codec
- SHA-256
- receipt provenance
- reconstruction lineage
- location truth
- capture-time metadata

This is the boundary where a Final Cut export becomes a durable VClip asset.

## 5. Materialize immutable canonical masters

Each approved clip has an immutable identity:

    VCLIP_<id>

The canonical video filename is:

    VCLIP_<id>.mp4

Canonical masters are sharded by the first two hexadecimal characters of the
VClip ID:

    masters/<shard>/VCLIP_<id>.mp4

When the Final Cut render and canonical library are on the same filesystem,
VClip uses hardlinks rather than copying the media.

The audited render and canonical master are therefore separate filesystem names
for the same physical video payload.

## 6. Build human-friendly views

Canonical identity is separate from browse organization.

For every master, VClip creates two additional hardlinks:

    views/by-location/<country>/<region>/<city>/<area>/<browse filename>

and:

    views/by-shoot/<shoot id>/<browse filename>

Browse filenames contain human context such as city, area, date, daypart, and a
short VClip ID.

These views are derived and can be regenerated. They are not the source of
truth.

## 7. Maintain one canonical catalog

The canonical catalog joins media identity, provenance, geography, time, and
browse paths.

It records the immutable master path independently from the location and shoot
views.

Geography can be corrected later without changing the VClip ID or canonical
master.

## 8. Enrich canonical masters visually

After physical identity and deterministic metadata are stable, representative
frames can be sampled from each canonical master.

Visual enrichment adds customer-facing metadata such as:

- factual captions
- controlled visual taxonomy tags
- named-subject suggestions
- searchable text

Enrichment identity should be based on VClip ID plus canonical-master SHA-256,
rather than temporary Stockify or Final Cut identities.

## Design principle

The pipeline deliberately separates evidence from presentation:

- render receipts are not physical-file proof
- project names are not location truth
- GPS coordinates are not automatically public browse labels
- browse filenames are not immutable identity
- friendly views are not independent video copies
- model-generated tags are enrichment, not source provenance

Every major transition has a fail-closed audit before the next layer is
materialized.
