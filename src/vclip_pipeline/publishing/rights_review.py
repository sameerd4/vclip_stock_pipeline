"""Human rights-review document for package content readiness."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import VClipError
from ..util import utc_now
from .paths import (
    load_json,
    load_package_release,
    release_directory,
    rights_review_path,
    write_json,
)

REVIEW_STATUSES = frozenset({"pending", "approved", "blocked"})
CHECK_STATUSES = frozenset({"unchecked", "none_visible", "reviewed"})
CHECK_FIELDS = (
    "people",
    "logos_trademarks",
    "artwork_property",
    "license_plates",
)


class RightsReviewService:
    """Create and reconcile ``internal/rights-review.json`` without wiping human work."""

    def prepare(
        self,
        *,
        slug: str,
        version: int,
        release_root: Path,
    ) -> dict[str, Any]:
        release_dir = release_directory(release_root, slug, version)
        package = load_package_release(release_dir)
        path = rights_review_path(release_dir)
        if path.exists():
            existing = load_json(path)
            document = reconcile_rights_review(existing, package)
        else:
            document = build_rights_review_template(package)
        write_json(path, document)
        document["path"] = str(path)
        return document


def build_rights_review_template(package: dict[str, Any]) -> dict[str, Any]:
    clips = [_default_clip_entry(clip) for clip in package.get("clips", [])]
    return {
        "document_version": 1,
        "collection_slug": package.get("collection_slug"),
        "collection_version": package.get("collection_version"),
        "package_id": package.get("package_id"),
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "clip_count": len(clips),
        "clips": clips,
    }


def reconcile_rights_review(
    existing: dict[str, Any],
    package: dict[str, Any],
) -> dict[str, Any]:
    existing_clips = existing.get("clips")
    if not isinstance(existing_clips, list):
        raise VClipError("rights-review.json clips must be a list.")

    by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    by_stock: dict[str, dict[str, Any]] = {}
    by_export: dict[str, dict[str, Any]] = {}
    for entry in existing_clips:
        if not isinstance(entry, dict):
            raise VClipError("rights-review.json contains a non-object clip entry.")
        stock_clip_id = str(entry.get("stock_clip_id") or "").strip()
        export_id = str(entry.get("export_id") or "").strip()
        if not stock_clip_id or not export_id:
            raise VClipError(
                "rights-review.json entry is missing stock_clip_id or export_id."
            )
        key = (stock_clip_id, export_id)
        if key in by_pair:
            raise VClipError(
                f"Duplicate rights-review identity for {stock_clip_id}/{export_id}."
            )
        if stock_clip_id in by_stock and by_stock[stock_clip_id]["export_id"] != export_id:
            raise VClipError(
                f"Conflicting rights-review identity for stock_clip_id {stock_clip_id}: "
                f"existing export_id {by_stock[stock_clip_id]['export_id']} vs {export_id}."
            )
        if export_id in by_export and by_export[export_id]["stock_clip_id"] != stock_clip_id:
            raise VClipError(
                f"Conflicting rights-review identity for export_id {export_id}: "
                f"existing stock_clip_id {by_export[export_id]['stock_clip_id']} "
                f"vs {stock_clip_id}."
            )
        by_pair[key] = entry
        by_stock[stock_clip_id] = entry
        by_export[export_id] = entry

    reconciled: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for package_clip in package.get("clips", []):
        stock_clip_id = str(package_clip.get("stock_clip_id") or "").strip()
        export_id = str(package_clip.get("export_id") or "").strip()
        if not stock_clip_id or not export_id:
            raise VClipError(
                "Package release clip is missing stock_clip_id or export_id "
                "required for rights review."
            )
        key = (stock_clip_id, export_id)

        if stock_clip_id in by_stock and by_stock[stock_clip_id]["export_id"] != export_id:
            raise VClipError(
                f"Rights-review identity conflict for stock_clip_id {stock_clip_id}: "
                f"document has export_id {by_stock[stock_clip_id]['export_id']}, "
                f"package release has {export_id}."
            )
        if export_id in by_export and by_export[export_id]["stock_clip_id"] != stock_clip_id:
            raise VClipError(
                f"Rights-review identity conflict for export_id {export_id}: "
                f"document has stock_clip_id {by_export[export_id]['stock_clip_id']}, "
                f"package release has {stock_clip_id}."
            )

        if key in by_pair:
            entry = dict(by_pair[key])
            entry["sort_order"] = int(package_clip["sort_order"])
            entry["customer_filename"] = package_clip.get("customer_filename")
            reconciled.append(entry)
        else:
            reconciled.append(_default_clip_entry(package_clip))
        seen_pairs.add(key)

    # Preserve orphaned human decisions that no longer appear in the package.
    for key, entry in by_pair.items():
        if key not in seen_pairs:
            reconciled.append(dict(entry))

    reconciled.sort(
        key=lambda item: (
            int(item.get("sort_order") or 10**9),
            str(item.get("stock_clip_id") or ""),
        )
    )
    return {
        "document_version": int(existing.get("document_version") or 1),
        "collection_slug": package.get("collection_slug"),
        "collection_version": package.get("collection_version"),
        "package_id": package.get("package_id"),
        "created_at": existing.get("created_at") or utc_now(),
        "updated_at": utc_now(),
        "clip_count": len(reconciled),
        "clips": reconciled,
    }


def _default_clip_entry(package_clip: dict[str, Any]) -> dict[str, Any]:
    return {
        "sort_order": int(package_clip["sort_order"]),
        "stock_clip_id": package_clip.get("stock_clip_id"),
        "export_id": package_clip.get("export_id"),
        "customer_filename": package_clip.get("customer_filename"),
        "review_status": "pending",
        "people": "unchecked",
        "logos_trademarks": "unchecked",
        "artwork_property": "unchecked",
        "license_plates": "unchecked",
        "notes": "",
        "reviewed_by": "",
        "reviewed_at": "",
    }


def rights_clip_failures(
    entry: dict[str, Any],
    *,
    label: str,
) -> list[str]:
    failures: list[str] = []
    status = entry.get("review_status")
    if status not in REVIEW_STATUSES:
        failures.append(f"{label}: invalid review_status {status!r}")
        return failures
    if status == "blocked":
        failures.append(f"{label}: review_status is blocked")
        return failures
    if status != "approved":
        failures.append(f"{label}: review_status is {status!r} (need approved)")

    for field in CHECK_FIELDS:
        value = entry.get(field)
        if value not in CHECK_STATUSES:
            failures.append(f"{label}: invalid {field} {value!r}")
        elif value == "unchecked":
            failures.append(f"{label}: {field} is still unchecked")

    if not str(entry.get("reviewed_by") or "").strip():
        failures.append(f"{label}: reviewed_by is empty")
    if not str(entry.get("reviewed_at") or "").strip():
        failures.append(f"{label}: reviewed_at is empty")
    return failures


def validate_rights_review(
    document: dict[str, Any],
    package: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    clips = document.get("clips")
    if not isinstance(clips, list):
        return ["rights-review.json clips must be a list"]

    if "clip_count" in document:
        try:
            declared = int(document["clip_count"])
        except (TypeError, ValueError):
            failures.append(
                f"rights-review.json clip_count is invalid: {document['clip_count']!r}"
            )
        else:
            if declared != len(clips):
                failures.append(
                    f"rights-review.json clip_count {declared} does not match "
                    f"clips length {len(clips)}"
                )

    by_pair = {
        (str(item.get("stock_clip_id")), str(item.get("export_id"))): item
        for item in clips
        if isinstance(item, dict)
    }
    for package_clip in package.get("clips", []):
        stock_clip_id = str(package_clip.get("stock_clip_id"))
        export_id = str(package_clip.get("export_id"))
        label = f"clip {stock_clip_id}"
        entry = by_pair.get((stock_clip_id, export_id))
        if entry is None:
            failures.append(
                f"{label}: missing rights-review entry for export_id {export_id}"
            )
            continue
        failures.extend(rights_clip_failures(entry, label=label))
    return failures
