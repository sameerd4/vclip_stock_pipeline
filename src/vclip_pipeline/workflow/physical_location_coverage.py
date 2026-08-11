"""Physical (manifest-event) location coverage audit for a review-shard corpus.

Unlike projected coverage, this does not overlay forensic recoveries. It counts
known vs Unknown Location solely from shard/event labels and classifies remaining
Unknown candidates by camera scope.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from ..db import CatalogRepository, Database
from ..util import json_dumps, utc_now
from .camera_scope import (
    SCOPE_OUT_OF_SCOPE_NON_DRONE,
    classify_vclip_camera_scope,
)
from .review_location_recover import ReviewLocationRecoverService


def is_unknown_event_name(event_name: str | None) -> bool:
    return "Unknown Location" in str(event_name or "")


class _NullResolver:
    def resolve(self, latitude: float, longitude: float):
        return None


def collect_physical_candidates(
    *,
    input_root: Path,
    repository: CatalogRepository,
) -> list[dict[str, Any]]:
    service = ReviewLocationRecoverService(repository, _NullResolver())
    appearances = service._collect_appearances(service._discover_shards(input_root))
    pairs = [(a["stockify_run_id"], a["stock_clip_id"]) for a in appearances]
    rows = repository.candidates_by_run_and_ids(pairs)

    by_clip: dict[str, dict[str, Any]] = {}
    for appearance in appearances:
        clip_id = str(appearance["stock_clip_id"])
        row = rows.get((appearance["stockify_run_id"], appearance["stock_clip_id"])) or {}
        source_basename = str(
            appearance.get("source_basename")
            or row.get("source_filename")
            or row.get("source_name")
            or Path(str(row.get("source_media_path") or "")).name
            or ""
        )
        event_name = str(appearance.get("event_name") or "")
        scope = classify_vclip_camera_scope(
            source_basename=source_basename,
            media_path=row.get("source_media_path"),
            camera_lut=row.get("camera_lut"),
            source_event_name=row.get("source_event_name"),
            source_project_name=row.get("source_project_name"),
            extra_texts=[
                event_name,
                appearance.get("project_name"),
                row.get("session_event_name"),
            ],
        )
        physical_unknown = is_unknown_event_name(event_name)
        oos = scope.get("camera_scope") == SCOPE_OUT_OF_SCOPE_NON_DRONE
        if physical_unknown and oos:
            physical_class = "out_of_scope_non_drone"
        elif physical_unknown:
            physical_class = "accepted_unresolved_drone"
        else:
            physical_class = "known_location"

        candidate = {
            "stock_clip_id": clip_id,
            "stockify_run_id": appearance["stockify_run_id"],
            "source_basename": source_basename,
            "event_name": event_name,
            "project_name": appearance.get("project_name"),
            "relative_xml": appearance.get("relative_xml"),
            "physical_unknown": physical_unknown,
            "physical_class": physical_class,
            "camera_scope": scope.get("camera_scope"),
            "camera_family": scope.get("camera_family"),
            "out_of_scope_non_drone": oos,
        }
        # Deduplicate by stock_clip_id; prefer Unknown if mixed (should not happen).
        prior = by_clip.get(clip_id)
        if prior is None:
            by_clip[clip_id] = candidate
        elif candidate["physical_unknown"] and not prior["physical_unknown"]:
            by_clip[clip_id] = candidate

    return sorted(by_clip.values(), key=lambda item: item["stock_clip_id"])


def build_physical_audit(
    *,
    input_root: Path,
    db_path: Path | None = None,
    repository: CatalogRepository | None = None,
) -> dict[str, Any]:
    if repository is None:
        if db_path is None:
            raise ValueError("db_path or repository is required")
        repository = CatalogRepository(Database(db_path))
    candidates = collect_physical_candidates(
        input_root=input_root, repository=repository
    )
    counts = Counter(item["physical_class"] for item in candidates)
    unknown = [item for item in candidates if item["physical_unknown"]]
    known = [item for item in candidates if not item["physical_unknown"]]
    troutville = [
        item["stock_clip_id"]
        for item in candidates
        if "troutville" in str(item.get("event_name") or "").casefold()
    ]
    unresolved_drone = [
        item for item in unknown if item["physical_class"] == "accepted_unresolved_drone"
    ]
    oos = [
        item for item in unknown if item["physical_class"] == "out_of_scope_non_drone"
    ]
    other_unknown = [
        item
        for item in unknown
        if item["physical_class"]
        not in {"accepted_unresolved_drone", "out_of_scope_non_drone"}
    ]
    return {
        "generated_at": utc_now(),
        "mode": "physical_manifest_location_coverage",
        "mutates_corpus": False,
        "input_root": str(input_root),
        "universe": {
            "physical_individual_candidates": len(candidates),
            "unique_stock_clip_ids": len(candidates),
            "duplicate_stock_clip_ids": 0,
            "dedupe_key": "stock_clip_id",
        },
        "physical_shard_state": {
            "total": len(candidates),
            "known_location": len(known),
            "unknown_location": len(unknown),
            "accepted_unresolved_drone": len(unresolved_drone),
            "out_of_scope_non_drone": len(oos),
            "other_unknown": len(other_unknown),
            "stale_troutville_candidates": len(troutville),
        },
        "by_physical_class": dict(counts),
        "unknown_events": dict(Counter(item["event_name"] for item in unknown)),
        "accepted_unresolved_drone_clip_ids": sorted(
            item["stock_clip_id"] for item in unresolved_drone
        ),
        "out_of_scope_non_drone_clip_ids": sorted(
            item["stock_clip_id"] for item in oos
        ),
        "other_unknown_clip_ids": sorted(
            item["stock_clip_id"] for item in other_unknown
        ),
        "stale_troutville_clip_ids": troutville,
        "note": (
            "Physical coverage uses shard/event labels only. Remaining Unknown "
            "candidates are split by camera_scope into accepted unresolved drone "
            "vs out-of-scope non-drone. No forensic overlay is applied."
        ),
    }


def format_physical_audit_text(audit: dict[str, Any]) -> str:
    phys = audit["physical_shard_state"]
    lines = [
        "PHYSICAL LOCATION COVERAGE AUDIT",
        "=" * 72,
        f"Generated at: {audit['generated_at']}",
        f"Input root:   {audit['input_root']}",
        f"Mode:         {audit['mode']} (mutates_corpus={audit['mutates_corpus']})",
        "",
        "Universe",
        "--------",
        f"  total candidates: {audit['universe']['physical_individual_candidates']}",
        f"  unique stock_clip_ids: {audit['universe']['unique_stock_clip_ids']}",
        f"  duplicates: {audit['universe']['duplicate_stock_clip_ids']}",
        "",
        "Physical shard/event state",
        "--------------------------",
        f"  known_location:            {phys['known_location']}",
        f"  unknown_location:          {phys['unknown_location']}",
        f"  accepted_unresolved_drone: {phys['accepted_unresolved_drone']}",
        f"  out_of_scope_non_drone:    {phys['out_of_scope_non_drone']}",
        f"  other_unknown:             {phys['other_unknown']}",
        f"  stale_troutville:          {phys['stale_troutville_candidates']}",
        "",
        "Unknown events",
        "--------------",
    ]
    for event, count in sorted(
        (audit.get("unknown_events") or {}).items(),
        key=lambda item: (-item[1], item[0]),
    ):
        lines.append(f"  {count:>4}  {event}")
    lines.extend(
        [
            "",
            f"Accepted unresolved drone IDs ({len(audit['accepted_unresolved_drone_clip_ids'])})",
            "--------------------------------",
        ]
    )
    for clip_id in audit["accepted_unresolved_drone_clip_ids"]:
        lines.append(f"  - {clip_id}")
    lines.extend(
        [
            "",
            f"Out-of-scope non-drone IDs ({len(audit['out_of_scope_non_drone_clip_ids'])})",
            "------------------------------",
        ]
    )
    for clip_id in audit["out_of_scope_non_drone_clip_ids"]:
        lines.append(f"  - {clip_id}")
    if audit.get("other_unknown_clip_ids"):
        lines.extend(["", "Other unknown IDs", "-----------------"])
        for clip_id in audit["other_unknown_clip_ids"]:
            lines.append(f"  - {clip_id}")
    lines.extend(["", f"Note: {audit['note']}", ""])
    return "\n".join(lines)


def write_physical_audit(
    audit: dict[str, Any],
    *,
    report: Path,
    text_report: Path,
) -> None:
    report.parent.mkdir(parents=True, exist_ok=True)
    text_report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json_dumps(audit), encoding="utf-8")
    text_report.write_text(format_physical_audit_text(audit), encoding="utf-8")
