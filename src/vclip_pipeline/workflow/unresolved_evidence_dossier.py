"""Report-only evidence dossiers for fully unresolved unknown clips.

Enumerates remaining possible evidence without inferring a location.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from ..db.repository import CatalogRepository
from ..errors import VClipError
from ..stockify.jpg_exif_same_shoot import (
    enumerate_nearby_jpg_evidence,
    find_dji_sequence_neighbors,
    parse_dji_file_identity,
)
from ..stockify.sidecars import normalized_stem
from .camera_scope import (
    SCOPE_OUT_OF_SCOPE_NON_DRONE,
    classify_appearance_camera_scope,
)
from .editorial_group_forensics import SourceGeoEvidence


def build_unresolved_evidence_dossiers(
    *,
    unknown_appearances: list[dict[str, Any]],
    source_evidence: dict[str, SourceGeoEvidence],
    editorial_summary: dict[str, Any],
    jpg_index: dict[str, list[Any]],
    media_roots: list[Path],
    repository: CatalogRepository,
) -> dict[str, Any]:
    """Build per-event dossiers for clips still lacking geographic context."""
    unresolved_keys = {
        (item["stockify_run_id"], item["stock_clip_id"])
        for item in editorial_summary.get("still_fully_unresolved_keys") or []
    }
    out_of_scope_keys = {
        (item["stockify_run_id"], item["stock_clip_id"])
        for item in editorial_summary.get("out_of_scope_non_drone_unresolved_keys")
        or []
    }
    if not unresolved_keys and not out_of_scope_keys:
        # Fall back to recomputing from evidence kinds if summary omitted keys.
        for item in unknown_appearances:
            stem = normalized_stem(item.get("source_basename"))
            evidence = source_evidence.get(stem)
            if evidence is not None and evidence.evidence_kind != "none":
                continue
            key = (item["stockify_run_id"], item["stock_clip_id"])
            scope = classify_appearance_camera_scope(item)
            if scope.get("camera_scope") == SCOPE_OUT_OF_SCOPE_NON_DRONE:
                out_of_scope_keys.add(key)
            else:
                unresolved_keys.add(key)

    located_by_date = _located_sources_by_date(source_evidence)
    in_scope_dossiers = _build_event_dossiers(
        unknown_appearances=unknown_appearances,
        selected_keys=unresolved_keys,
        source_evidence=source_evidence,
        jpg_index=jpg_index,
        media_roots=media_roots,
        repository=repository,
        located_by_date=located_by_date,
        camera_scope="drone_backlog",
    )
    out_of_scope_dossiers = _build_event_dossiers(
        unknown_appearances=unknown_appearances,
        selected_keys=out_of_scope_keys,
        source_evidence=source_evidence,
        jpg_index=jpg_index,
        media_roots=media_roots,
        repository=repository,
        located_by_date=located_by_date,
        camera_scope=SCOPE_OUT_OF_SCOPE_NON_DRONE,
    )
    return {
        "mode": "read_only_unresolved_evidence_dossier",
        "fully_unresolved_clips": len(unresolved_keys),
        "unresolved_events": len(in_scope_dossiers),
        "events_ranked_by_clip_count": in_scope_dossiers,
        "out_of_scope_non_drone_clips": len(out_of_scope_keys),
        "out_of_scope_non_drone_events": len(out_of_scope_dossiers),
        "out_of_scope_non_drone_events_ranked": out_of_scope_dossiers,
        "note": (
            "Ranked unresolved editorial events with remaining possible evidence "
            "sources enumerated per unique media source. No location inference. "
            f"Non-drone families ({SCOPE_OUT_OF_SCOPE_NON_DRONE}) are listed "
            "separately and excluded from the drone backlog."
        ),
    }


def _build_event_dossiers(
    *,
    unknown_appearances: list[dict[str, Any]],
    selected_keys: set[tuple[str, str]],
    source_evidence: dict[str, SourceGeoEvidence],
    jpg_index: dict[str, list[Any]],
    media_roots: list[Path],
    repository: CatalogRepository,
    located_by_date: dict[str, list[dict[str, Any]]],
    camera_scope: str,
) -> list[dict[str, Any]]:
    by_event: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for appearance in unknown_appearances:
        key = (appearance["stockify_run_id"], appearance["stock_clip_id"])
        if key not in selected_keys:
            continue
        event_name = appearance.get("event_name") or "Unknown Location"
        by_event[(appearance["stockify_run_id"], event_name)].append(appearance)

    library_by_run = {
        run_id: repository.libraries_for_stockify_run(run_id)
        for run_id in {run_id for run_id, _event in by_event}
    }
    run_meta: dict[str, dict[str, Any] | None] = {}
    for run_id in {run_id for run_id, _event in by_event}:
        try:
            run_meta[run_id] = repository.get_stockify_run(run_id)
        except VClipError:
            run_meta[run_id] = None

    dossiers: list[dict[str, Any]] = []
    for (run_id, event_name), members in by_event.items():
        stems = sorted(
            {
                normalized_stem(item.get("source_basename"))
                for item in members
                if normalized_stem(item.get("source_basename"))
            }
        )
        media_rows = repository.source_media_for_stems(run_id, stems)
        source_dossiers = []
        for stem in stems:
            stem_members = [
                item
                for item in members
                if normalized_stem(item.get("source_basename")) == stem
            ]
            dossier = _source_dossier(
                stem=stem,
                members=stem_members,
                source_evidence=source_evidence.get(stem),
                media_row=media_rows.get(stem),
                jpg_index=jpg_index,
                media_roots=media_roots,
                located_by_date=located_by_date,
                libraries=library_by_run.get(run_id) or [],
                run_meta=run_meta.get(run_id),
            )
            dossier["camera_scope"] = classify_appearance_camera_scope(
                stem_members[0] if stem_members else {}
            )
            source_dossiers.append(dossier)
        shard_paths = sorted(
            {
                str(item.get("relative_xml") or "")
                for item in members
                if item.get("relative_xml")
            }
        )
        dossiers.append(
            {
                "original_event_name": event_name,
                "stockify_run_id": run_id,
                "camera_scope": camera_scope,
                "shard_paths": shard_paths,
                "unresolved_clip_count": len(
                    {(m["stockify_run_id"], m["stock_clip_id"]) for m in members}
                ),
                "unique_sources": len(stems),
                "stock_clip_ids": sorted({m["stock_clip_id"] for m in members}),
                "fcp_provenance": {
                    "source_event_names": sorted(
                        {
                            str((m.get("row") or {}).get("source_event_name") or "")
                            for m in members
                            if (m.get("row") or {}).get("source_event_name")
                        }
                    ),
                    "source_project_names": sorted(
                        {
                            str((m.get("row") or {}).get("source_project_name") or "")
                            for m in members
                            if (m.get("row") or {}).get("source_project_name")
                        }
                    ),
                    "libraries": library_by_run.get(run_id) or [],
                    "stockify_run": _run_provenance(run_meta.get(run_id)),
                },
                "sources": source_dossiers,
                "note": (
                    "Evidence dossier only — no location inferred. "
                    "Nearby JPG/sequence neighbors are listed even below "
                    "current inference thresholds."
                ),
            }
        )

    dossiers.sort(
        key=lambda item: (-int(item["unresolved_clip_count"]), item["original_event_name"])
    )
    return dossiers


def _source_dossier(
    *,
    stem: str,
    members: list[dict[str, Any]],
    source_evidence: SourceGeoEvidence | None,
    media_row: dict[str, Any] | None,
    jpg_index: dict[str, list[Any]],
    media_roots: list[Path],
    located_by_date: dict[str, list[dict[str, Any]]],
    libraries: list[dict[str, Any]],
    run_meta: dict[str, Any] | None,
) -> dict[str, Any]:
    row = (members[0].get("row") or {}) if members else {}
    basename = str(
        (source_evidence.source_basename if source_evidence else None)
        or row.get("source_filename")
        or members[0].get("source_basename")
        or stem
    )
    media_path = row.get("source_media_path") or (media_row or {}).get("media_path")
    identity = parse_dji_file_identity(basename) or (
        parse_dji_file_identity(str(media_path)) if media_path else None
    )
    capture_date = (
        (identity.date if identity else None)
        or str(row.get("session_capture_date") or "")[:10]
        or str((media_row or {}).get("capture_date") or "")[:10]
        or None
    )
    weak_jpgs = enumerate_nearby_jpg_evidence(
        basename,
        jpg_index=jpg_index,
        media_path=str(media_path) if media_path else None,
    )
    sequence_neighbors = find_dji_sequence_neighbors(
        source_basename=basename,
        media_path=str(media_path) if media_path else None,
        media_roots=media_roots,
    )
    candidate_location = row.get("location") if isinstance(row.get("location"), dict) else {}
    media_location = (media_row or {}).get("location") if media_row else {}
    if not isinstance(media_location, dict):
        media_location = {}

    return {
        "source_basename": basename,
        "stem": stem,
        "stock_clip_ids": sorted({item["stock_clip_id"] for item in members}),
        "physical_media_paths": sorted(
            {
                str(path)
                for path in (
                    media_path,
                    (media_row or {}).get("media_path"),
                    row.get("source_srt_path"),
                    (media_row or {}).get("srt_path"),
                )
                if path
            }
        ),
        "dji_identity": identity.as_dict() if identity else None,
        "capture_date": capture_date,
        "fcp_library_event_project_provenance": {
            "source_event_name": row.get("source_event_name"),
            "source_project_name": row.get("source_project_name"),
            "source_project_index": row.get("source_project_index"),
            "project_name": members[0].get("project_name") if members else None,
            "event_name": members[0].get("event_name") if members else None,
            "libraries": libraries,
            "stockify_run": _run_provenance(run_meta),
        },
        "existing_location_json": {
            "candidate_location": candidate_location or None,
            "source_media_location": media_location or None,
            "session": {
                "session_city": row.get("session_city"),
                "session_neighborhood": row.get("session_neighborhood"),
                "session_public_label": row.get("session_public_label"),
                "session_center_lat": row.get("session_center_lat"),
                "session_center_lon": row.get("session_center_lon"),
                "session_event_name": row.get("session_event_name"),
            },
        },
        "nearby_same_day_jpgs": weak_jpgs,
        "nearby_dji_sequence_neighbors": sequence_neighbors,
        "same_day_resolved_sources": list(located_by_date.get(capture_date or "", [])),
        "current_source_evidence_kind": (
            source_evidence.evidence_kind if source_evidence else "none"
        ),
        "inference_status": "no_location_inferred",
    }


def _located_sources_by_date(
    source_evidence: dict[str, SourceGeoEvidence],
) -> dict[str, list[dict[str, Any]]]:
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for evidence in source_evidence.values():
        if evidence.evidence_kind == "none" or not (
            evidence.city or evidence.public_label or evidence.has_coordinates
        ):
            continue
        identity = parse_dji_file_identity(evidence.source_basename)
        date = identity.date if identity else None
        if not date:
            continue
        by_date[date].append(
            {
                "source_basename": evidence.source_basename,
                "stem": evidence.stem,
                "evidence_kind": evidence.evidence_kind,
                "city": evidence.city,
                "neighborhood": evidence.neighborhood,
                "public_label": evidence.public_label,
                "latitude": evidence.latitude,
                "longitude": evidence.longitude,
                "confidence": evidence.confidence,
            }
        )
    for date in by_date:
        by_date[date].sort(key=lambda item: item["source_basename"])
    return dict(by_date)


def _run_provenance(run_meta: dict[str, Any] | None) -> dict[str, Any] | None:
    if not run_meta:
        return None
    return {
        "id": run_meta.get("id"),
        "source_xml_path": run_meta.get("source_xml_path"),
        "output_xml_path": run_meta.get("output_xml_path"),
        "report_path": run_meta.get("report_path"),
        "manifest_path": run_meta.get("manifest_path"),
        "status": run_meta.get("status"),
    }
