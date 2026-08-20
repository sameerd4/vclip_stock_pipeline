# The VClip Product Model

## The core distinction

VClip turns a private footage archive into versioned, sellable stock-footage products.

The most important distinction in the system is:

> **A collection is what we are selling. A package is the exact release we deliver.**

A collection is logical and editorial. It answers:

> Which clips belong together, and what promise does this set make to the buyer?

A package is physical and operational. It answers:

> Exactly which files, metadata, license, previews, and supporting assets make up this released product?

Customers browse and buy **collections**. Behind the Buy button, VClip delivers a **package** built from a specific version of that collection.

---

## The VClip hierarchy

```text
Source Media
    ↓
Canonical Clip
    ↓
Approved Master Export
    ↓
Collection
    ↓
Collection Version
    ↓
Package Release
    ↓
Storefront Product
    ↓
Customer Download
```

Each layer has one job. Keeping those responsibilities separate is what allows VClip to scale from one package to thousands of clips without losing identity, provenance, or editorial control.

---

# 1. The canonical clip

A **clip** is the smallest stock-footage asset VClip understands.

It is not merely an MP4 filename. It is a stable media identity represented by a `stock_clip_id`.

A clip includes or points to:

* Its original source media.
* Its source Final Cut library, event, and project.
* Its reviewed start time and duration.
* Its human review status.
* Its approved master export.
* Its capture date and time.
* Its public location.
* Its private provenance and telemetry.
* Its orientation, resolution, frame rate, and codec.
* Its visual description, subjects, tags, and possible quality or rights flags.

The customer-facing filename can change without changing the clip’s identity.

For example, all of these could represent the same canonical clip:

```text
Mission Bay Evening — Clip 01.mp4
San Francisco Golden Hour — Clip 01.mp4
VCLIP_8FCEA11D7EEC56FDAAC1
```

The first may be a historical Final Cut export name. The second may be its name inside a customer package. The third is the permanent VClip identity.

A canonical clip can appear in more than one collection. A skyline shot might belong to:

* **San Francisco Golden Hour — Vertical Drone Aerials**
* **California City Skylines**
* **Vertical Establishing Shots for Social Media**
* **Bay Area Waterfronts**

The clip remains one asset. Collections provide different editorial contexts around it.

---

# 2. The approved master export

The **master export** is the approved rendered media file for a canonical clip.

This is the high-quality MP4 that has survived:

* Final Cut review.
* Human trimming and deletion decisions.
* Reconciliation.
* Color and treatment decisions.
* Export matching.
* Technical probing.
* Hashing and integrity checks.

The master export is the source for two different purposes:

```text
Approved 4K master
        │
        ├── Customer receives this media
        │
        ├── Website preview is derived from it
        │
        └── Thumbnail or poster is derived from it
```

The master should have a complete canonical technical record:

```text
export_id
stock_clip_id
path
filename
file size
duration
SHA-256
width
height
frame rate
codec
```

The bytes of the master matter. Its SHA-256 identifies the exact media used in a release.

If the rendered bytes change because the color, edit, export settings, or source footage changed, that should be treated as a new export revision rather than silently replacing a published asset.

---

# 3. The collection

## Definition

A **collection** is a curated set of clips united by a coherent editorial promise.

Examples:

* **San Francisco Golden Hour — Vertical Drone Aerials**
* **Seattle Capitol Hill at Sunset**
* **Santa Cruz Coastline — Summer Aerials**
* **Bay Area Transit and Infrastructure**
* **Vertical City Establishing Shots**

A collection is the unit a customer understands.

The customer does not need to know which Final Cut project produced a clip, which DJI source file it came from, or which drive held the original media. The collection translates a complicated archive into a simple proposition:

> Here is a coherent set of footage for a particular place, subject, format, mood, or use.

## What a collection contains

A collection definition has a stable identity:

```text
collection_id
slug
title
description
editorial theme
selection rule
status
```

The rule may describe the candidate pool:

```json
{
  "markets": ["san-francisco"],
  "required_tags": ["golden_hour"],
  "preferred_tags": [
    "city_urban",
    "coastal",
    "architecture",
    "skyline",
    "bridge",
    "waterfront"
  ],
  "orientation": "vertical"
}
```

The rule is not the final editorial authority. It helps VClip propose a set. A human can review the result, remove weak or repetitive footage, change the order, and approve the final membership.

In other words:

> **The algorithm suggests. The collection version commits.**

## What a collection is not

A collection is not:

* A directory of copied MP4s.
* A Final Cut project.
* A shooting session.
* A search result.
* Every clip recorded in one place on one day.
* A temporary database query.
* A promise that every clip has the same exact camera location.

A shooting session can produce several collections. One collection can also draw from several sessions.

A collection is defined by customer value and editorial coherence, not by how the footage happened to be organized during production.

---

# 4. The collection version

A collection has a stable identity, but its contents can evolve. That is why it needs numbered versions.

```text
San Francisco Golden Hour — Vertical Drone Aerials
    ├── Version 1
    ├── Version 2
    └── Version 3
```

A **collection version** is an immutable editorial snapshot containing:

* The exact collection ID.
* The exact version number.
* The exact `stock_clip_id`s.
* The exact approved `export_id`s.
* The exact clip order.
* The selection rule used at the time.
* The title and description at publication.
* Selection scores or rationale where useful.

VClip already implements the core of this model: collection definitions are saved separately from numbered collection versions, and each version stores the selected export IDs, order, score, and rationale. When retrieving a version, VClip joins those frozen export IDs back to the canonical export records for paths, durations, and hashes.

## When to create a new collection version

Create a new collection version when the editorial product changes:

* A clip is added or removed.
* The order materially changes.
* A weak shot is replaced.
* The collection’s scope changes.
* The title or promise changes substantially.
* A new set is curated from the same candidate pool.

Do not create a new collection version merely because:

* A missing duration was backfilled.
* A hash was calculated.
* A typo in an internal report was fixed.
* A preview was re-encoded at a different bitrate.
* A thumbnail was regenerated.

Those are package-build or metadata-completion changes, not editorial membership changes.

---

# 5. The package

## Definition

A **package** is a complete, validated release built from one specific collection version.

It is the exact artifact VClip can hand to:

* The storefront.
* Object storage.
* A fulfillment service.
* A customer after purchase.
* Another system through an API.

A package is not simply “a folder containing MP4s.” It is the complete release contract.

It says:

> These exact master files, with these exact hashes, names, descriptions, previews, license terms, and technical specifications, constitute this sellable release.

## Collections are curated; packages are compiled

The relationship is:

```text
Collection
    “What belongs together?”

Collection Version
    “Which exact clips and in what order?”

Package Release
    “What exact files and assets are being delivered?”
```

A package must always identify the collection version from which it was built.

```text
package_id
collection_id
collection_version_id
collection_version
package_revision
build_profile
created_at
status
```

## One collection version, potentially multiple package builds

A collection version may remain unchanged while its package is rebuilt.

For example:

```text
Collection v1
    ├── Package build 1: initial previews
    ├── Package build 2: corrected README
    └── Package build 3: improved preview encoding
```

The 15 selected master clips remain the same, so the collection stays at version 1. The release artifact changes, so the package gets a new build or revision.

If the actual clip membership changes, that is collection version 2.

---

# 6. The three surfaces of a package

A complete package has three related but distinct surfaces.

## A. Customer delivery payload

This is what the buyer receives.

```text
delivery/
├── clips/
│   ├── San Francisco Golden Hour Vertical — Clip 01.mp4
│   ├── San Francisco Golden Hour Vertical — Clip 02.mp4
│   └── ...
├── README.txt
├── LICENSE.txt
└── manifest.json
```

The delivery payload contains:

* The approved full-resolution masters.
* Clean customer-facing filenames.
* A plain-language README.
* The applicable license.
* A customer-safe manifest.
* Any usage guidance that belongs with the footage.

Historical names such as `Mission Bay Evening — Clip 01.mp4` should not leak into the final customer package when the actual product is called **San Francisco Golden Hour — Vertical Drone Aerials**.

The package names should communicate the product the buyer purchased, not the project structure from which the clip originated.

## B. Storefront payload

This is what the website needs to present the collection without loading the full 4K masters.

```text
storefront/
├── cover.jpg
├── thumbnails/
│   ├── 01.jpg
│   ├── 02.jpg
│   └── ...
└── previews/
    ├── 01.mp4
    ├── 02.mp4
    └── ...
```

The storefront payload includes:

* Collection cover or hero image.
* Poster image for each clip.
* Lightweight preview video for each clip.
* Public captions.
* Public tags and categories.
* Technical summary.
* Clip count and total runtime.

These assets do not replace the masters. They allow the customer to evaluate the product before buying it.

## C. Internal release record

This is retained by VClip but is not delivered to the customer.

```text
internal/
├── provenance.json
├── validation.json
├── rights-review.json
└── build.json
```

It may include:

* Canonical VClip IDs.
* Source Final Cut provenance.
* Original source-media paths.
* Source SRT paths.
* Exact internal GPS coordinates.
* Enrichment model and taxonomy versions.
* Frame-analysis evidence.
* Validation details.
* Rights-review notes.
* Build configuration and tool versions.

This separation is essential.

The public package should expose useful location labels such as:

```text
Mission Bay, San Francisco
Marina District, San Francisco
San Francisco, California
```

It should not expose private storage paths, exact source-drive locations, or unnecessarily precise internal GPS evidence.

---

# 7. The package manifest

Every package should be machine-readable.

A package manifest is the release’s canonical bill of materials.

```json
{
  "manifest_version": 1,
  "package_id": "PACKAGE_...",
  "collection_id": "COLLECTION_...",
  "collection_version_id": "COLLECTIONVERSION_...",
  "collection_version": 1,
  "package_revision": 1,
  "title": "San Francisco Golden Hour — Vertical Drone Aerials",
  "slug": "san-francisco-golden-hour-vertical",
  "status": "publish_ready",
  "clip_count": 15,
  "total_duration_seconds": 115.58,
  "formats": {
    "orientation": "vertical",
    "resolution": "2160x3840",
    "frame_rate": 60,
    "codec": "h264"
  },
  "license": {
    "license_id": "VCLIP_STANDARD",
    "version": 1
  },
  "clips": [
    {
      "sort_order": 1,
      "stock_clip_id": "VCLIP_...",
      "export_id": "EXPORT_...",
      "customer_filename": "San Francisco Golden Hour Vertical — Clip 01.mp4",
      "duration_seconds": 7.333333,
      "width": 2160,
      "height": 3840,
      "frame_rate": 60,
      "codec": "h264",
      "file_size_bytes": 47724338,
      "sha256": "...",
      "caption": "...",
      "tags": ["city_urban", "skyline", "bridge", "waterfront"],
      "thumbnail": "storefront/thumbnails/01.jpg",
      "preview": "storefront/previews/01.mp4"
    }
  ]
}
```

The manifest allows VClip to verify the release, upload it, rebuild it, expose it through an API, and confirm what a customer received.

---

# 8. The storefront product

A **product listing** is the commerce record that points to a package release.

It contains things that do not define the underlying collection:

* Price.
* Currency.
* SKU.
* Sale status.
* Product URL.
* Cover image.
* Marketing copy.
* License options.
* Checkout configuration.
* Download entitlement.
* Featured or promoted status.

This distinction matters because price can change without changing the footage.

```text
Collection:
    The editorial work.

Package:
    The released files and metadata.

Product listing:
    The commercial offer.
```

In the first VClip implementation, these may have a one-to-one relationship:

```text
one collection
→ one approved package
→ one storefront product
```

The separation still protects the architecture as VClip grows.

---

# 9. What the customer sees

The public website should primarily use the language of **collections**.

A customer-facing page might say:

> **San Francisco Golden Hour — Vertical Drone Aerials**
> A curated collection of 15 vertical 4K60 drone clips featuring San Francisco skylines, waterfronts, bridges, architecture, and coastal views.

The customer browses:

* The collection cover.
* Individual clip previews.
* Clip descriptions.
* Technical specifications.
* What the download contains.
* The license.
* The price.

At checkout or download time, the site may say:

> Your download package includes 15 full-resolution MP4 clips, a license, and a manifest.

So the recommended public terminology is:

```text
Public concept:    Collection
Marketing alias:   Footage Collection or Footage Pack
Delivery concept:  Download Package
Internal concept:  Package Release
```

We do not need to force customers to understand every internal term.

---

# 10. The San Francisco example

VClip now has a real example of this model:

```text
Collection:
San Francisco Golden Hour — Vertical Drone Aerials

Slug:
san-francisco-golden-hour-vertical

Version:
1

Membership:
15 exact approved exports
```

The collection was published with a stable collection ID and version ID, then materialized with 15 selected clips.

That means **collection v1 exists**.

It has:

* A stable identity.
* An editorial title and description.
* A frozen membership.
* An exact clip order.
* Approved master exports.
* Complete master durations and hashes.
* 4K vertical technical metadata.
* OpenAI-assisted visual captions and tags.
* Canonical provenance in SQLite.

But collection v1 is not yet a complete package release.

It still needs:

* Final customer-facing filenames.
* A complete package manifest.
* Collection and clip-level public copy.
* Rights-review decisions.
* License and README.
* Storefront thumbnails and previews.
* Cover selection.
* Package validation.
* A final `PUBLISH_READY` result.

This is the exact boundary we were trying to discover by taking one collection to completion.

---

# 11. The current meaning of `package` in the codebase

The word **package** has been overloaded in the existing implementation.

The older `vclip package` flow groups exports by their original Final Cut `source_project_id`, then builds a folder for each source project.

That was useful for proving:

* Export matching.
* Media probing.
* Checksumming.
* Metadata generation.
* Physical file transfer.
* Package-folder creation.

But a source Final Cut project is not necessarily a coherent customer product. That is why one old “Mission Bay” package contained footage spanning several parts of San Francisco.

The newer collection layer is the correct editorial foundation. It suggests clip sets from the enriched catalog, publishes stable versions, and can create a versioned materialized view.

We should therefore reserve the terms as follows:

```text
Legacy package:
A source-project-derived export bundle used during migration and development.

Collection materialization:
A working or staging view of a frozen collection version.

Final package release:
The complete, validated, customer- and storefront-ready build of a collection version.
```

The final package should be built by the publishing layer, not inferred directly from the original Final Cut project structure.

---

# 12. Package readiness

A package should only be considered sellable when it passes a deterministic readiness contract.

## Media integrity

* Every expected master exists.
* Every master matches its stored SHA-256.
* Duration is present and valid.
* Resolution, orientation, frame rate, and codec are known.
* No files are corrupt or unexpectedly truncated.
* No master has silently changed since the build was approved.

## Editorial completeness

* The collection version is published.
* Membership and order are frozen.
* No unwanted duplicates remain.
* Each clip is visually usable.
* The title accurately describes the set.
* The package delivers the promise made by the title and description.

## Metadata completeness

* Every clip has a public caption.
* Every clip has useful search tags.
* Public geography is accurate.
* Capture date and time-of-day are represented appropriately.
* Customer-facing filenames are clean.
* Total runtime and technical specifications are calculated.

## Rights readiness

* Possible people, logos, artwork, license plates, or restricted subjects are flagged.
* A human has reviewed those flags.
* Commercial versus editorial eligibility has been decided.
* The package has an explicit rights-review status.
* Internal AI output is not treated as the legal authority.

## Storefront readiness

* Collection cover exists.
* Every clip has a thumbnail.
* Every clip has a lightweight preview.
* Public copy is complete.
* Pricing and license options are configured.

## Delivery readiness

* License file exists.
* README exists.
* Customer-safe manifest exists.
* Package structure is valid.
* Final customer files have the expected names.
* The downloadable release can be rebuilt and verified.

Only when all required checks pass should VClip emit:

```text
PUBLISH_READY
```

If readiness fails, the validator should state exactly why:

```text
NOT_PUBLISH_READY

Missing:
- rights review for clip 07
- preview for clip 12
- license version
```

---

# 13. Immutability and reproducibility

A published package should never be quietly mutated.

Once a package release is live:

* Its manifest is frozen.
* Its master hashes are frozen.
* Its customer filenames are frozen.
* Its license version is recorded.
* Its collection version is recorded.
* Its derivative configuration is recorded.

Corrections produce a new package revision.

Membership changes produce a new collection version.

This gives VClip a reproducible chain:

```text
collection version
+ exact export hashes
+ publishing configuration
+ license version
= package release
```

That is the foundation for trust.

A customer can receive the same release later. VClip can verify that the files have not changed. A broken release can be traced to the exact source clip, export, build, and package manifest.

---

# 14. Storage is not identity

A package is a logical release even before VClip creates a giant duplicate directory of every master.

During development, VClip can keep:

* Canonical masters on an external SSD.
* Collection membership in SQLite.
* Lightweight previews and thumbnails in a product directory.
* Package metadata in a release manifest.

The full customer payload only needs to be physically assembled once, when preparing the actual downloadable artifact or upload.

This avoids treating duplicated storage as proof that a product exists.

The product exists because VClip has:

* A frozen collection version.
* Verified master identities.
* A complete package manifest.
* Required supporting assets.
* A passing readiness result.

Physical copying should always be explicit. A hardlink request must never silently become a full copy. A package operation should not unexpectedly consume internal SSD space merely because two paths are on different filesystems.

---

# 15. The concepts VClip introduces

VClip is introducing more than a stock-footage folder structure.

## Footage as canonical inventory

A clip is a durable identity with provenance, not a filename lost somewhere inside an editing library.

## Collections as editorial products

The valuable unit is not always one isolated clip. It can be a coherent, immediately useful set built around place, subject, format, time, mood, or use case.

## Human authority with machine assistance

Final Cut review and collection approval remain human editorial decisions. Telemetry, deterministic analysis, and OpenAI visual understanding make the archive searchable and scalable without replacing that authority.

## Packages as reproducible releases

A package is not an improvised ZIP. It is a versioned, validated build with exact media hashes, metadata, licensing, and storefront assets.

## Metadata as part of the product

Descriptions, tags, location context, technical specifications, previews, and manifests are not administrative leftovers. They are what make the footage discoverable, understandable, trustworthy, and sellable.

## The archive as source of truth

Collections and packages are generated products built from a canonical archive. They do not replace the archive, and they do not require uncontrolled duplication of it.

---

# Canonical definitions

> **Clip:** A stable, atomic stock-footage asset with canonical identity, provenance, reviewed timing, an approved master export, and metadata.

> **Master Export:** The approved high-quality rendered media file representing a canonical clip.

> **Collection:** A stable editorial product definition that groups clips around a coherent customer promise.

> **Collection Version:** An immutable snapshot of the exact clips, exports, order, rule, title, and description approved for a collection at a point in time.

> **Package:** A complete, validated release built from one collection version, containing or referencing the exact customer masters, public metadata, license, storefront assets, and machine-readable manifest.

> **Package Revision:** A new build of the same collection version created because delivery assets, documents, encoding, or other non-editorial release details changed.

> **Product Listing:** The storefront and commerce record—price, SKU, availability, marketing presentation, and purchase behavior—that points to an approved package release.

The simplest expression remains:

> **The collection is the idea and the editorial promise. The package is the exact released implementation of that promise.**
