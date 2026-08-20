"""Human-confirmed rights facts and schema-v2 reconciliation."""

from __future__ import annotations

import json
from copy import deepcopy
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
from .rights_evidence import OBSERVATION_FIELDS
from .rights_policy import (
    FACT_FIELDS,
    FACT_VALUES,
    POLICY_VERSION,
    derive_classification,
    validate_human_fields,
)

SCHEMA_VERSION = 2
OLD_CHECK_FIELDS = (
    "people",
    "logos_trademarks",
    "artwork_property",
    "license_plates",
)


class RightsReviewService:
    """Create/reconcile schema-v2 review data without replacing human facts."""

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
            existing_text = path.read_text(encoding="utf-8")
            existing = load_json(path)
            document = reconcile_rights_review(existing, package)
            rendered = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
            if rendered == existing_text:
                document["path"] = str(path)
                return document
        else:
            document = build_rights_review_template(package)
        write_json(path, document)
        document["path"] = str(path)
        return document

    def confirm(
        self,
        *,
        slug: str,
        version: int,
        release_root: Path,
        clip: int,
        reviewed_by: str,
        accept_machine_evidence: bool = False,
        recognizable_people: str | None = None,
        trademarks: str | None = None,
        copyrighted_artwork: str | None = None,
        identifiable_property: str | None = None,
        identifying_information: str | None = None,
        professional_event_content: str | None = None,
        capture_provenance: str | None = None,
        notes: str | None = None,
        human_status: str = "confirmed",
    ) -> dict[str, Any]:
        if not reviewed_by.strip():
            raise VClipError("--reviewed-by is required.")

        release_dir = release_directory(release_root, slug, version)
        review_path = rights_review_path(release_dir)
        evidence_path = release_dir / "internal" / "rights-evidence.json"
        review = load_json(review_path)
        evidence = load_json(evidence_path)
        review_clip = _clip_by_order(review, clip, "rights-review.json")
        evidence_clip = _clip_by_order(evidence, clip, "rights-evidence.json")
        _require_same_identity(review_clip, evidence_clip)

        updated = deepcopy(review_clip)
        facts = updated["facts"]
        if accept_machine_evidence:
            _accept_none_detected(facts, evidence_clip.get("observations") or {})

        overrides = {
            "recognizable_people": recognizable_people,
            "trademarks": trademarks,
            "copyrighted_artwork": copyrighted_artwork,
            "identifiable_property": identifiable_property,
            "identifying_information": identifying_information,
            "professional_event_content": professional_event_content,
        }
        for field, value in overrides.items():
            if value is None:
                continue
            if value not in FACT_VALUES[field]:
                raise VClipError(f"Invalid {field} value: {value!r}")
            facts[field] = value

        if capture_provenance is not None:
            updated["capture_provenance"]["status"] = capture_provenance
        if notes is not None:
            updated["human_review"]["notes"] = notes
        updated["human_review"]["status"] = human_status
        updated["human_review"]["reviewed_by"] = reviewed_by.strip()
        updated["human_review"]["reviewed_at"] = utc_now()

        field_failures = validate_human_fields(updated)
        if field_failures:
            raise VClipError("Invalid review values: " + "; ".join(field_failures))
        if human_status == "confirmed":
            unconfirmed = [field for field in FACT_FIELDS if facts.get(field) == "unconfirmed"]
            if unconfirmed:
                raise VClipError(
                    "Cannot confirm while facts remain unconfirmed: " + ", ".join(unconfirmed)
                )
            if updated["capture_provenance"]["status"] == "unconfirmed":
                raise VClipError("Cannot confirm while capture_provenance is unconfirmed.")

        classification = derive_classification(updated)
        updated["classification"] = {
            **classification,
            "derived_at": utc_now(),
        }

        for index, item in enumerate(review["clips"]):
            if int(item["sort_order"]) == clip:
                review["clips"][index] = updated
                break
        review["clip_count"] = len(review["clips"])
        write_json(review_path, review)
        return updated


def build_rights_review_template(package: dict[str, Any]) -> dict[str, Any]:
    clips = [_default_clip_entry(clip) for clip in package.get("clips", [])]
    return {
        "schema_version": SCHEMA_VERSION,
        "collection_slug": package.get("collection_slug"),
        "collection_version": package.get("collection_version"),
        "clip_count": len(clips),
        "clips": clips,
    }


def reconcile_rights_review(
    existing: dict[str, Any],
    package: dict[str, Any],
) -> dict[str, Any]:
    if existing.get("schema_version") != SCHEMA_VERSION:
        if _is_safe_old_pending_schema(existing):
            return build_rights_review_template(package)
        raise VClipError(
            "Existing rights-review.json uses the old schema and contains human "
            "decisions. Automatic migration is refused; migrate it manually."
        )

    existing_clips = existing.get("clips")
    if not isinstance(existing_clips, list):
        raise VClipError("rights-review.json clips must be a list.")
    by_pair, by_stock, by_export = _identity_indexes(existing_clips)

    reconciled: list[dict[str, Any]] = []
    package_pairs: set[tuple[str, str]] = set()
    for package_clip in package.get("clips", []):
        stock_id, export_id = _identity(package_clip)
        key = (stock_id, export_id)
        package_pairs.add(key)
        if stock_id in by_stock and str(by_stock[stock_id]["export_id"]) != export_id:
            raise VClipError(f"Rights-review identity conflict for stock_clip_id {stock_id}.")
        if export_id in by_export and str(by_export[export_id]["stock_clip_id"]) != stock_id:
            raise VClipError(f"Rights-review identity conflict for export_id {export_id}.")
        if key in by_pair:
            entry = deepcopy(by_pair[key])
            entry["sort_order"] = int(package_clip["sort_order"])
            entry["customer_filename"] = package_clip["customer_filename"]
            reconciled.append(entry)
        else:
            reconciled.append(_default_clip_entry(package_clip))

    extras = set(by_pair) - package_pairs
    if extras:
        labels = ", ".join(f"{stock}/{export}" for stock, export in sorted(extras))
        raise VClipError(
            "rights-review.json contains clips outside the frozen release; refusing "
            f"to discard human data: {labels}"
        )
    reconciled.sort(key=lambda item: int(item["sort_order"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "collection_slug": package.get("collection_slug"),
        "collection_version": package.get("collection_version"),
        "clip_count": len(reconciled),
        "clips": reconciled,
    }


def validate_review_document(
    document: dict[str, Any],
    package: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    if document.get("schema_version") != SCHEMA_VERSION:
        failures.append(f"rights-review.json schema_version must be {SCHEMA_VERSION}")
    clips = document.get("clips")
    if not isinstance(clips, list):
        return [*failures, "rights-review.json clips must be a list"]
    if document.get("clip_count") != len(clips):
        failures.append(
            f"rights-review.json clip_count {document.get('clip_count')!r} does not "
            f"match clips length {len(clips)}"
        )

    try:
        by_pair, _, _ = _identity_indexes(clips)
    except VClipError as exc:
        failures.append(str(exc))
        return failures

    for package_clip in package.get("clips", []):
        key = _identity(package_clip)
        entry = by_pair.get(key)
        label = f"clip {key[0]}"
        if entry is None:
            failures.append(f"{label}: missing rights-review entry for {key[1]}")
            continue
        for failure in validate_human_fields(entry):
            failures.append(f"{label}: {failure}")
        expected = derive_classification(entry)
        stored = entry.get("classification") or {}
        if stored.get("derived_at") is None:
            if stored.get("value") != "unclassified":
                failures.append(f"{label}: classification without derived_at must be unclassified")
        else:
            for field in ("value", "policy_version", "reasons", "customer_notices"):
                if stored.get(field) != expected.get(field):
                    failures.append(
                        f"{label}: stored classification {field} does not match policy v1"
                    )
    return failures


# Compatibility alias for callers from the first content-readiness iteration.
validate_rights_review = validate_review_document


def _default_clip_entry(package_clip: dict[str, Any]) -> dict[str, Any]:
    return {
        "sort_order": int(package_clip["sort_order"]),
        "stock_clip_id": package_clip.get("stock_clip_id"),
        "export_id": package_clip.get("export_id"),
        "customer_filename": package_clip.get("customer_filename"),
        "facts": {field: "unconfirmed" for field in OBSERVATION_FIELDS},
        "capture_provenance": {"status": "unconfirmed", "notes": ""},
        "human_review": {
            "status": "pending",
            "reviewed_by": None,
            "reviewed_at": None,
            "notes": "",
        },
        "classification": {
            "value": "unclassified",
            "derived_at": None,
            "policy_version": POLICY_VERSION,
            "reasons": [],
            "customer_notices": [],
        },
    }


def _is_safe_old_pending_schema(document: dict[str, Any]) -> bool:
    clips = document.get("clips")
    if not isinstance(clips, list):
        return False
    for entry in clips:
        if not isinstance(entry, dict):
            return False
        if entry.get("review_status") != "pending":
            return False
        if any(entry.get(field) != "unchecked" for field in OLD_CHECK_FIELDS):
            return False
        if str(entry.get("reviewed_by") or "").strip():
            return False
        if str(entry.get("reviewed_at") or "").strip():
            return False
    return True


def _identity(entry: dict[str, Any]) -> tuple[str, str]:
    stock_id = str(entry.get("stock_clip_id") or "").strip()
    export_id = str(entry.get("export_id") or "").strip()
    if not stock_id or not export_id:
        raise VClipError("Review entry is missing stock_clip_id or export_id.")
    return stock_id, export_id


def _identity_indexes(
    clips: list[Any],
) -> tuple[
    dict[tuple[str, str], dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    by_stock: dict[str, dict[str, Any]] = {}
    by_export: dict[str, dict[str, Any]] = {}
    for raw in clips:
        if not isinstance(raw, dict):
            raise VClipError("rights-review.json contains a non-object clip entry.")
        stock_id, export_id = _identity(raw)
        key = (stock_id, export_id)
        if key in by_pair:
            raise VClipError(f"Duplicate rights-review identity: {stock_id}/{export_id}")
        if stock_id in by_stock or export_id in by_export:
            raise VClipError(f"Conflicting rights-review identity: {stock_id}/{export_id}")
        by_pair[key] = raw
        by_stock[stock_id] = raw
        by_export[export_id] = raw
    return by_pair, by_stock, by_export


def _clip_by_order(document: dict[str, Any], order: int, label: str) -> dict[str, Any]:
    for item in document.get("clips", []):
        if int(item.get("sort_order") or 0) == order:
            return item
    raise VClipError(f"{label} has no clip with sort_order {order}.")


def _require_same_identity(left: dict[str, Any], right: dict[str, Any]) -> None:
    if _identity(left) != _identity(right):
        raise VClipError("Evidence/review identity conflict for selected clip.")


def _accept_none_detected(
    facts: dict[str, Any],
    observations: dict[str, Any],
) -> None:
    for field in FACT_FIELDS:
        observation = observations.get(field) or {}
        if observation.get("status") == "none_detected":
            facts[field] = "none"
