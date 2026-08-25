#!/usr/bin/env python3
"""Read-only Projected Drone Location Coverage Audit for a review-shard corpus."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from ..db import CatalogRepository, Database
from ..stockify.sidecars import normalized_stem
from ..util import json_dumps, utc_now
from .camera_scope import (
    SCOPE_OUT_OF_SCOPE_NON_DRONE,
    classify_vclip_camera_scope,
)
from .editorial_group_forensics import (
    _implied_place_from_event_name,
    country_for_admin_area,
)
from .review_location_recover import (
    ReviewLocationRecoverService,
    _is_unknown_candidate,
)

MUTATION_STATES = frozenset(
    {
        "recoverable_jpg_exif",
        "stale_location_requires_correction",
        "recoverable_group_consensus",
    }
)

PROJECTED_STATES = (
    "known_existing",
    "recoverable_srt",
    "recoverable_jpg_exif",
    "recoverable_group_consensus",
    "stale_location_requires_correction",
    "accepted_unresolved_drone",
    "out_of_scope_non_drone",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-root",
        type=Path,
        default=Path("/Users/sameer/Desktop/vclip-work/work/review-shards-t9-recovery"),
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("/Users/sameer/Desktop/vclip-work/work/vclip.sqlite3"),
    )
    parser.add_argument(
        "--forensic-json",
        type=Path,
        default=Path(
            "/Users/sameer/Desktop/vclip-work/work/library-audits/jpg-exif-forensic.json"
        ),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(
            "/Users/sameer/Desktop/vclip-work/work/library-audits/"
            "projected-drone-location-coverage.json"
        ),
    )
    parser.add_argument(
        "--text-report",
        type=Path,
        default=Path(
            "/Users/sameer/Desktop/vclip-work/work/library-audits/"
            "projected-drone-location-coverage.txt"
        ),
    )
    args = parser.parse_args()
    audit = build_audit(
        input_root=args.input_root.expanduser().resolve(),
        db_path=args.db.expanduser().resolve(),
        forensic_path=args.forensic_json.expanduser().resolve(),
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json_dumps(audit), encoding="utf-8")
    args.text_report.write_text(format_text(audit), encoding="utf-8")
    print(f"JSON report: {args.report}")
    print(f"Text report: {args.text_report}")
    print(
        "Projected drone unresolved:",
        audit["drone_only"]["by_projected_state"].get("accepted_unresolved_drone"),
    )
    return 0


def classify_corpus_candidates(
    *,
    input_root: Path,
    repository: CatalogRepository,
    forensic: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Classify every candidate from shards + persisted forensic reports.

    Does not scan mounted media. Returns (candidates, appearances, overlay).
    """
    service = ReviewLocationRecoverService(repository, _NullResolver())
    shard_entries = service._discover_shards(input_root)
    appearances = service._collect_appearances(shard_entries)

    pairs = [(a["stockify_run_id"], a["stock_clip_id"]) for a in appearances]
    rows = repository.candidates_by_run_and_ids(pairs)
    for appearance in appearances:
        key = (appearance["stockify_run_id"], appearance["stock_clip_id"])
        appearance["row"] = rows.get(key) or {}

    overlay = _build_overlay(forensic, repository)

    by_clip: dict[str, dict[str, Any]] = {}
    for appearance in appearances:
        clip_id = str(appearance["stock_clip_id"])
        prior = by_clip.get(clip_id)
        candidate = _classify_candidate(appearance, overlay)
        if prior is None:
            by_clip[clip_id] = candidate
            continue
        by_clip[clip_id] = _prefer_candidate(prior, candidate)

    candidates = sorted(by_clip.values(), key=lambda item: item["stock_clip_id"])
    assert len(candidates) == len(by_clip)
    return candidates, appearances, overlay


def build_audit(
    *,
    input_root: Path,
    db_path: Path,
    forensic_path: Path,
    include_candidates: bool = False,
) -> dict[str, Any]:
    database = Database(db_path)
    repository = CatalogRepository(database)
    forensic = json.loads(forensic_path.read_text(encoding="utf-8"))
    candidates, _appearances, overlay = classify_corpus_candidates(
        input_root=input_root,
        repository=repository,
        forensic=forensic,
    )

    corpus = _summarize(candidates, drone_only=False)
    drone = _summarize(
        [item for item in candidates if not item["out_of_scope_non_drone"]],
        drone_only=True,
    )
    geo = _geo_aggregates(
        [item for item in candidates if not item["out_of_scope_non_drone"]]
    )
    stale = [
        {
            "stock_clip_id": item["stock_clip_id"],
            "physical_event_name": item["physical_event_name"],
            "physical_label": item["physical_label"],
            "projected_label": item["projected_label"],
            "projected_city": item["projected_city"],
            "projected_neighborhood": item["projected_neighborhood"],
            "evidence_kind": item["evidence_kind"],
            "contradiction": item["contradiction"],
            "source_basename": item["source_basename"],
        }
        for item in candidates
        if item["projected_state"] == "stale_location_requires_correction"
    ]
    physical = _physical_snapshot(candidates)
    projected = _projected_snapshot(candidates)

    payload = {
        "generated_at": utc_now(),
        "mode": "read_only_projected_coverage_audit",
        "mutates_corpus": False,
        "input_root": str(input_root),
        "forensic_json": str(forensic_path),
        "universe": {
            "physical_individual_candidates": len(candidates),
            "unique_stock_clip_ids": len(candidates),
            "dedupe_key": "stock_clip_id",
        },
        "overlay_sources": {
            "existing_known_shard_locations": True,
            "srt_gps_recoveries_applied_in_db": overlay["srt_applied_count"],
            "srt_gps_remaining_unknowns": overlay["srt_remaining_unknown_count"],
            "jpg_exif_same_shoot_sources": overlay["jpg_source_count"],
            "jpg_exif_same_shoot_clips": overlay["jpg_clip_count"],
            "editorial_group_consensus_approved_clips": overlay[
                "approved_consensus_clip_count"
            ],
            "stale_location_contradiction_events": overlay["stale_event_count"],
            "out_of_scope_non_drone_clips": overlay["oos_clip_count"],
            "accepted_unresolved_drone_clips": drone["by_projected_state"].get(
                "accepted_unresolved_drone", 0
            ),
        },
        "physical_shard_state_today": physical,
        "projected_post_recovery_state": projected,
        "entire_corpus": corpus,
        "drone_only": drone,
        "projected_drone_coverage_by_geography": geo,
        "stale_known_locations_requiring_correction": {
            "count": len(stale),
            "candidates": stale,
            "note": (
                "These candidates currently carry a non-unknown shard/event label "
                "that is contradicted by newer source-level GPS/provenance. They "
                "are NOT counted as correct known_existing coverage."
            ),
        },
        "accepted_unresolved_drone_clip_ids": sorted(
            item["stock_clip_id"]
            for item in candidates
            if item["projected_state"] == "accepted_unresolved_drone"
        ),
        "out_of_scope_non_drone_clip_ids": sorted(
            item["stock_clip_id"]
            for item in candidates
            if item["projected_state"] == "out_of_scope_non_drone"
        ),
        "note": (
            "Projected states overlay forensic knowledge without writing XML/DB. "
            "Prior applied SRT recoveries appear as known_existing in today's "
            "physical shard state when their labels remain uncontradicted."
        ),
    }
    if include_candidates:
        payload["candidates"] = candidates
    return payload


def _build_overlay(
    forensic: dict[str, Any],
    repository: CatalogRepository,
) -> dict[str, Any]:
    jf = forensic.get("jpg_exif_forensic") or {}
    evidence_by_stem: dict[str, dict[str, Any]] = {}
    jpg_clips: set[str] = set()
    srt_clips: set[str] = set()
    for row in jf.get("source_level_evidence") or []:
        stem = str(row.get("stem") or "")
        if stem:
            evidence_by_stem[stem] = row
        for clip_id in row.get("stock_clip_ids") or []:
            if row.get("evidence_kind") == "jpg_exif_same_shoot":
                jpg_clips.add(str(clip_id))
            elif row.get("evidence_kind") == "srt_gps":
                srt_clips.add(str(clip_id))

    # Hypothetical recoveries also map clip → jpg place.
    recovery_by_clip: dict[str, dict[str, Any]] = {}
    for item in forensic.get("recoveries") or []:
        clip_id = str(item.get("stock_clip_id") or "")
        if not clip_id:
            continue
        recovery_by_clip[clip_id] = item
        reason = str(item.get("recovery_reason") or "")
        if reason == "jpg_exif_same_shoot" or "jpg" in reason:
            jpg_clips.add(clip_id)
        if reason == "srt_gps_review_recovery" or reason.startswith("srt"):
            srt_clips.add(clip_id)

    approved_consensus: dict[str, dict[str, Any]] = {}
    stale_events: dict[str, dict[str, Any]] = {}
    for group in jf.get("editorial_groups") or []:
        event_name = str(group.get("original_event_name") or "")
        if any(
            "stale_event_label_contradicted_by_source_gps" in str(note)
            for note in group.get("contradictory_evidence") or []
        ):
            stale_events[event_name] = group
        approved = (
            not group.get("review_required")
            and str(group.get("confidence") or "") in {"high", "medium"}
            and group.get("recommended_group_label")
            and group.get("geographic_coherence")
            in {"neighborhood", "city", "metro", "region"}
        )
        if not approved:
            continue
        label = str(group.get("recommended_group_label") or "")
        states = {
            str(item.get("state") or "")
            for item in group.get("source_evidence") or []
            if item.get("state")
        }
        group_country = country_for_admin_area(
            next(iter(states)) if len(states) == 1 else None,
            countries=[
                item.get("country")
                for item in group.get("source_evidence") or []
            ],
        )
        for clip_id in group.get("unknown_clips_eligible_to_inherit") or []:
            approved_consensus[str(clip_id)] = {
                "label": label,
                "level": group.get("recommended_label_level"),
                "confidence": group.get("confidence"),
                "event_name": event_name,
                "country": group_country,
                "evidence_source": "editorial_group_consensus",
            }

    oos_clips = {
        str(item.get("stock_clip_id"))
        for item in (jf.get("editorial_group_summary") or {}).get(
            "out_of_scope_non_drone_keys"
        )
        or []
    }
    # Also include unresolved oos keys.
    oos_clips |= {
        str(item.get("stock_clip_id"))
        for item in (jf.get("editorial_group_summary") or {}).get(
            "out_of_scope_non_drone_unresolved_keys"
        )
        or []
    }

    srt_applied = _load_applied_srt_recoveries(repository)

    return {
        "evidence_by_stem": evidence_by_stem,
        "jpg_clips": jpg_clips,
        "srt_clips": srt_clips | set(srt_applied),
        "srt_applied": srt_applied,
        "srt_applied_count": len(srt_applied),
        "srt_remaining_unknown_count": 0,  # filled later if needed
        "recovery_by_clip": recovery_by_clip,
        "approved_consensus": approved_consensus,
        "approved_consensus_clip_count": len(approved_consensus),
        "stale_events": stale_events,
        "stale_event_count": len(stale_events),
        "oos_clips": oos_clips,
        "oos_clip_count": len(oos_clips),
        "jpg_source_count": sum(
            1
            for row in evidence_by_stem.values()
            if row.get("evidence_kind") == "jpg_exif_same_shoot"
        ),
        "jpg_clip_count": len(jpg_clips),
    }


def _load_applied_srt_recoveries(repository: CatalogRepository) -> dict[str, dict[str, Any]]:
    with repository.database.connect() as connection:
        rows = connection.execute(
            """
            SELECT stock_clip_id, new_event_name, original_event_name,
                   recovery_reason, representative_lat, representative_lon,
                   provenance_json
            FROM review_location_recoveries
            WHERE recovery_reason = 'srt_gps_review_recovery'
            """
        ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        out[str(item["stock_clip_id"])] = item
    return out


def _classify_candidate(
    appearance: dict[str, Any],
    overlay: dict[str, Any],
) -> dict[str, Any]:
    row = appearance.get("row") or {}
    clip_id = str(appearance["stock_clip_id"])
    event_name = str(appearance.get("event_name") or "")
    source_basename = str(
        appearance.get("source_basename")
        or row.get("source_filename")
        or row.get("source_name")
        or Path(str(row.get("source_media_path") or "")).name
        or ""
    )
    if not appearance.get("source_basename"):
        appearance["source_basename"] = source_basename
    stem = normalized_stem(source_basename)
    scope = classify_vclip_camera_scope(
        source_basename=source_basename,
        media_path=row.get("source_media_path"),
        camera_lut=row.get("camera_lut"),
        source_event_name=row.get("source_event_name"),
        source_project_name=row.get("source_project_name"),
        extra_texts=[event_name, appearance.get("project_name"), row.get("session_event_name")],
    )
    # Physical shard state follows the event label in the review corpus.
    # A named non-Unknown event is "known" even if DB session city is empty
    # (e.g. stale Troutville labels living in an --unknown-- shard).
    if event_name.strip() and "Unknown Location" not in event_name:
        physical_unknown = False
    else:
        physical_unknown = _is_unknown_candidate(row, event_name)
    physical_label = _physical_label(event_name, row)
    evidence = overlay["evidence_by_stem"].get(stem) or {}
    recovery = overlay["recovery_by_clip"].get(clip_id) or {}
    consensus = overlay["approved_consensus"].get(clip_id)
    stale_group = overlay["stale_events"].get(event_name)

    evidence_kind = str(evidence.get("evidence_kind") or "none")
    has_jpg = clip_id in overlay["jpg_clips"] or evidence_kind == "jpg_exif_same_shoot"
    has_srt = clip_id in overlay["srt_clips"] or evidence_kind == "srt_gps"
    oos = (
        clip_id in overlay["oos_clips"]
        or scope.get("camera_scope") == SCOPE_OUT_OF_SCOPE_NON_DRONE
    )

    projected_place = _projected_place(
        evidence=evidence,
        recovery=recovery,
        consensus=consensus,
        row=row,
        event_name=event_name,
    )
    # Contradictions require newer source-level GPS evidence (SRT/JPG), never
    # session-fallback vs ambiguous "Neighborhood, City" event parsing.
    contradiction = None
    source_city = evidence.get("city") if evidence else None
    if (has_jpg or has_srt) and (stale_group is not None or source_city):
        if stale_group is not None:
            contradiction = (
                "stale_event_label_contradicted_by_source_gps:"
                f"event={event_name}|sources="
                + ",".join(stale_group.get("cities_represented") or [])
            )
        elif source_city and _event_conflicts_with_source_city(event_name, evidence):
            contradiction = (
                "stale_event_label_contradicted_by_source_gps:"
                f"event={event_name}|source_city={source_city}"
            )

    # Classification precedence for projected post-recovery state.
    if oos:
        state = "out_of_scope_non_drone"
    elif contradiction and (has_jpg or has_srt):
        state = "stale_location_requires_correction"
    elif has_srt and physical_unknown:
        state = "recoverable_srt"
    elif has_jpg and physical_unknown:
        state = "recoverable_jpg_exif"
    elif consensus is not None and physical_unknown and not has_jpg and not has_srt:
        state = "recoverable_group_consensus"
    elif not physical_unknown:
        # Named labels inside --unknown-- shards with no source GPS are not
        # confirmed coverage (e.g. "Capitol Hill, Seattle — Unknown Date" with
        # empty session location and no SRT/JPG evidence).
        if _unconfirmed_named_label(
            appearance=appearance,
            row=row,
            has_jpg=has_jpg,
            has_srt=has_srt,
        ):
            state = "accepted_unresolved_drone"
        else:
            state = "known_existing"
    else:
        state = "accepted_unresolved_drone"

    # If stale, prefer source GPS / editorial group corrected place.
    if state == "stale_location_requires_correction" and not (
        projected_place.get("source") in {"jpg_exif_same_shoot", "srt_gps"}
        and projected_place.get("city")
    ):
        if evidence.get("city"):
            projected_place = {
                "city": evidence.get("city"),
                "neighborhood": evidence.get("neighborhood"),
                "state": evidence.get("state"),
                "country": evidence.get("country"),
                "public_label": evidence.get("public_label"),
                "source": evidence.get("evidence_kind"),
            }
        elif stale_group is not None:
            cities = stale_group.get("cities_represented") or []
            neighborhoods = stale_group.get("neighborhoods_represented") or []
            projected_place = {
                "city": cities[0] if cities else None,
                "neighborhood": neighborhoods[0] if neighborhoods else None,
                "public_label": stale_group.get("recommended_group_label"),
                "state": None,
                "country": None,
                "source": "editorial_group_source_gps",
            }

    return {
        "stock_clip_id": clip_id,
        "stockify_run_id": appearance["stockify_run_id"],
        "source_basename": source_basename,
        "stem": stem,
        "physical_event_name": event_name,
        "physical_project_name": appearance.get("project_name"),
        "physical_unknown": physical_unknown,
        "physical_label": physical_label,
        "relative_xml": appearance.get("relative_xml"),
        "camera_scope": scope.get("camera_scope"),
        "camera_family": scope.get("camera_family"),
        "out_of_scope_non_drone": oos,
        "evidence_kind": evidence_kind if evidence_kind != "none" else (
            "jpg_exif_same_shoot"
            if has_jpg
            else ("srt_gps" if has_srt else ("editorial_group_consensus" if consensus else "none"))
        ),
        "has_jpg_exif": has_jpg,
        "has_srt_gps": has_srt,
        "has_approved_group_consensus": consensus is not None,
        "contradiction": contradiction,
        "projected_state": state,
        "projected_label": projected_place.get("public_label")
        or _label_from_parts(projected_place),
        "projected_city": projected_place.get("city"),
        "projected_neighborhood": _neighborhood_only(projected_place.get("neighborhood")),
        "projected_state_region": projected_place.get("state"),
        "projected_country": projected_place.get("country"),
        "projected_place_source": projected_place.get("source"),
    }


def _projected_place(
    *,
    evidence: dict[str, Any],
    recovery: dict[str, Any],
    consensus: dict[str, Any] | None,
    row: dict[str, Any],
    event_name: str,
) -> dict[str, Any]:
    if evidence and evidence.get("evidence_kind") in {"jpg_exif_same_shoot", "srt_gps"}:
        return {
            "city": evidence.get("city"),
            "neighborhood": evidence.get("neighborhood"),
            "state": evidence.get("state"),
            "country": evidence.get("country"),
            "public_label": evidence.get("public_label"),
            "source": evidence.get("evidence_kind"),
        }
    if recovery:
        new_event = str(recovery.get("new_event_name") or "")
        implied = _implied_place_from_event_name(new_event)
        city = implied[0] if implied else None
        state = implied[1] if implied else None
        # Prefer nested provenance location if present.
        prov = recovery.get("provenance") or {}
        if isinstance(prov, str):
            try:
                prov = json.loads(prov)
            except json.JSONDecodeError:
                prov = {}
        loc = prov.get("resolved_location") if isinstance(prov, dict) else None
        if isinstance(loc, dict) and loc.get("city"):
            return {
                "city": loc.get("city"),
                "neighborhood": loc.get("neighborhood"),
                "state": loc.get("state") or loc.get("region"),
                "country": loc.get("country"),
                "public_label": loc.get("public_label") or new_event.split(" — ")[0],
                "source": recovery.get("recovery_reason") or "recovery",
            }
        return {
            "city": _title(city) if city else None,
            "neighborhood": None,
            "state": _title(state) if state else None,
            "country": None,
            "public_label": new_event.split(" — ")[0] if new_event else None,
            "source": recovery.get("recovery_reason") or "recovery",
        }
    if consensus:
        label = str(consensus.get("label") or "")
        implied = _implied_place_from_event_name(label)
        parsed = _parse_existing_event_label(label)
        state = (
            _title(implied[1])
            if implied and implied[1]
            else parsed.get("state")
        )
        return {
            "city": _title(implied[0]) if implied and implied[0] else label,
            "neighborhood": label if consensus.get("level") == "neighborhood" else None,
            "state": state,
            "country": country_for_admin_area(
                state,
                explicit_country=str(consensus.get("country") or parsed.get("country") or "")
                or None,
            ),
            "public_label": label,
            "source": "editorial_group_consensus",
        }
    # Fall back to current DB/session location for known clips; if those are
    # empty, parse the shard event label (Neighborhood, City — date).
    location = row.get("location") if isinstance(row.get("location"), dict) else {}
    city = location.get("city") or row.get("session_city")
    neighborhood = location.get("neighborhood") or row.get("session_neighborhood")
    state = location.get("state") or row.get("session_state")
    country = location.get("country") or row.get("session_country")
    public_label = (
        location.get("public_label")
        or row.get("session_public_label")
        or (event_name.split(" — ")[0] if event_name and "Unknown Location" not in event_name else None)
    )
    if (not city or not state) and public_label:
        parsed = _parse_existing_event_label(public_label)
        city = city or parsed.get("city")
        neighborhood = neighborhood or parsed.get("neighborhood")
        state = state or parsed.get("state")
        country = country or parsed.get("country")
    return {
        "city": city,
        "neighborhood": neighborhood,
        "state": state,
        "country": country,
        "public_label": public_label,
        "source": "existing_shard_or_session",
    }


def _parse_existing_event_label(label: str) -> dict[str, str | None]:
    """Best-effort parse of VClip event labels like 'Capitol Hill, Seattle'."""
    text = str(label or "").strip()
    if not text or "unknown location" in text.casefold():
        return {"city": None, "neighborhood": None, "state": None, "country": None}
    # Common US state names used in this corpus.
    states = {
        "washington",
        "california",
        "virginia",
        "oregon",
        "british columbia",
        "hawaii",
        "florida",
        "nevada",
        "arizona",
        "colorado",
        "texas",
        "new york",
    }
    if "," in text:
        left, right = [part.strip() for part in text.split(",", 1)]
        right_norm = _norm(right) or ""
        if right_norm in states:
            return {
                "city": left,
                "neighborhood": None,
                "state": right,
                "country": (
                    "Canada" if right_norm == "british columbia" else "United States"
                ),
            }
        # Neighborhood, City
        return {
            "city": right,
            "neighborhood": left,
            "state": None,
            "country": None,
        }
    return {"city": text, "neighborhood": None, "state": None, "country": None}


def _physical_label(event_name: str, row: dict[str, Any]) -> str | None:
    if event_name and "Unknown Location" not in event_name:
        return event_name.split(" — ")[0].strip()
    location = row.get("location") if isinstance(row.get("location"), dict) else {}
    return (
        location.get("public_label")
        or row.get("session_public_label")
        or location.get("city")
        or row.get("session_city")
        or None
    )


def _prefer_candidate(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    # Prefer correction/recovery states over plain known/unresolved.
    priority = {
        "stale_location_requires_correction": 0,
        "recoverable_srt": 1,
        "recoverable_jpg_exif": 2,
        "recoverable_group_consensus": 3,
        "out_of_scope_non_drone": 4,
        "known_existing": 5,
        "accepted_unresolved_drone": 6,
    }
    ra = priority.get(a["projected_state"], 99)
    rb = priority.get(b["projected_state"], 99)
    return a if ra <= rb else b


def _summarize(candidates: list[dict[str, Any]], *, drone_only: bool) -> dict[str, Any]:
    total = len(candidates)
    by_state = Counter(item["projected_state"] for item in candidates)
    physical_known = sum(1 for item in candidates if not item["physical_unknown"])
    physical_unknown = total - physical_known
    located_projected = sum(
        1
        for item in candidates
        if item["projected_state"]
        in {
            "known_existing",
            "recoverable_srt",
            "recoverable_jpg_exif",
            "recoverable_group_consensus",
            "stale_location_requires_correction",
        }
    )
    return {
        "universe": "drone_only" if drone_only else "entire_corpus",
        "total_candidates": total,
        "by_projected_state": {state: by_state.get(state, 0) for state in PROJECTED_STATES},
        "by_projected_state_pct": {
            state: round(100.0 * by_state.get(state, 0) / total, 2) if total else 0.0
            for state in PROJECTED_STATES
        },
        "physical_known_today": physical_known,
        "physical_unknown_today": physical_unknown,
        "projected_located_or_correctable": located_projected,
        "projected_located_or_correctable_pct": (
            round(100.0 * located_projected / total, 2) if total else 0.0
        ),
        "unique_sources": len({item["stem"] for item in candidates if item.get("stem")}),
    }


def _geo_aggregates(drone_candidates: list[dict[str, Any]]) -> dict[str, Any]:
    located_states = {
        "known_existing",
        "recoverable_srt",
        "recoverable_jpg_exif",
        "recoverable_group_consensus",
        "stale_location_requires_correction",
    }
    rows = [
        item
        for item in drone_candidates
        if item["projected_state"] in located_states and item.get("projected_city")
    ]

    def agg(key_fn):
        buckets: dict[str, dict[str, Any]] = {}
        for item in rows:
            key = key_fn(item) or "Unknown"
            bucket = buckets.setdefault(
                key,
                {"label": key, "clip_count": 0, "source_stems": set()},
            )
            bucket["clip_count"] += 1
            if item.get("stem"):
                bucket["source_stems"].add(item["stem"])
        out = []
        for label, bucket in buckets.items():
            out.append(
                {
                    "label": label,
                    "clip_count": bucket["clip_count"],
                    "unique_sources": len(bucket["source_stems"]),
                }
            )
        out.sort(key=lambda item: (-item["clip_count"], item["label"]))
        return out

    return {
        "located_drone_clips": len(rows),
        "by_country": agg(lambda item: item.get("projected_country") or "Unknown"),
        "by_state_region": agg(
            lambda item: (
                f"{item.get('projected_state_region')}, {item.get('projected_country')}"
                if item.get("projected_state_region") and item.get("projected_country")
                else item.get("projected_state_region")
                or item.get("projected_country")
                or "Unknown"
            )
        ),
        "by_city": agg(
            lambda item: (
                f"{item.get('projected_city')}, {item.get('projected_state_region')}"
                if item.get("projected_city") and item.get("projected_state_region")
                else item.get("projected_city") or "Unknown"
            )
        ),
        "by_neighborhood": agg(
            lambda item: (
                f"{item.get('projected_neighborhood')}, {item.get('projected_city')}"
                if item.get("projected_neighborhood") and item.get("projected_city")
                else item.get("projected_neighborhood")
                or item.get("projected_city")
                or "Unknown"
            )
        ),
    }


def _physical_snapshot(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    known = [item for item in candidates if not item["physical_unknown"]]
    unknown = [item for item in candidates if item["physical_unknown"]]
    oos_unknown = [item for item in unknown if item["out_of_scope_non_drone"]]
    drone_unknown = [item for item in unknown if not item["out_of_scope_non_drone"]]
    # Physical "known" that forensic marks stale should not be celebrated.
    stale_known = [
        item
        for item in known
        if item["projected_state"] == "stale_location_requires_correction"
    ]
    return {
        "total": len(candidates),
        "known_location_labels": len(known),
        "known_location_labels_uncontradicted": len(known) - len(stale_known),
        "known_location_labels_contradicted_by_source_gps": len(stale_known),
        "unknown_location_labels": len(unknown),
        "unknown_drone": len(drone_unknown),
        "unknown_out_of_scope_non_drone": len(oos_unknown),
        "note": (
            "Physical shard/event labels as they exist today in "
            "review-shards-t9-recovery. Contradicted known labels are counted "
            "separately and are not treated as correct coverage."
        ),
    }


def _projected_snapshot(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    drone = [item for item in candidates if not item["out_of_scope_non_drone"]]
    by_state = Counter(item["projected_state"] for item in drone)
    covered = (
        by_state.get("known_existing", 0)
        + by_state.get("recoverable_srt", 0)
        + by_state.get("recoverable_jpg_exif", 0)
        + by_state.get("recoverable_group_consensus", 0)
        + by_state.get("stale_location_requires_correction", 0)
    )
    return {
        "drone_candidates": len(drone),
        "projected_drone_located_or_correctable": covered,
        "projected_drone_located_or_correctable_pct": (
            round(100.0 * covered / len(drone), 2) if drone else 0.0
        ),
        "accepted_unresolved_drone": by_state.get("accepted_unresolved_drone", 0),
        "out_of_scope_non_drone": sum(
            1 for item in candidates if item["out_of_scope_non_drone"]
        ),
        "by_state": {state: by_state.get(state, 0) for state in PROJECTED_STATES},
    }


def format_text(audit: dict[str, Any]) -> str:
    lines = [
        "PROJECTED DRONE LOCATION COVERAGE AUDIT",
        "=" * 72,
        f"Generated at: {audit['generated_at']}",
        f"Input root:   {audit['input_root']}",
        f"Mode:         {audit['mode']} (mutates_corpus={audit['mutates_corpus']})",
        f"Universe:     {audit['universe']['physical_individual_candidates']} "
        f"candidates deduped by {audit['universe']['dedupe_key']}",
        "",
        "Physical shard state today",
        "--------------------------",
    ]
    phys = audit["physical_shard_state_today"]
    for key in (
        "total",
        "known_location_labels",
        "known_location_labels_uncontradicted",
        "known_location_labels_contradicted_by_source_gps",
        "unknown_location_labels",
        "unknown_drone",
        "unknown_out_of_scope_non_drone",
    ):
        lines.append(f"  {key}: {phys[key]}")
    lines.extend(["", "Projected post-recovery state (drone)", "------------------------------------"])
    proj = audit["projected_post_recovery_state"]
    lines.append(f"  drone_candidates: {proj['drone_candidates']}")
    lines.append(
        f"  located_or_correctable: {proj['projected_drone_located_or_correctable']} "
        f"({proj['projected_drone_located_or_correctable_pct']}%)"
    )
    lines.append(f"  accepted_unresolved_drone: {proj['accepted_unresolved_drone']}")
    lines.append(f"  out_of_scope_non_drone: {proj['out_of_scope_non_drone']}")
    lines.extend(["", "Entire corpus by projected state", "--------------------------------"])
    corpus = audit["entire_corpus"]
    for state in PROJECTED_STATES:
        lines.append(
            f"  {state}: {corpus['by_projected_state'][state]} "
            f"({corpus['by_projected_state_pct'][state]}%)"
        )
    lines.extend(["", "Drone-only by projected state", "-----------------------------"])
    drone = audit["drone_only"]
    for state in PROJECTED_STATES:
        if state == "out_of_scope_non_drone":
            continue
        lines.append(
            f"  {state}: {drone['by_projected_state'][state]} "
            f"({drone['by_projected_state_pct'][state]}%)"
        )
    lines.extend(["", "Projected drone geography (top cities)", "--------------------------------------"])
    for row in audit["projected_drone_coverage_by_geography"]["by_city"][:15]:
        lines.append(
            f"  {row['clip_count']} clips / {row['unique_sources']} sources — {row['label']}"
        )
    lines.extend(["", "Projected drone geography (top neighborhoods)", "---------------------------------------------"])
    for row in audit["projected_drone_coverage_by_geography"]["by_neighborhood"][:15]:
        lines.append(
            f"  {row['clip_count']} clips / {row['unique_sources']} sources — {row['label']}"
        )
    stale = audit["stale_known_locations_requiring_correction"]
    lines.extend(
        [
            "",
            "Stale known locations requiring correction",
            "-----------------------------------------",
            f"  count: {stale['count']}",
            f"  note: {stale['note']}",
        ]
    )
    for item in stale["candidates"][:20]:
        lines.append(
            f"  - {item['stock_clip_id']}: {item['physical_event_name']} "
            f"→ {item['projected_label']} ({item['evidence_kind']})"
        )
    lines.extend(
        [
            "",
            f"Accepted unresolved drone clips: "
            f"{len(audit['accepted_unresolved_drone_clip_ids'])}",
            f"Note: {audit['note']}",
            "",
        ]
    )
    return "\n".join(lines)


def _unconfirmed_named_label(
    *,
    appearance: dict[str, Any],
    row: dict[str, Any],
    has_jpg: bool,
    has_srt: bool,
) -> bool:
    if has_jpg or has_srt:
        return False
    relative = str(appearance.get("relative_xml") or "").casefold()
    if "--unknown--" not in relative and "/unknown" not in relative:
        return False
    location = row.get("location") if isinstance(row.get("location"), dict) else {}
    city = str(location.get("city") or row.get("session_city") or "").strip()
    label = str(
        location.get("public_label") or row.get("session_public_label") or ""
    ).strip()
    if city or label:
        return False
    return True


def _event_conflicts_with_source_city(
    event_name: str,
    evidence: dict[str, Any],
) -> bool:
    """True when a named event label conflicts with source-level city evidence."""
    if not event_name or "Unknown Location" in event_name:
        return False
    source_city = _norm(evidence.get("city"))
    source_state = _norm(evidence.get("state"))
    if not source_city:
        return False
    head = event_name
    for sep in (" — ", " – ", "—", "–"):
        if sep in head:
            head = head.split(sep, 1)[0].strip()
            break
    head_norm = _norm(head) or ""
    # Compatible if event head contains the source city (Capitol Hill, Seattle)
    # or equals City, State.
    if source_city in head_norm:
        return False
    if source_state and f"{source_city}, {source_state}" == head_norm:
        return False
    implied = _implied_place_from_event_name(event_name)
    if not implied or not implied[0]:
        return False
    implied_city, implied_state = implied
    # "Neighborhood, City" events parse as city=Neighborhood — only conflict when
    # neither token matches the source city and the implied city looks like a
    # distinct municipality (has a state token that also mismatches).
    if implied_city == source_city:
        return False
    if implied_state and implied_state == source_city:
        # Parsed "Capitol Hill, Seattle" → state token is actually city.
        return False
    if implied_state and source_state and implied_state != source_state:
        return True
    if implied_state and implied_city != source_city:
        # City, State style mismatch (Troutville, Virginia vs Seattle).
        return True
    return False


def _neighborhood_only(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    if "," in text:
        return text.split(",", 1)[0].strip()
    return text


def _label_from_parts(place: dict[str, Any]) -> str | None:
    if place.get("neighborhood") and place.get("city"):
        nb = _neighborhood_only(place.get("neighborhood"))
        return f"{nb}, {place['city']}"
    if place.get("city") and place.get("state"):
        return f"{place['city']}, {place['state']}"
    return place.get("city") or place.get("public_label")


def _norm(value: str | None) -> str | None:
    if not value:
        return None
    return " ".join(str(value).casefold().split())


def _title(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(part.capitalize() for part in value.split())


class _NullResolver:
    def resolve(self, latitude: float, longitude: float):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
