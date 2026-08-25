"""Propagate already-solved historical locations onto latest unresolved rows.

Phase 1 copies existing catalog / final-corpus locations. Phase 2 copies
safe_to_inherit source-identity donor locations. Neither remounts media,
reruns SRT/JPG inference, or rewrites FCPXML.

Validate is read-only. Write requires explicit --write, a pre-mutation backup,
zero missing candidates, and zero conflicting current evidence. Phase 2 never
writes ambiguous source-identity rows and does not flatten session summaries.
"""

from __future__ import annotations

import copy
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..db.repository import CatalogRepository
from ..errors import VClipError
from ..stockify.jpg_exif_same_shoot import parse_dji_file_identity
from ..stockify.metadata import is_usable_gps
from ..stockify.naming import TIME_LABELS, event_base_name, project_base_label
from ..util import json_loads, safe_filename
from .catalog import WorkflowCatalog
from .physical_location_coverage import is_unknown_event_name
from .review_location_recover import (
    LocationRecoveryRow,
    persist_review_location_candidate_updates,
)
from .review_location_restore import (
    ALREADY_APPLIED,
    CONFLICTING_CURRENT,
    GPS_KIND_JPG,
    MALFORMED,
    MISSING_CANDIDATE,
    RANK_JPG,
    RANK_UNKNOWN,
    SAFE_TO_RESTORE,
    SAFETY_CLASSES,
    STRONGER_CURRENT,
    _conservative_session_summaries,
    _persist_session_summaries,
    create_pre_restore_backup,
    current_evidence_rank,
)

MODE = "historical_location_propagate_validation"
WRITE_MODE = "historical_location_propagate_write"
PHASE2_MODE = "historical_location_propagate_phase2_validation"
PHASE2_WRITE_MODE = "historical_location_propagate_phase2_write"
SOURCE_IDENTITY_MODE = "source_identity_propagation_safety"

PHASE1_CORPUS = "ALREADY_SOLVED_FINAL_CORPUS"
PHASE1_OLDER_DB = "OLDER_DB_OCCURRENCE_RESOLVED"
PHASE1_BUCKETS = frozenset({PHASE1_CORPUS, PHASE1_OLDER_DB})
SOURCE_IDENTITY_BUCKET = "SOURCE_IDENTITY_HAS_RESOLVED_EVIDENCE"

REASON_OLDER_DB = "older_db_occurrence_propagation"
REASON_CORPUS = "final_corpus_location_propagation"
REASON_SOURCE_IDENTITY = "source_identity_occurrence_propagation"
PROPAGATION_EVIDENCE = "historical_location_propagation"
CORPUS_EVIDENCE = "final_review_corpus"
JPG_EVIDENCE_SOURCES = frozenset(
    {
        "jpg_exif_same_shoot",
        "inferred_jpg_exif_same_shoot",
        "inferred_jpg_exif",
    }
)
JPG_GPS_KINDS = frozenset(
    {
        GPS_KIND_JPG,
        "inferred_jpg_exif_same_shoot",
        "inferred_jpg_exif",
        "jpg_exif_same_shoot",
    }
)

SAFE_TO_INHERIT = "safe_to_inherit"
AMBIGUOUS_SOURCE = "ambiguous_source_identity"
CONFLICTING_SOURCE = "conflicting_source_identity"
MISSING_SOURCE_DONOR = "missing_source_donor"

GPS_CONFLICT_METERS = 1000.0
LIBRARY_8_6_26 = "8-6-26"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EVENT_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

US_STATE_NAMES = frozenset(
    {
        "alabama",
        "alaska",
        "arizona",
        "arkansas",
        "california",
        "colorado",
        "connecticut",
        "delaware",
        "florida",
        "georgia",
        "hawaii",
        "idaho",
        "illinois",
        "indiana",
        "iowa",
        "kansas",
        "kentucky",
        "louisiana",
        "maine",
        "maryland",
        "massachusetts",
        "michigan",
        "minnesota",
        "mississippi",
        "missouri",
        "montana",
        "nebraska",
        "nevada",
        "new hampshire",
        "new jersey",
        "new mexico",
        "new york",
        "north carolina",
        "north dakota",
        "ohio",
        "oklahoma",
        "oregon",
        "pennsylvania",
        "rhode island",
        "south carolina",
        "south dakota",
        "tennessee",
        "texas",
        "utah",
        "vermont",
        "virginia",
        "washington",
        "west virginia",
        "wisconsin",
        "wyoming",
        "district of columbia",
    }
)

UNKNOWN_LABELS = frozenset(
    {
        "",
        "unknown",
        "unknown location",
        "unknown date",
        "none",
        "null",
        "n/a",
    }
)

STRONG_EVIDENCE_SOURCES = frozenset(
    {
        "manual_gps_override",
        "manual_confirmed",
        "manual_override",
        "srt_gps",
        "srt_gps_review_recovery",
        "source_srt",
        "direct_source_gps",
        "flight_trajectory",
        "flight_session_trajectory",
        "jpg_exif_same_shoot",
        "inferred_jpg_exif_same_shoot",
        "inferred_jpg_exif",
    }
)

LATEST_UNIVERSE = {
    "universe_name": "latest_accepted_stock_clip_id",
    "dedupe_key": "stock_clip_id",
    "eligibility": "stock_candidates.eligibility_status='accepted'",
    "latest_run_semantics": (
        "one row per stock_clip_id ordered by stockify_runs.started_at DESC, "
        "stock_candidates.updated_at DESC, run_id DESC"
    ),
    "resolved_definition": (
        "location_json has a non-unknown city, neighborhood, or public_label, "
        "or usable center_lat/center_lon"
    ),
}

PHASE1_MUTATION_UNIVERSE = {
    "universe_name": "phase1_historical_location_propagation_targets",
    "dedupe_key": ["stockify_run_id", "stock_clip_id"],
    "latest_run_semantics": (
        "write target is the live latest accepted occurrence of each "
        "reconciliation stock_clip_id"
    ),
    "accepted_eligibility_semantics": (
        "Phase 1 copies already-solved locations from an older occurrence or "
        "the final review corpus. It does not invent new GPS."
    ),
}

PHASE2_MUTATION_UNIVERSE = {
    "universe_name": "phase2_source_identity_propagation_targets",
    "dedupe_key": ["stockify_run_id", "stock_clip_id"],
    "latest_run_semantics": (
        "write target is the live latest accepted occurrence of each "
        "safe_to_inherit source-identity stock_clip_id"
    ),
    "accepted_eligibility_semantics": (
        "Phase 2 copies a same-stem donor's existing location_json onto the "
        "latest unresolved occurrence. Ambiguous source-identity rows are "
        "excluded. Candidate-level location remains authoritative."
    ),
}

ALL_CANDIDATES_UNIVERSE = {
    "universe_name": "all_stock_candidates_rows",
    "dedupe_key": ["stockify_run_id", "stock_clip_id"],
}


@dataclass
class HistoricalLocationPropagateReport:
    mode: str = MODE
    read_only: bool = True
    dry_run: bool = True
    phase: int = 1
    db_path: str = ""
    reconciliation_path: str = ""
    source_identity_path: str = ""
    backup_path: str | None = None
    phase1_targets: int = 0
    phase2_targets: int = 0
    matched_candidates: int = 0
    missing_candidates: int = 0
    safe_to_restore: int = 0
    already_applied: int = 0
    stronger_current_evidence: int = 0
    conflicting_current_evidence: int = 0
    malformed_historical_recovery: int = 0
    ambiguous_excluded: int = 0
    ambiguous_excluded_ids: list[str] = field(default_factory=list)
    by_bucket: dict[str, int] = field(default_factory=dict)
    by_safety_class: dict[str, int] = field(default_factory=dict)
    by_recovery_reason: dict[str, int] = field(default_factory=dict)
    candidate_universe: dict[str, Any] = field(default_factory=dict)
    coverage_before: dict[str, Any] = field(default_factory=dict)
    coverage_after: dict[str, Any] = field(default_factory=dict)
    locations_grouped_by_count: list[dict[str, Any]] = field(default_factory=list)
    library_8_6_26: dict[str, Any] = field(default_factory=dict)
    source_identity: dict[str, Any] = field(default_factory=dict)
    post_write_audit: dict[str, Any] = field(default_factory=dict)
    write_blocked_reason: str | None = None
    mutations: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "read_only": self.read_only,
            "dry_run": self.dry_run,
            "phase": self.phase,
            "did_not": [
                "scan /Volumes",
                "rerun srt or jpg inference",
                "mutate fcpxml",
                *(["mutate sqlite"] if self.read_only else []),
            ],
            "db_path": self.db_path,
            "reconciliation_path": self.reconciliation_path,
            "source_identity_path": self.source_identity_path,
            "backup_path": self.backup_path,
            "phase1_targets": self.phase1_targets,
            "phase2_targets": self.phase2_targets,
            "matched_candidates": self.matched_candidates,
            "missing_candidates": self.missing_candidates,
            "safe_to_restore": self.safe_to_restore,
            "already_applied": self.already_applied,
            "stronger_current_evidence": self.stronger_current_evidence,
            "conflicts": self.conflicting_current_evidence,
            "conflicting_current_evidence": self.conflicting_current_evidence,
            "malformed_historical_recovery": self.malformed_historical_recovery,
            "missing": self.missing_candidates,
            "ambiguous_excluded": self.ambiguous_excluded,
            "ambiguous_excluded_ids": list(self.ambiguous_excluded_ids),
            "by_bucket": dict(self.by_bucket),
            "by_safety_class": dict(self.by_safety_class),
            "by_recovery_reason": dict(self.by_recovery_reason),
            "candidate_universe": dict(self.candidate_universe),
            "coverage_before": dict(self.coverage_before),
            "coverage_after": dict(self.coverage_after),
            "locations_grouped_by_count": list(self.locations_grouped_by_count),
            "library_8_6_26": dict(self.library_8_6_26),
            "source_identity": dict(self.source_identity),
            "post_write_audit": dict(self.post_write_audit),
            "write_blocked_reason": self.write_blocked_reason,
            "mutations": [
                {key: value for key, value in item.items() if not str(key).startswith("_")}
                for item in self.mutations
            ],
            "warnings": list(self.warnings),
        }


class HistoricalLocationPropagateService:
    """Validate Phase-1 historical location copies and optionally persist them."""

    def __init__(
        self,
        repository: CatalogRepository,
        catalog: WorkflowCatalog | None = None,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.repository = repository
        self.catalog = catalog
        self._announce = progress or (lambda _message: None)

    def validate(
        self,
        *,
        phase: int = 1,
        reconciliation_path: Path | None = None,
        source_identity_path: Path | None = None,
    ) -> HistoricalLocationPropagateReport:
        if phase == 2:
            if source_identity_path is None:
                raise VClipError("Phase 2 requires --source-identity-safety")
            return self._validate_phase2(source_identity_path=source_identity_path)
        if reconciliation_path is None:
            raise VClipError("Phase 1 requires --reconciliation")
        payload = load_reconciliation(reconciliation_path)
        phase1_clips = [
            clip
            for clip in payload["clips"]
            if str(clip.get("exclusive_bucket") or "") in PHASE1_BUCKETS
        ]
        source_clips = [
            clip
            for clip in payload["clips"]
            if str(clip.get("exclusive_bucket") or "") == SOURCE_IDENTITY_BUCKET
        ]
        self._announce(
            f"Loaded {len(phase1_clips)} Phase-1 targets and "
            f"{len(source_clips)} source-identity cases (read-only)."
        )
        latest_by_clip = self._load_latest_universe()
        occurrences = self._load_occurrences(
            {str(clip.get("stock_clip_id") or "") for clip in phase1_clips}
        )
        mutations = [
            self._classify_phase1_row(clip, latest_by_clip, occurrences)
            for clip in phase1_clips
        ]
        source_identity = self._analyze_source_identity(source_clips, latest_by_clip)
        report = self._build_report(
            phase=1,
            reconciliation_path=reconciliation_path,
            source_identity_path=None,
            mutations=mutations,
            latest_by_clip=latest_by_clip,
            source_identity=source_identity,
            read_only=True,
            dry_run=True,
        )
        return report

    def _validate_phase2(
        self,
        *,
        source_identity_path: Path,
    ) -> HistoricalLocationPropagateReport:
        payload = load_source_identity_safety(source_identity_path)
        journal_rows = list(payload.get("rows") or [])
        excluded = [
            row
            for row in journal_rows
            if str(row.get("safety_class") or "") != SAFE_TO_INHERIT
        ]
        journal_safe = [
            row
            for row in journal_rows
            if str(row.get("safety_class") or "") == SAFE_TO_INHERIT
        ]
        excluded_ids = [str(row.get("stock_clip_id") or "") for row in excluded]
        self._announce(
            f"Loaded {len(journal_safe)} Phase-2 safe_to_inherit targets; "
            f"excluded {len(excluded)} non-safe source-identity rows (read-only)."
        )
        latest_by_clip = self._load_latest_universe()
        donors_by_stem = self._load_resolved_by_stem(
            {
                str(
                    (latest_by_clip.get(str(row.get("stock_clip_id") or "")) or {}).get(
                        "source_normalized_stem"
                    )
                    or row.get("source_stem")
                    or ""
                )
                for row in journal_safe
            }
        )
        mutations = []
        for row in journal_safe:
            identity = self._classify_source_identity_row(
                row, latest_by_clip, donors_by_stem
            )
            mutations.append(
                self._mutation_from_source_identity(
                    identity, latest_by_clip.get(str(row.get("stock_clip_id") or ""))
                )
            )
        source_identity = {
            "mode": SOURCE_IDENTITY_MODE,
            "read_only": True,
            "phase": 2,
            "input_rows": len(journal_rows),
            "journal_safe_to_inherit": len(journal_safe),
            "ambiguous_excluded": sum(
                1 for row in excluded if str(row.get("safety_class")) == AMBIGUOUS_SOURCE
            ),
            "ambiguous_excluded_ids": [
                str(row.get("stock_clip_id") or "")
                for row in excluded
                if str(row.get("safety_class")) == AMBIGUOUS_SOURCE
            ],
            "other_excluded_ids": [
                str(row.get("stock_clip_id") or "")
                for row in excluded
                if str(row.get("safety_class")) != AMBIGUOUS_SOURCE
            ],
            "note": (
                "Phase 2 writes only live-revalidated safe_to_inherit rows. "
                "Ambiguous source-identity cases stay untouched."
            ),
        }
        report = self._build_report(
            phase=2,
            reconciliation_path=None,
            source_identity_path=source_identity_path,
            mutations=mutations,
            latest_by_clip=latest_by_clip,
            source_identity=source_identity,
            read_only=True,
            dry_run=True,
        )
        report.ambiguous_excluded = len(source_identity["ambiguous_excluded_ids"])
        report.ambiguous_excluded_ids = list(source_identity["ambiguous_excluded_ids"])
        if excluded_ids and not report.warnings:
            report.warnings.append(
                f"Excluded {len(excluded_ids)} non-safe source-identity journal "
                f"row(s) from Phase 2: {', '.join(id for id in excluded_ids if id)}"
            )
        return report

    def propagate(
        self,
        *,
        phase: int = 1,
        reconciliation_path: Path | None = None,
        source_identity_path: Path | None = None,
        write: bool = False,
        backup_path: Path | None = None,
        fail_after: int | None = None,
    ) -> HistoricalLocationPropagateReport:
        report = self.validate(
            phase=phase,
            reconciliation_path=reconciliation_path,
            source_identity_path=source_identity_path,
        )
        if not write:
            return report
        blocked = self._write_block_reason(report)
        if blocked:
            report.write_blocked_reason = blocked
            raise VClipError(blocked)
        db_path = Path(self.repository.database.path)
        prefix = (
            "pre-location-propagate-phase2" if phase == 2 else "pre-location-propagate"
        )
        created_backup = create_pre_restore_backup(
            db_path,
            backup_path=backup_path,
            name_prefix=prefix,
        )
        report.backup_path = str(created_backup)
        self._announce(f"Backup written: {created_backup}")
        to_write = [
            mutation
            for mutation in report.mutations
            if mutation["safety_class"] == SAFE_TO_RESTORE
        ]
        recoveries = [_recovery_from_mutation(mutation) for mutation in to_write]
        before = self._fingerprint_all_candidates()
        sessions_before = self._fingerprint_sessions()
        recoveries_before = self._recovery_table_snapshot()
        write_mode = PHASE2_WRITE_MODE if phase == 2 else WRITE_MODE
        try:
            self._persist_propagate(
                recoveries,
                mutations=to_write,
                fail_after=fail_after,
                persist_sessions=phase == 1,
            )
        except Exception:
            report.mode = write_mode
            report.read_only = False
            report.dry_run = False
            report.backup_path = str(created_backup)
            raise
        after = self._fingerprint_all_candidates()
        sessions_after = self._fingerprint_sessions()
        recoveries_after = self._recovery_table_snapshot()
        written_keys = {
            (item.stockify_run_id, item.stock_clip_id) for item in recoveries
        }
        latest_after = self._load_latest_universe()
        report.mode = write_mode
        report.read_only = False
        report.dry_run = False
        report.backup_path = str(created_backup)
        report.coverage_after = _latest_coverage(
            latest_after,
            projected_safe=0,
            after_write=True,
        )
        report.post_write_audit = self._post_write_audit(
            mutations=report.mutations,
            written_keys=written_keys,
            before=before,
            after=after,
            recoveries_before=recoveries_before,
            recoveries_after=recoveries_after,
            phase=phase,
            sessions_before=sessions_before,
            sessions_after=sessions_after,
            excluded_ids=set(report.ambiguous_excluded_ids),
        )
        return report

    def _write_block_reason(self, report: HistoricalLocationPropagateReport) -> str | None:
        if report.missing_candidates:
            return (
                "Refusing historical location propagation: "
                f"{report.missing_candidates} missing candidate(s)."
            )
        if report.conflicting_current_evidence:
            return (
                "Refusing historical location propagation: "
                f"{report.conflicting_current_evidence} conflicting_current_evidence row(s)."
            )
        if report.malformed_historical_recovery:
            return (
                "Refusing historical location propagation: "
                f"{report.malformed_historical_recovery} malformed_historical_recovery row(s)."
            )
        return None

    def _persist_propagate(
        self,
        recoveries: list[LocationRecoveryRow],
        *,
        mutations: list[dict[str, Any]],
        fail_after: int | None = None,
        persist_sessions: bool = True,
    ) -> None:
        session_summaries = (
            _conservative_session_summaries(mutations) if persist_sessions else []
        )
        catalog = self.catalog or WorkflowCatalog(self.repository.database)
        with self.repository.database.transaction() as connection:
            for index, recovery in enumerate(recoveries, start=1):
                persist_review_location_candidate_updates(connection, [recovery])
                if fail_after is not None and index >= fail_after:
                    raise RuntimeError("injected propagate failure")
            catalog.record_review_location_recoveries(
                recoveries=recoveries,
                connection=connection,
            )
            if persist_sessions:
                _persist_session_summaries(connection, session_summaries)

    def _classify_phase1_row(
        self,
        clip: dict[str, Any],
        latest_by_clip: dict[str, dict[str, Any]],
        occurrences: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        clip_id = str(clip.get("stock_clip_id") or "")
        bucket = str(clip.get("exclusive_bucket") or "")
        journal_run_id = str(clip.get("current_run_id") or "")
        latest = latest_by_clip.get(clip_id)
        clip_occurrences = occurrences.get(clip_id) or []
        proposed, provenance_notes, malformed_reason, recovery_reason = _proposed_phase1(
            clip, latest, clip_occurrences
        )
        current_location = dict((latest or {}).get("location") or {})
        current_event = str((latest or {}).get("generated_event_name") or "")
        current_project = str((latest or {}).get("generated_clip_project_name") or "")
        current_rank, current_kind = current_evidence_rank(
            current_location,
            recovery_reason=(latest or {}).get("existing_recovery_reason"),
        )
        historical_rank, historical_kind = current_evidence_rank(proposed)
        if historical_rank == RANK_UNKNOWN and location_is_resolved(proposed):
            historical_kind = "named_place_without_gps"
        safety = SAFE_TO_RESTORE
        safety_detail = "existing solved location is strictly stronger than current catalog state"
        run_id = str((latest or {}).get("run_id") or journal_run_id)
        if not clip_id:
            safety = MALFORMED
            safety_detail = "missing stock_clip_id"
        elif latest is None:
            safety = MISSING_CANDIDATE
            safety_detail = "no latest accepted stock_candidates row for stock_clip_id"
        elif malformed_reason:
            safety = MALFORMED
            safety_detail = malformed_reason
        elif _already_applied(latest, proposed):
            safety = ALREADY_APPLIED
            safety_detail = "latest row already carries the proposed location"
        elif current_rank > historical_rank:
            safety = STRONGER_CURRENT
            safety_detail = (
                f"current {current_kind} outranks historical {historical_kind}"
            )
        elif _location_conflict(current_location, proposed, current_event=current_event):
            safety = CONFLICTING_CURRENT
            safety_detail = "current catalog geography conflicts with historical location"
        elif current_rank == historical_rank and current_rank > RANK_UNKNOWN:
            if not _locations_equivalent(current_location, proposed):
                safety = CONFLICTING_CURRENT
                safety_detail = (
                    f"current {current_kind} conflicts with historical {historical_kind}"
                )
        elif current_rank == RANK_UNKNOWN:
            safety = SAFE_TO_RESTORE
            safety_detail = "current catalog location is unknown/empty"
        elif current_rank < historical_rank:
            safety = SAFE_TO_RESTORE
            safety_detail = (
                f"historical {historical_kind} outranks current {current_kind}"
            )

        new_event, new_project = _proposed_names(clip, latest, proposed, clip_occurrences)
        location_label = display_location_label(proposed) or "Unknown"
        return {
            "run_id": run_id,
            "stockify_run_id": run_id,
            "stock_clip_id": clip_id,
            "session_id": (latest or {}).get("session_id"),
            "library": (latest or {}).get("library") or clip.get("library"),
            "source_media": (latest or {}).get("source_filename") or clip.get("source_filename"),
            "source_stem": (latest or {}).get("source_normalized_stem")
            or clip.get("source_stem"),
            "eligibility_status": (latest or {}).get("eligibility_status"),
            "reconciliation_bucket": bucket,
            "recovery_reason": recovery_reason,
            "confidence": str(proposed.get("confidence") or "high"),
            "journal_run_id": journal_run_id,
            "journal_run_matches_latest": bool(latest) and journal_run_id == run_id,
            "current_generated_event_name": current_event,
            "current_generated_project_name": current_project,
            "current_location": current_location,
            "current_evidence_kind": current_kind,
            "current_evidence_rank": current_rank,
            "historical_evidence_kind": historical_kind,
            "historical_evidence_rank": historical_rank,
            "old_location_snapshot": {
                "generated_event_name": current_event,
                "generated_project_name": current_project,
                "location": current_location,
            },
            "proposed_location_snapshot": {
                "generated_event_name": new_event,
                "generated_project_name": new_project,
                "location": proposed,
            },
            "proposed_location_label": location_label,
            "representative_lat": proposed.get("center_lat"),
            "representative_lon": proposed.get("center_lon"),
            "safety_class": safety,
            "safety_detail": safety_detail,
            "provenance_notes": provenance_notes,
            "capture_date": (latest or {}).get("session_capture_date")
            or clip.get("capture_date"),
            "matched": latest is not None,
            "is_8_6_26": _is_8_6_26(
                library=(latest or {}).get("library") or clip.get("library"),
                source_event=(latest or {}).get("source_event_name"),
                source_xml=(latest or {}).get("source_xml_path"),
            ),
            "_recovery_row": LocationRecoveryRow(
                stockify_run_id=run_id,
                stock_clip_id=clip_id,
                original_event_name=current_event,
                new_event_name=new_event,
                original_project_name=current_project,
                new_project_name=new_project,
                source_media=(latest or {}).get("source_filename"),
                srt_paths=[],
                representative_lat=_optional_float(proposed.get("center_lat")),
                representative_lon=_optional_float(proposed.get("center_lon")),
                resolution_confidence=str(proposed.get("confidence") or "high"),
                recovery_reason=recovery_reason,
                source_shard="historical_location_propagation",
                input_xml=str((latest or {}).get("source_xml_path") or ""),
                output_xml=None,
                provenance={
                    "location": proposed,
                    "generated_project_label": project_base_label(
                        proposed,
                        {"label": "unknown"},
                    ),
                    "propagation": (proposed.get("propagation") or {}),
                    "evidence_sources": list(proposed.get("evidence_sources") or []),
                    "recovery_reason": recovery_reason,
                    "direct_source_gps": proposed.get("direct_source_gps"),
                    "gps_kind": proposed.get("gps_kind"),
                },
            ),
        }

    def _mutation_from_source_identity(
        self,
        identity: dict[str, Any],
        latest: dict[str, Any] | None,
    ) -> dict[str, Any]:
        clip_id = str(identity.get("stock_clip_id") or "")
        run_id = str((latest or {}).get("run_id") or identity.get("stockify_run_id") or "")
        current_location = dict((latest or {}).get("location") or {})
        current_event = str((latest or {}).get("generated_event_name") or "")
        current_project = str((latest or {}).get("generated_clip_project_name") or "")
        proposed = preserve_jpg_inference(
            copy.deepcopy(identity.get("proposed_location") or {})
        )
        current_rank, current_kind = current_evidence_rank(
            current_location,
            recovery_reason=(latest or {}).get("existing_recovery_reason"),
        )
        historical_rank, historical_kind = current_evidence_rank(proposed)
        identity_class = str(identity.get("safety_class") or "")
        safety, safety_detail = _phase2_safety_class(
            identity_class=identity_class,
            identity_detail=str(identity.get("safety_detail") or ""),
            current_rank=current_rank,
            current_kind=current_kind,
            historical_rank=historical_rank,
            historical_kind=historical_kind,
            latest=latest,
            proposed=proposed,
            current_location=current_location,
            current_event=current_event,
        )
        capture_date = str(
            (latest or {}).get("session_capture_date") or identity.get("capture_date") or ""
        )
        donor_event = str(identity.get("donor_generated_event_name") or "")
        donor_project = str(identity.get("donor_generated_project_name") or "")
        new_event = donor_event if donor_event and not is_unknown_event_name(donor_event) else (
            event_base_name(proposed, {"date": capture_date or "Unknown Date"})
            if proposed
            else current_event
        )
        new_base = project_base_label(proposed, {"label": "unknown"}) if proposed else current_project
        new_project = donor_project or _relabel_project(current_project, new_base)
        jpg_ok = not _is_jpg_location(proposed) or (
            proposed.get("direct_source_gps") is False
            and proposed.get("gps_kind") == GPS_KIND_JPG
        )
        if not jpg_ok and safety == SAFE_TO_RESTORE:
            safety = MALFORMED
            safety_detail = "JPG donor location must not claim direct_source_gps"
        return {
            "run_id": run_id,
            "stockify_run_id": run_id,
            "stock_clip_id": clip_id,
            "session_id": (latest or {}).get("session_id"),
            "library": (latest or {}).get("library") or identity.get("library"),
            "source_media": (latest or {}).get("source_filename")
            or identity.get("source_filename"),
            "source_stem": identity.get("source_stem"),
            "identity_kind": identity.get("identity_kind"),
            "eligibility_status": (latest or {}).get("eligibility_status"),
            "reconciliation_bucket": SOURCE_IDENTITY_BUCKET,
            "recovery_reason": REASON_SOURCE_IDENTITY,
            "confidence": str(proposed.get("confidence") or "high"),
            "donor_run_id": identity.get("donor_run_id"),
            "donor_stock_clip_id": identity.get("donor_stock_clip_id"),
            "primary_evidence_kind": identity.get("primary_evidence_kind"),
            "current_generated_event_name": current_event,
            "current_generated_project_name": current_project,
            "current_location": current_location,
            "current_evidence_kind": current_kind,
            "current_evidence_rank": current_rank,
            "historical_evidence_kind": historical_kind,
            "historical_evidence_rank": historical_rank,
            "old_location_snapshot": {
                "generated_event_name": current_event,
                "generated_project_name": current_project,
                "location": current_location,
            },
            "proposed_location_snapshot": {
                "generated_event_name": new_event,
                "generated_project_name": new_project,
                "location": proposed,
            },
            "proposed_location_label": display_location_label(proposed) or "Unknown",
            "representative_lat": proposed.get("center_lat"),
            "representative_lon": proposed.get("center_lon"),
            "safety_class": safety,
            "safety_detail": safety_detail,
            "provenance_notes": [
                (
                    "inherited existing historical source-location knowledge from "
                    f"stem {identity.get('source_stem')} donor "
                    f"{identity.get('donor_run_id')}/{identity.get('donor_stock_clip_id')}"
                )
            ],
            "capture_date": capture_date,
            "matched": latest is not None,
            "is_8_6_26": _is_8_6_26(
                library=(latest or {}).get("library") or identity.get("library"),
                source_event=(latest or {}).get("source_event_name"),
                source_xml=(latest or {}).get("source_xml_path"),
            ),
            "journal_run_matches_latest": bool(latest)
            and str(identity.get("stockify_run_id") or "") == run_id,
            "_recovery_row": LocationRecoveryRow(
                stockify_run_id=run_id,
                stock_clip_id=clip_id,
                original_event_name=current_event,
                new_event_name=new_event,
                original_project_name=current_project,
                new_project_name=new_project,
                source_media=(latest or {}).get("source_filename")
                or identity.get("source_filename"),
                srt_paths=[],
                representative_lat=_optional_float(proposed.get("center_lat")),
                representative_lon=_optional_float(proposed.get("center_lon")),
                resolution_confidence=str(proposed.get("confidence") or "high"),
                recovery_reason=REASON_SOURCE_IDENTITY,
                source_shard="source_identity_propagation",
                input_xml=str((latest or {}).get("source_xml_path") or ""),
                output_xml=None,
                provenance={
                    "location": proposed,
                    "generated_project_label": new_base,
                    "propagation": (proposed.get("propagation") or {}),
                    "evidence_sources": list(proposed.get("evidence_sources") or []),
                    "recovery_reason": REASON_SOURCE_IDENTITY,
                    "direct_source_gps": proposed.get("direct_source_gps"),
                    "gps_kind": proposed.get("gps_kind"),
                    "source_stem": identity.get("source_stem"),
                    "donor_run_id": identity.get("donor_run_id"),
                    "donor_stock_clip_id": identity.get("donor_stock_clip_id"),
                    "donor_evidence_kind": identity.get("primary_evidence_kind"),
                },
            ),
        }

    def _analyze_source_identity(
        self,
        clips: list[dict[str, Any]],
        latest_by_clip: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        donors_by_stem = self._load_resolved_by_stem(
            {
                str(
                    (latest_by_clip.get(str(clip.get("stock_clip_id") or "")) or {}).get(
                        "source_normalized_stem"
                    )
                    or clip.get("source_stem")
                    or ""
                )
                for clip in clips
            }
        )
        for clip in clips:
            rows.append(
                self._classify_source_identity_row(
                    clip, latest_by_clip, donors_by_stem
                )
            )
        by_class = Counter(str(row["safety_class"]) for row in rows)
        safe_rows = [row for row in rows if row["safety_class"] == SAFE_TO_INHERIT]
        evidence = Counter(str(row["primary_evidence_kind"]) for row in safe_rows)
        identity = Counter(str(row["identity_kind"]) for row in safe_rows)
        locations = Counter(str(row["proposed_location_label"]) for row in safe_rows)
        return {
            "mode": SOURCE_IDENTITY_MODE,
            "read_only": True,
            "did_not_write": True,
            "note": (
                "Source-identity cases are not written by Phase 1. Safe means a "
                "later pass could inherit the donor's real provenance; it does "
                "not mean every geographically-consistent audit row is safe."
            ),
            "targets": len(rows),
            "safe_to_inherit": by_class.get(SAFE_TO_INHERIT, 0),
            "ambiguous": by_class.get(AMBIGUOUS_SOURCE, 0),
            "conflicting": by_class.get(CONFLICTING_SOURCE, 0),
            "missing_donor": by_class.get(MISSING_SOURCE_DONOR, 0),
            "missing_candidate": by_class.get(MISSING_CANDIDATE, 0),
            "by_safety_class": dict(by_class),
            "safe_primary_evidence": dict(sorted(evidence.items())),
            "safe_identity_kind": dict(sorted(identity.items())),
            "safe_original_evidence_sources": dict(
                sorted(
                    Counter(
                        source
                        for row in safe_rows
                        for source in row.get("donor_evidence_kinds") or []
                    ).items()
                )
            ),
            "safe_locations_grouped_by_count": _grouped_labels(locations),
            "rows": rows,
        }

    def _classify_source_identity_row(
        self,
        clip: dict[str, Any],
        latest_by_clip: dict[str, dict[str, Any]],
        donors_by_stem: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        clip_id = str(clip.get("stock_clip_id") or "")
        latest = latest_by_clip.get(clip_id)
        stem = str(
            (latest or {}).get("source_normalized_stem") or clip.get("source_stem") or ""
        )
        filename = str(
            (latest or {}).get("source_filename") or clip.get("source_filename") or ""
        )
        current_location = dict((latest or {}).get("location") or {})
        dji = parse_dji_file_identity(filename) or parse_dji_file_identity(stem)
        identity_kind = "dji_datetime" if dji is not None else "short_dji_stem"
        donors = [
            row
            for row in (donors_by_stem.get(stem) or [])
            if not (
                str(row.get("stock_clip_id")) == clip_id
                and str(row.get("run_id")) == str((latest or {}).get("run_id") or "")
            )
        ]
        donor_locations = [dict(row.get("location") or {}) for row in donors]
        labels = sorted(
            {
                display_location_label(location)
                for location in donor_locations
                if display_location_label(location)
            }
        )
        cities = sorted(
            {
                clean_label(location.get("city"))
                for location in donor_locations
                if clean_label(location.get("city"))
            }
        )
        evidence_kinds = sorted(
            {
                str(source)
                for location in donor_locations
                for source in (location.get("evidence_sources") or [])
                if source
            }
        )
        strongest = None
        strongest_donor: dict[str, Any] | None = None
        strongest_rank = RANK_UNKNOWN
        strongest_kind = "unknown_or_empty"
        for row in donors:
            location = dict(row.get("location") or {})
            rank, kind = current_evidence_rank(location)
            if rank > strongest_rank:
                strongest_rank = rank
                strongest_kind = kind
                strongest = location
                strongest_donor = row
        gps_spread = _max_gps_spread_m(donor_locations)
        geo_ok = (
            bool(labels)
            and (len(cities) <= 1)
            and (gps_spread is None or gps_spread <= GPS_CONFLICT_METERS)
        )
        labels_agree = _labels_are_single_place(labels)
        has_strong = bool(set(evidence_kinds) & STRONG_EVIDENCE_SOURCES) or (
            strongest_rank >= RANK_JPG
        )
        safety = SAFE_TO_INHERIT
        detail = "stable source identity with geographically consistent strong evidence"
        if latest is None:
            safety = MISSING_CANDIDATE
            detail = "no latest accepted stock_candidates row for stock_clip_id"
        elif not stem or not donors:
            safety = MISSING_SOURCE_DONOR
            detail = "no resolved same-stem donor in the live catalog"
        elif gps_spread is not None and gps_spread > GPS_CONFLICT_METERS:
            safety = CONFLICTING_SOURCE
            detail = (
                f"same-stem donors are {round(gps_spread)}m apart; refusing inherit"
            )
        elif len(cities) > 1:
            safety = CONFLICTING_SOURCE
            detail = f"same-stem donors disagree on city: {cities}"
        elif not labels_agree:
            safety = AMBIGUOUS_SOURCE
            detail = f"same-stem donors have distinct place labels: {labels}"
        elif not geo_ok:
            safety = AMBIGUOUS_SOURCE
            detail = "same-stem donors are not geographically consistent in the live catalog"
        elif not has_strong:
            safety = AMBIGUOUS_SOURCE
            detail = "donors lack manual/SRT/trajectory/JPG evidence"
        elif location_is_resolved(current_location) and strongest is not None:
            if _location_conflict(current_location, strongest):
                safety = CONFLICTING_SOURCE
                detail = "current latest geography conflicts with same-stem donors"
            else:
                safety = ALREADY_APPLIED
                detail = "latest row is already resolved consistently with donors"

        donor_run_id = str((strongest_donor or {}).get("run_id") or "")
        donor_clip_id = str((strongest_donor or {}).get("stock_clip_id") or "")
        proposed = (
            attach_propagation(
                preserve_jpg_inference(copy.deepcopy(strongest or {})),
                inherited_from="source_identity_resolved_occurrence",
                bucket=SOURCE_IDENTITY_BUCKET,
                source_run_id=donor_run_id,
                source_stock_clip_id=donor_clip_id,
                extra_notes=(
                    "Latest candidate inherited existing historical "
                    "source-location knowledge from a same-stem donor. "
                    "Not new inference."
                ),
                extra={
                    "source_stem": stem,
                    "identity_kind": identity_kind,
                    "donor_run_id": donor_run_id,
                    "donor_stock_clip_id": donor_clip_id,
                    "donor_evidence_kind": strongest_kind,
                    "inherited_existing_historical_source_location": True,
                },
            )
            if strongest
            else {}
        )
        return {
            "stock_clip_id": clip_id,
            "stockify_run_id": str((latest or {}).get("run_id") or ""),
            "source_stem": stem,
            "source_filename": filename,
            "identity_kind": identity_kind,
            "identity_stable": dji is not None,
            "library": (latest or {}).get("library") or clip.get("library"),
            "donor_count": len(donors),
            "donor_run_id": donor_run_id,
            "donor_stock_clip_id": donor_clip_id,
            "donor_generated_event_name": str(
                (strongest_donor or {}).get("generated_event_name") or ""
            ),
            "donor_generated_project_name": str(
                (strongest_donor or {}).get("generated_clip_project_name") or ""
            ),
            "donor_stock_clip_ids": sorted(
                {str(row.get("stock_clip_id")) for row in donors if row.get("stock_clip_id")}
            ),
            "donor_labels": labels,
            "donor_evidence_kinds": evidence_kinds,
            "primary_evidence_kind": strongest_kind,
            "gps_spread_meters": None if gps_spread is None else round(gps_spread, 1),
            "geographically_consistent_live": geo_ok and labels_agree,
            "audit_geographically_consistent": bool(
                (clip.get("source_identity") or {}).get("geographically_consistent")
            ),
            "safety_class": safety,
            "safety_detail": detail,
            "proposed_location_label": display_location_label(proposed) or None,
            "proposed_location": proposed or None,
            "would_write": False,
        }

    def _build_report(
        self,
        *,
        phase: int,
        reconciliation_path: Path | None,
        source_identity_path: Path | None,
        mutations: list[dict[str, Any]],
        latest_by_clip: dict[str, dict[str, Any]],
        source_identity: dict[str, Any],
        read_only: bool,
        dry_run: bool,
    ) -> HistoricalLocationPropagateReport:
        by_class = Counter(str(item["safety_class"]) for item in mutations)
        by_bucket = Counter(str(item["reconciliation_bucket"] or "") for item in mutations)
        by_reason = Counter(str(item["recovery_reason"] or "") for item in mutations)
        safe_labels = Counter(
            str(item["proposed_location_label"])
            for item in mutations
            if item["safety_class"] == SAFE_TO_RESTORE
        )
        safe_count = by_class.get(SAFE_TO_RESTORE, 0)
        coverage_before = _latest_coverage(latest_by_clip, projected_safe=0)
        coverage_after = _latest_coverage(latest_by_clip, projected_safe=safe_count)
        source_identity = dict(source_identity)
        if phase == 1:
            source_safe = int(source_identity.get("safe_to_inherit") or 0)
            coverage_if_source = _latest_coverage(
                latest_by_clip,
                projected_safe=safe_count + source_safe,
            )
            source_identity["projected_unresolved_if_safe_later_propagated"] = (
                coverage_if_source.get("projected_unresolved")
            )
            source_identity["projected_resolved_if_safe_later_propagated"] = (
                coverage_if_source.get("projected_resolved")
            )
        evidence = Counter(
            str(item.get("primary_evidence_kind") or item.get("historical_evidence_kind") or "")
            for item in mutations
            if item["safety_class"] == SAFE_TO_RESTORE
        )
        if phase == 2:
            source_identity["live_safe_to_restore"] = safe_count
            source_identity["live_primary_evidence"] = dict(sorted(evidence.items()))
        serializable = [_public_mutation(item) for item in mutations]
        report = HistoricalLocationPropagateReport(
            mode=PHASE2_MODE if phase == 2 else MODE,
            read_only=read_only,
            dry_run=dry_run,
            phase=phase,
            db_path=str(self.repository.database.path),
            reconciliation_path=str(reconciliation_path) if reconciliation_path else "",
            source_identity_path=(
                str(source_identity_path) if source_identity_path else ""
            ),
            phase1_targets=len(mutations) if phase == 1 else 0,
            phase2_targets=len(mutations) if phase == 2 else 0,
            matched_candidates=sum(1 for item in mutations if item["matched"]),
            missing_candidates=by_class.get(MISSING_CANDIDATE, 0),
            safe_to_restore=safe_count,
            already_applied=by_class.get(ALREADY_APPLIED, 0),
            stronger_current_evidence=by_class.get(STRONGER_CURRENT, 0),
            conflicting_current_evidence=by_class.get(CONFLICTING_CURRENT, 0),
            malformed_historical_recovery=by_class.get(MALFORMED, 0),
            by_bucket=dict(sorted(by_bucket.items())),
            by_safety_class={key: by_class.get(key, 0) for key in SAFETY_CLASSES},
            by_recovery_reason=dict(sorted(by_reason.items())),
            candidate_universe=dict(
                PHASE2_MUTATION_UNIVERSE if phase == 2 else PHASE1_MUTATION_UNIVERSE
            ),
            coverage_before=coverage_before,
            coverage_after=coverage_after,
            locations_grouped_by_count=_grouped_labels(safe_labels),
            library_8_6_26=_library_8_6_26_section(mutations),
            source_identity=source_identity,
            mutations=serializable,
        )
        for public, original in zip(report.mutations, mutations, strict=True):
            public["_recovery_row"] = original["_recovery_row"]
            public["_session_id"] = original.get("session_id")
        return report

    def _load_latest_universe(self) -> dict[str, dict[str, Any]]:
        connection = self._readonly_connect()
        try:
            rows = connection.execute(
                """
                WITH ranked AS (
                  SELECT
                    c.stock_clip_id,
                    c.run_id,
                    c.session_id,
                    c.eligibility_status,
                    c.location_json,
                    c.generated_event_name,
                    c.generated_clip_project_name,
                    c.source_name,
                    c.original_duration_seconds,
                    c.proposed_duration_seconds,
                    c.updated_at,
                    e.source_name AS source_event_name,
                    m.original_filename AS source_filename,
                    m.normalized_stem AS source_normalized_stem,
                    s.capture_date AS session_capture_date,
                    r.started_at AS run_started_at,
                    r.source_xml_path,
                    rec.recovery_reason AS existing_recovery_reason,
                    ROW_NUMBER() OVER (
                      PARTITION BY c.stock_clip_id
                      ORDER BY r.started_at DESC, c.updated_at DESC, c.run_id DESC
                    ) AS rn
                  FROM stock_candidates c
                  JOIN stockify_runs r ON r.id=c.run_id
                  JOIN source_projects p ON p.id=c.source_project_id
                  JOIN source_events e ON e.id=p.source_event_id
                  LEFT JOIN source_media m ON m.id=c.source_media_id
                  LEFT JOIN shoot_sessions s ON s.id=c.session_id
                  LEFT JOIN review_location_recoveries rec
                    ON rec.stockify_run_id=c.run_id AND rec.stock_clip_id=c.stock_clip_id
                  WHERE c.eligibility_status='accepted'
                )
                SELECT * FROM ranked WHERE rn=1
                """
            ).fetchall()
        finally:
            connection.close()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            item["location"] = json_loads(item.pop("location_json"), {})
            item["library"] = library_from_source_xml(item.get("source_xml_path"))
            item["duration_seconds"] = float(
                item.get("proposed_duration_seconds")
                or item.get("original_duration_seconds")
                or 0.0
            )
            item["resolved"] = location_is_resolved(item["location"])
            latest[str(item["stock_clip_id"])] = item
        return latest

    def _load_occurrences(
        self, clip_ids: set[str]
    ) -> dict[str, list[dict[str, Any]]]:
        unique = sorted(clip_id for clip_id in clip_ids if clip_id)
        if not unique:
            return {}
        placeholders = ", ".join("?" for _ in unique)
        connection = self._readonly_connect()
        try:
            rows = connection.execute(
                f"""
                SELECT
                    c.stock_clip_id,
                    c.run_id,
                    c.session_id,
                    c.eligibility_status,
                    c.location_json,
                    c.generated_event_name,
                    c.generated_clip_project_name,
                    c.generated_project_label,
                    m.original_filename AS source_filename,
                    m.normalized_stem AS source_normalized_stem,
                    s.capture_date AS session_capture_date,
                    r.started_at AS run_started_at,
                    r.source_xml_path
                FROM stock_candidates c
                JOIN stockify_runs r ON r.id=c.run_id
                LEFT JOIN source_media m ON m.id=c.source_media_id
                LEFT JOIN shoot_sessions s ON s.id=c.session_id
                WHERE c.stock_clip_id IN ({placeholders})
                ORDER BY r.started_at DESC, c.updated_at DESC, c.run_id DESC
                """,
                unique,
            ).fetchall()
        finally:
            connection.close()
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            item = dict(row)
            item["location"] = json_loads(item.pop("location_json"), {})
            item["resolved"] = location_is_resolved(item["location"])
            grouped[str(item["stock_clip_id"])].append(item)
        return grouped

    def _load_resolved_by_stem(
        self, stems: set[str]
    ) -> dict[str, list[dict[str, Any]]]:
        unique = sorted(stem for stem in stems if stem)
        if not unique:
            return {}
        placeholders = ", ".join("?" for _ in unique)
        connection = self._readonly_connect()
        try:
            rows = connection.execute(
                f"""
                SELECT
                    c.stock_clip_id,
                    c.run_id,
                    c.location_json,
                    c.generated_event_name,
                    c.generated_clip_project_name,
                    m.original_filename AS source_filename,
                    m.normalized_stem AS source_normalized_stem,
                    r.started_at AS run_started_at
                FROM stock_candidates c
                JOIN stockify_runs r ON r.id=c.run_id
                LEFT JOIN source_media m ON m.id=c.source_media_id
                WHERE c.eligibility_status='accepted'
                  AND m.normalized_stem IN ({placeholders})
                """,
                unique,
            ).fetchall()
        finally:
            connection.close()
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            item = dict(row)
            item["location"] = json_loads(item.pop("location_json"), {})
            if not location_is_resolved(item["location"]):
                continue
            stem = str(item.get("source_normalized_stem") or "")
            grouped[stem].append(item)
        return grouped

    def _readonly_connect(self) -> sqlite3.Connection:
        path = Path(self.repository.database.path).expanduser().resolve()
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    def _fingerprint_all_candidates(self) -> dict[tuple[str, str], tuple[str, str, str]]:
        with self.repository.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT run_id, stock_clip_id, location_json,
                       generated_event_name, generated_clip_project_name
                FROM stock_candidates
                """
            ).fetchall()
        return {
            (str(row["run_id"]), str(row["stock_clip_id"])): (
                str(row["location_json"] or ""),
                str(row["generated_event_name"] or ""),
                str(row["generated_clip_project_name"] or ""),
            )
            for row in rows
        }

    def _fingerprint_sessions(self) -> dict[str, tuple[str, str, str]]:
        with self.repository.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, location_json, city, neighborhood
                FROM shoot_sessions
                """
            ).fetchall()
        return {
            str(row["id"]): (
                str(row["location_json"] or ""),
                str(row["city"] or ""),
                str(row["neighborhood"] or ""),
            )
            for row in rows
        }

    def _recovery_table_snapshot(self) -> dict[tuple[str, str], dict[str, Any]]:
        with self.repository.database.connect() as connection:
            try:
                rows = connection.execute(
                    """
                    SELECT stockify_run_id, stock_clip_id, recovery_reason,
                           provenance_json, representative_lat, representative_lon
                    FROM review_location_recoveries
                    """
                ).fetchall()
            except sqlite3.OperationalError:
                return {}
        snapshot: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            snapshot[(str(row["stockify_run_id"]), str(row["stock_clip_id"]))] = {
                "recovery_reason": row["recovery_reason"],
                "provenance": json_loads(row["provenance_json"], {}),
                "representative_lat": row["representative_lat"],
                "representative_lon": row["representative_lon"],
            }
        return snapshot

    def _post_write_audit(
        self,
        *,
        mutations: list[dict[str, Any]],
        written_keys: set[tuple[str, str]],
        before: dict[tuple[str, str], tuple[str, str, str]],
        after: dict[tuple[str, str], tuple[str, str, str]],
        recoveries_before: dict[tuple[str, str], dict[str, Any]],
        recoveries_after: dict[tuple[str, str], dict[str, Any]],
        phase: int = 1,
        sessions_before: dict[str, tuple[str, str, str]] | None = None,
        sessions_after: dict[str, tuple[str, str, str]] | None = None,
        excluded_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        unintended: list[str] = []
        for key, fingerprint in after.items():
            if key in written_keys:
                continue
            if before.get(key) != fingerprint:
                unintended.append(f"{key[0]}:{key[1]}")
        intended_missing = [
            f"{run}:{clip}"
            for run, clip in sorted(written_keys)
            if before.get((run, clip)) == after.get((run, clip))
        ]
        provenance_ok = True
        stronger_overwritten = []
        jpg_direct_flags = []
        excluded_written = []
        for item in mutations:
            key = (item["stockify_run_id"], item["stock_clip_id"])
            if item["stock_clip_id"] in (excluded_ids or set()) and key in written_keys:
                excluded_written.append(f"{key[0]}:{key[1]}")
            if item["safety_class"] != SAFE_TO_RESTORE:
                if key in written_keys:
                    stronger_overwritten.append(f"{key[0]}:{key[1]}")
                continue
            persisted = recoveries_after.get(key) or {}
            if persisted.get("recovery_reason") != item["recovery_reason"]:
                provenance_ok = False
            location = (persisted.get("provenance") or {}).get("location") or {}
            if PROPAGATION_EVIDENCE not in set(location.get("evidence_sources") or []):
                provenance_ok = False
            if not (location.get("propagation") or {}):
                provenance_ok = False
            if phase == 2:
                prop = location.get("propagation") or {}
                if not prop.get("source_stem") or not prop.get("donor_stock_clip_id"):
                    provenance_ok = False
                if not prop.get("inherited_existing_historical_source_location"):
                    provenance_ok = False
            if _is_jpg_location(location):
                if location.get("direct_source_gps") is not False:
                    jpg_direct_flags.append(f"{key[0]}:{key[1]}")
                if location.get("gps_kind") != GPS_KIND_JPG:
                    jpg_direct_flags.append(f"{key[0]}:{key[1]}:gps_kind")
        eight = [item for item in mutations if item.get("is_8_6_26")]
        session_changes = []
        for session_id, fingerprint in (sessions_after or {}).items():
            if (sessions_before or {}).get(session_id) != fingerprint:
                session_changes.append(session_id)
        return {
            "intended_rows_written": len(written_keys),
            "intended_rows_unchanged": intended_missing,
            "unintended_rows_changed": unintended,
            "unintended_change_universe": dict(ALL_CANDIDATES_UNIVERSE),
            "review_location_recoveries_before": len(recoveries_before),
            "review_location_recoveries_after": len(recoveries_after),
            "review_location_recoveries_upserted": len(written_keys),
            "provenance_round_trip_ok": provenance_ok,
            "non_safe_rows_written": stronger_overwritten,
            "jpg_direct_source_gps_violations": jpg_direct_flags,
            "library_8_6_26_written": sum(
                1
                for item in eight
                if (item["stockify_run_id"], item["stock_clip_id"]) in written_keys
                or item["safety_class"] == ALREADY_APPLIED
            ),
            "library_8_6_26_expected": len(eight),
            "source_identity_rows_written": (
                len(written_keys) if phase == 2 else 0
            ),
            "ambiguous_rows_written": excluded_written,
            "session_summaries_changed": session_changes,
            "session_summaries_written": 0 if phase == 2 else None,
            "fcpxml_writes": 0,
            "propagate_impact_universe": dict(
                PHASE2_MUTATION_UNIVERSE if phase == 2 else PHASE1_MUTATION_UNIVERSE
            ),
        }


def load_source_identity_safety(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VClipError(f"Could not read source-identity safety JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise VClipError(f"Source-identity safety JSON {path} is not an object.")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise VClipError("Source-identity safety JSON rows must be a list.")
    return payload


def load_reconciliation(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VClipError(f"Could not read reconciliation JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise VClipError(f"Reconciliation JSON {path} is not an object.")
    clips = payload.get("clips")
    if not isinstance(clips, list):
        raise VClipError("Reconciliation JSON clips must be a list.")
    return payload


def location_is_resolved(location: dict[str, Any] | None) -> bool:
    payload = location if isinstance(location, dict) else {}
    if clean_label(payload.get("city")):
        return True
    if clean_label(payload.get("neighborhood")):
        return True
    if clean_label(payload.get("public_label")):
        return True
    return is_usable_gps(payload.get("center_lat"), payload.get("center_lon"))


def clean_label(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or text.casefold() in UNKNOWN_LABELS:
        return None
    return text


def display_location_label(location: dict[str, Any] | None) -> str | None:
    payload = location if isinstance(location, dict) else {}
    for key in ("public_label", "neighborhood", "city", "state"):
        label = clean_label(payload.get(key))
        if label:
            if key == "neighborhood" and clean_label(payload.get("city")):
                return f"{label}, {clean_label(payload.get('city'))}"
            return label
    return None


def library_from_source_xml(path: str | None) -> str:
    if not path:
        return "(unknown library)"
    parts = Path(path).parts
    for part in parts:
        lowered = part.lower()
        if lowered.endswith(".fcpxmld") or lowered.endswith(".fcpbundle"):
            return Path(part).stem
    return Path(path).parent.name or "(unknown library)"


def attach_propagation(
    location: dict[str, Any],
    *,
    inherited_from: str,
    bucket: str,
    source_run_id: str,
    source_stock_clip_id: str,
    extra_notes: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(location or {})
    sources = [str(value) for value in (payload.get("evidence_sources") or []) if value]
    if PROPAGATION_EVIDENCE not in sources:
        sources.append(PROPAGATION_EVIDENCE)
    payload["evidence_sources"] = sources
    payload["propagation"] = {
        "method": PROPAGATION_EVIDENCE,
        "inherited_from": inherited_from,
        "reconciliation_bucket": bucket,
        "source_run_id": source_run_id,
        "source_stock_clip_id": source_stock_clip_id,
        "note": extra_notes
        or (
            "Copied an existing solved location onto the latest unresolved "
            "occurrence. Not new inference."
        ),
        **(extra or {}),
    }
    return payload


def _is_jpg_location(location: dict[str, Any] | None) -> bool:
    payload = location if isinstance(location, dict) else {}
    sources = {str(value) for value in (payload.get("evidence_sources") or [])}
    gps_kind = str(payload.get("gps_kind") or "")
    return gps_kind in JPG_GPS_KINDS or bool(sources & JPG_EVIDENCE_SOURCES)


def preserve_jpg_inference(location: dict[str, Any] | None) -> dict[str, Any]:
    """Copy a donor location while keeping JPG evidence inferred, never direct GPS."""
    payload = dict(location or {})
    if not _is_jpg_location(payload):
        return payload
    payload["direct_source_gps"] = False
    payload["gps_kind"] = GPS_KIND_JPG
    return payload


def _phase2_safety_class(
    *,
    identity_class: str,
    identity_detail: str,
    current_rank: int,
    current_kind: str,
    historical_rank: int,
    historical_kind: str,
    latest: dict[str, Any] | None,
    proposed: dict[str, Any],
    current_location: dict[str, Any],
    current_event: str,
) -> tuple[str, str]:
    if latest is None:
        return MISSING_CANDIDATE, "no latest accepted stock_candidates row for stock_clip_id"
    if identity_class == MISSING_CANDIDATE:
        return MISSING_CANDIDATE, identity_detail
    if identity_class == MISSING_SOURCE_DONOR:
        return MISSING_CANDIDATE, identity_detail
    if identity_class == AMBIGUOUS_SOURCE:
        return MALFORMED, identity_detail
    if identity_class == CONFLICTING_SOURCE:
        return CONFLICTING_CURRENT, identity_detail
    if not location_is_resolved(proposed):
        return MALFORMED, "source-identity donor location is not resolved"
    if identity_class == ALREADY_APPLIED or _already_applied(latest, proposed):
        return ALREADY_APPLIED, "latest row already carries the proposed location"
    if current_rank > historical_rank:
        return STRONGER_CURRENT, (
            f"current {current_kind} outranks inherited {historical_kind}"
        )
    if _location_conflict(current_location, proposed, current_event=current_event):
        return CONFLICTING_CURRENT, "current catalog geography conflicts with donor location"
    if identity_class == SAFE_TO_INHERIT:
        return SAFE_TO_RESTORE, identity_detail
    return MALFORMED, f"unexpected source-identity safety class {identity_class}"


def _proposed_phase1(
    clip: dict[str, Any],
    latest: dict[str, Any] | None,
    occurrences: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str], str | None, str]:
    bucket = str(clip.get("exclusive_bucket") or "")
    notes: list[str] = []
    latest_run = str((latest or {}).get("run_id") or "")
    older_resolved = [
        row
        for row in occurrences
        if row.get("resolved") and str(row.get("run_id") or "") != latest_run
    ]
    corpus = ((clip.get("final_corpus") or {}).get("chosen") or {}) if isinstance(
        clip.get("final_corpus"), dict
    ) else {}

    if bucket == PHASE1_OLDER_DB:
        if not older_resolved:
            return {}, notes, "OLDER_DB_OCCURRENCE_RESOLVED has no live older resolved occurrence", REASON_OLDER_DB
        donor = older_resolved[0]
        proposed = attach_propagation(
            dict(donor.get("location") or {}),
            inherited_from="older_stock_candidates",
            bucket=bucket,
            source_run_id=str(donor.get("run_id") or ""),
            source_stock_clip_id=str(donor.get("stock_clip_id") or ""),
        )
        if not location_is_resolved(proposed):
            return proposed, notes, "older occurrence location is not resolved", REASON_OLDER_DB
        notes.append(
            f"copied location_json from older run {donor.get('run_id')}"
        )
        return proposed, notes, None, REASON_OLDER_DB

    if bucket == PHASE1_CORPUS:
        event_name = str(corpus.get("event_name") or "")
        parsed, _date = parse_event_place(event_name)
        if older_resolved:
            donor = older_resolved[0]
            donor_location = dict(donor.get("location") or {})
            if _labels_compatible(
                display_location_label(donor_location) or "",
                parsed.get("public_label") or event_name,
            ) or not parsed.get("public_label"):
                proposed = attach_propagation(
                    donor_location,
                    inherited_from="older_stock_candidates",
                    bucket=bucket,
                    source_run_id=str(donor.get("run_id") or ""),
                    source_stock_clip_id=str(donor.get("stock_clip_id") or ""),
                    extra_notes=(
                        "Final-corpus row also has an older resolved catalog "
                        "occurrence; GPS/evidence copied from that occurrence."
                    ),
                )
                notes.append(
                    "used older resolved GPS/evidence; corpus supplied the known event name"
                )
                return proposed, notes, None, REASON_CORPUS
            return (
                {},
                notes,
                (
                    "final corpus place "
                    f"{parsed.get('public_label')!r} conflicts with older "
                    f"{display_location_label(donor_location)!r}"
                ),
                REASON_CORPUS,
            )
        if not parsed.get("public_label"):
            return {}, notes, "final corpus appearance has no usable event place", REASON_CORPUS
        proposed = attach_propagation(
            {
                "status": "resolved",
                "confidence": "medium",
                "evidence_sources": [CORPUS_EVIDENCE],
                "center_lat": None,
                "center_lon": None,
                "city": parsed.get("city"),
                "state": parsed.get("state"),
                "country": parsed.get("country"),
                "neighborhood": parsed.get("neighborhood"),
                "public_label": parsed.get("public_label"),
                "direct_source_gps": False,
                "gps_kind": None,
                "note": (
                    "Place copied from the final review-corpus event name. "
                    "No GPS was invented."
                ),
            },
            inherited_from="final_review_corpus",
            bucket=bucket,
            source_run_id=str(corpus.get("stockify_run_id") or latest_run),
            source_stock_clip_id=str(clip.get("stock_clip_id") or ""),
        )
        notes.append("copied named place from final review corpus; no GPS invented")
        return proposed, notes, None, REASON_CORPUS

    return {}, notes, f"unsupported reconciliation bucket {bucket}", ""


def _proposed_names(
    clip: dict[str, Any],
    latest: dict[str, Any] | None,
    proposed: dict[str, Any],
    occurrences: list[dict[str, Any]],
) -> tuple[str, str]:
    capture_date = str(
        (latest or {}).get("session_capture_date") or clip.get("capture_date") or ""
    )
    current_event = str((latest or {}).get("generated_event_name") or "")
    current_project = str((latest or {}).get("generated_clip_project_name") or "")
    latest_run = str((latest or {}).get("run_id") or "")
    older_resolved = [
        row
        for row in occurrences
        if row.get("resolved") and str(row.get("run_id") or "") != latest_run
    ]
    corpus = ((clip.get("final_corpus") or {}).get("chosen") or {}) if isinstance(
        clip.get("final_corpus"), dict
    ) else {}
    event_candidates = []
    if older_resolved:
        event_candidates.append(str(older_resolved[0].get("generated_event_name") or ""))
    if corpus.get("event_name"):
        parsed, date = parse_event_place(str(corpus.get("event_name")))
        event_candidates.append(
            event_base_name(
                proposed or parsed,
                {"date": date or capture_date or "Unknown Date"},
            )
        )
    if current_event and not is_unknown_event_name(current_event):
        event_candidates.append(current_event)
    event_candidates.append(
        event_base_name(proposed, {"date": capture_date or "Unknown Date"})
    )
    new_event = next((name for name in event_candidates if name and not is_unknown_event_name(name)), current_event)

    project_candidates = []
    if older_resolved:
        project_candidates.append(str(older_resolved[0].get("generated_clip_project_name") or ""))
    if corpus.get("project_name"):
        project_candidates.append(str(corpus.get("project_name")))
    new_base = project_base_label(proposed, {"label": "unknown"})
    project_candidates.append(_relabel_project(current_project, new_base))
    new_project = next((name for name in project_candidates if name), current_project or new_base)
    return new_event, new_project


def parse_event_place(event_name: str) -> tuple[dict[str, Any], str | None]:
    parts = [part.strip() for part in str(event_name or "").split(" — ") if part.strip()]
    core = parts[0] if parts else ""
    date = None
    for part in parts[1:]:
        match = EVENT_DATE_RE.search(part)
        if match and DATE_RE.fullmatch(match.group(0)):
            date = match.group(0)
            break
        if part.casefold() == "unknown date":
            date = None
            break
    payload: dict[str, Any] = {
        "public_label": clean_label(core),
        "city": None,
        "neighborhood": None,
        "state": None,
        "country": None,
    }
    if not payload["public_label"]:
        return payload, date
    if "," in core:
        left, right = [piece.strip() for piece in core.split(",", 1)]
        if right.casefold() in US_STATE_NAMES:
            payload["city"] = left
            payload["state"] = right
            payload["country"] = "United States"
        else:
            payload["neighborhood"] = left
            payload["city"] = right
            if right.casefold() == "seattle":
                payload["state"] = "Washington"
                payload["country"] = "United States"
    elif core.casefold() in US_STATE_NAMES:
        payload["state"] = core
        payload["country"] = "United States"
    elif core.casefold().startswith("downtown "):
        city = core.split(" ", 1)[1].strip()
        payload["neighborhood"] = "Downtown"
        payload["city"] = city
        if city.casefold() == "seattle":
            payload["state"] = "Washington"
            payload["country"] = "United States"
    else:
        payload["city"] = core
    return payload, date


def _labels_compatible(left: str, right: str) -> bool:
    a = _label_tokens(left)
    b = _label_tokens(right)
    if not a or not b:
        return True
    if a == b:
        return True
    if a.issubset(b) or b.issubset(a):
        return True
    return bool(a & b)


def _label_tokens(value: str) -> set[str]:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold())
    skip = {"the", "of", "and", "unknown", "date", "location"}
    return {token for token in text.split() if token and token not in skip}


def _labels_are_single_place(labels: list[str]) -> bool:
    """Source-identity donors must share one specific place label.

    State-only labels such as "Washington" are coarser than "Sequim, Washington"
    and do not make the stem ambiguous. Distinct specific labels
    (Charlottesville vs University of Virginia) stay ambiguous.
    """
    specific = []
    for label in labels:
        if not label:
            continue
        parsed, _date = parse_event_place(label)
        if parsed.get("state") and not parsed.get("city") and not parsed.get("neighborhood"):
            continue
        specific.append(label.casefold())
    return len(set(specific)) <= 1


def _location_conflict(
    current: dict[str, Any],
    proposed: dict[str, Any],
    *,
    current_event: str | None = None,
) -> bool:
    if not location_is_resolved(current):
        return False
    current_city = clean_label(current.get("city"))
    proposed_city = clean_label(proposed.get("city"))
    if current_city and proposed_city and current_city.casefold() != proposed_city.casefold():
        if not _labels_compatible(current_city, proposed_city):
            return True
    current_label = display_location_label(current)
    proposed_label = display_location_label(proposed)
    if current_label and proposed_label and not _labels_compatible(current_label, proposed_label):
        return True
    if is_usable_gps(current.get("center_lat"), current.get("center_lon")) and is_usable_gps(
        proposed.get("center_lat"), proposed.get("center_lon")
    ):
        spread = _haversine_m(
            float(current["center_lat"]),
            float(current["center_lon"]),
            float(proposed["center_lat"]),
            float(proposed["center_lon"]),
        )
        if spread > GPS_CONFLICT_METERS:
            return True
    if current_event and not is_unknown_event_name(current_event):
        parsed, _date = parse_event_place(current_event)
        event_label = parsed.get("public_label")
        if event_label and proposed_label and not _labels_compatible(event_label, proposed_label):
            # State-only current names are coarser, not conflicting.
            if parsed.get("city") or parsed.get("neighborhood"):
                return True
    return False


def _locations_equivalent(current: dict[str, Any], proposed: dict[str, Any]) -> bool:
    if not location_is_resolved(current) or not location_is_resolved(proposed):
        return False
    if is_usable_gps(current.get("center_lat"), current.get("center_lon")) or is_usable_gps(
        proposed.get("center_lat"), proposed.get("center_lon")
    ):
        if not (
            is_usable_gps(current.get("center_lat"), current.get("center_lon"))
            and is_usable_gps(proposed.get("center_lat"), proposed.get("center_lon"))
        ):
            return False
        if _haversine_m(
            float(current["center_lat"]),
            float(current["center_lon"]),
            float(proposed["center_lat"]),
            float(proposed["center_lon"]),
        ) > 1.0:
            return False
    current_label = display_location_label(current)
    proposed_label = display_location_label(proposed)
    if current_label and proposed_label:
        return current_label.casefold() == proposed_label.casefold()
    return True


def _already_applied(latest: dict[str, Any], proposed: dict[str, Any]) -> bool:
    current = dict(latest.get("location") or {})
    if not location_is_resolved(current):
        return False
    return _locations_equivalent(current, proposed)


def _max_gps_spread_m(locations: list[dict[str, Any]]) -> float | None:
    coords = [
        (float(item["center_lat"]), float(item["center_lon"]))
        for item in locations
        if is_usable_gps(item.get("center_lat"), item.get("center_lon"))
    ]
    if len(coords) < 2:
        return 0.0 if coords else None
    farthest = 0.0
    for index, (lat, lon) in enumerate(coords):
        for other_lat, other_lon in coords[index + 1 :]:
            farthest = max(farthest, _haversine_m(lat, lon, other_lat, other_lon))
    return farthest


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    hav = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(hav))


def _is_8_6_26(
    *,
    library: str | None,
    source_event: str | None,
    source_xml: str | None,
) -> bool:
    blob = f"{library or ''}\n{source_event or ''}\n{source_xml or ''}".casefold()
    return LIBRARY_8_6_26 in blob


def _relabel_project(old_label: str, new_base: str) -> str:
    if not old_label:
        return new_base
    if " — " not in old_label:
        return new_base
    _base, suffix = old_label.split(" — ", 1)
    if suffix.lower() in {str(label).lower() for label in TIME_LABELS.values()}:
        return new_base
    return safe_filename(f"{new_base} — {suffix}")


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _grouped_labels(counter: Counter[str]) -> list[dict[str, Any]]:
    return [
        {"label": label, "count": count}
        for label, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    ]


def _latest_coverage(
    latest_by_clip: dict[str, dict[str, Any]],
    *,
    projected_safe: int,
    after_write: bool = False,
) -> dict[str, Any]:
    total = len(latest_by_clip)
    resolved = sum(1 for item in latest_by_clip.values() if item.get("resolved"))
    unresolved = total - resolved
    unresolved_hours = (
        sum(
            float(item.get("duration_seconds") or 0.0)
            for item in latest_by_clip.values()
            if not item.get("resolved")
        )
        / 3600.0
    )
    projected_unresolved = max(unresolved - projected_safe, 0)
    projected_resolved = min(resolved + projected_safe, total)
    labels = Counter(
        display_location_label(item.get("location")) or "Unknown / unresolved"
        for item in latest_by_clip.values()
    )
    return {
        "universe": dict(LATEST_UNIVERSE),
        "after_write": after_write,
        "total": total,
        "resolved": resolved if after_write else resolved,
        "unresolved": unresolved if after_write else unresolved,
        "unresolved_hours": round(unresolved_hours, 4),
        "projected_resolved": projected_resolved,
        "projected_unresolved": projected_unresolved,
        "projected_unresolved_hours_estimate": round(
            unresolved_hours * (projected_unresolved / unresolved) if unresolved else 0.0,
            4,
        ),
        "locations_grouped_by_count": _grouped_labels(labels),
    }


def _library_8_6_26_section(mutations: list[dict[str, Any]]) -> dict[str, Any]:
    scoped = [item for item in mutations if item.get("is_8_6_26")]
    labels = Counter(str(item.get("proposed_location_label") or "Unknown") for item in scoped)
    return {
        "library": LIBRARY_8_6_26,
        "expected": 18,
        "phase1_rows": len(scoped),
        "matched": sum(1 for item in scoped if item.get("matched")),
        "safe_to_restore": sum(1 for item in scoped if item["safety_class"] == SAFE_TO_RESTORE),
        "missing": sum(1 for item in scoped if item["safety_class"] == MISSING_CANDIDATE),
        "conflicts": sum(
            1 for item in scoped if item["safety_class"] == CONFLICTING_CURRENT
        ),
        "stronger_current_evidence": sum(
            1 for item in scoped if item["safety_class"] == STRONGER_CURRENT
        ),
        "all_eighteen_safe": len(scoped) == 18
        and all(item["safety_class"] == SAFE_TO_RESTORE for item in scoped),
        "locations_grouped_by_count": _grouped_labels(labels),
        "stock_clip_ids": [item["stock_clip_id"] for item in scoped],
    }


def _public_mutation(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": item["run_id"],
        "stockify_run_id": item["stockify_run_id"],
        "stock_clip_id": item["stock_clip_id"],
        "session_id": item.get("session_id"),
        "library": item.get("library"),
        "source_media": item.get("source_media"),
        "source_stem": item.get("source_stem"),
        "eligibility_status": item.get("eligibility_status"),
        "reconciliation_bucket": item.get("reconciliation_bucket"),
        "recovery_reason": item.get("recovery_reason"),
        "confidence": item.get("confidence"),
        "old_location_snapshot": item.get("old_location_snapshot"),
        "proposed_location_snapshot": item.get("proposed_location_snapshot"),
        "proposed_location_label": item.get("proposed_location_label"),
        "current_generated_event_name": item.get("current_generated_event_name"),
        "current_generated_project_name": item.get("current_generated_project_name"),
        "current_evidence_kind": item.get("current_evidence_kind"),
        "historical_evidence_kind": item.get("historical_evidence_kind"),
        "safety_class": item.get("safety_class"),
        "safety_detail": item.get("safety_detail"),
        "provenance_notes": item.get("provenance_notes"),
        "capture_date": item.get("capture_date"),
        "matched": item.get("matched"),
        "is_8_6_26": item.get("is_8_6_26"),
        "journal_run_matches_latest": item.get("journal_run_matches_latest"),
        "representative_lat": item.get("representative_lat"),
        "representative_lon": item.get("representative_lon"),
        "donor_run_id": item.get("donor_run_id"),
        "donor_stock_clip_id": item.get("donor_stock_clip_id"),
        "primary_evidence_kind": item.get("primary_evidence_kind"),
        "identity_kind": item.get("identity_kind"),
    }


def _recovery_from_mutation(mutation: dict[str, Any]) -> LocationRecoveryRow:
    row = mutation.get("_recovery_row")
    if isinstance(row, LocationRecoveryRow):
        proposed = (mutation.get("proposed_location_snapshot") or {}).get("location")
        if proposed:
            row.provenance = dict(row.provenance or {})
            row.provenance["location"] = proposed
        return row
    raise VClipError("Propagate mutation is missing the historical recovery row.")


def format_propagate_report_text(report: HistoricalLocationPropagateReport) -> str:
    before = report.coverage_before
    after = report.coverage_after
    eight = report.library_8_6_26
    source = report.source_identity
    if report.phase == 2:
        lines = [
            "Historical location propagation (Phase 2)",
            f"Mode: {report.mode}",
            f"Read-only: {report.read_only}",
            f"DB: {report.db_path}",
            f"Source-identity safety: {report.source_identity_path}",
            "",
            "Phase-2 source-identity (safe_to_inherit only)",
            f"  targets: {report.phase2_targets}",
            f"  matched: {report.matched_candidates}",
            f"  safe_to_restore: {report.safe_to_restore}",
            f"  already_applied: {report.already_applied}",
            f"  stronger_current_evidence: {report.stronger_current_evidence}",
            f"  conflicting_current_evidence: {report.conflicting_current_evidence}",
            f"  missing: {report.missing_candidates}",
            f"  malformed: {report.malformed_historical_recovery}",
            f"  ambiguous excluded: {report.ambiguous_excluded}",
            f"  ambiguous excluded ids: {report.ambiguous_excluded_ids}",
            f"  live primary evidence: {source.get('live_primary_evidence')}",
            "",
            "Latest accepted universe coverage",
            (
                f"  before: {before.get('resolved')} resolved / "
                f"{before.get('unresolved')} unresolved / {before.get('total')} total"
            ),
            (
                "  projected after Phase 2: "
                f"{after.get('projected_resolved')} resolved / "
                f"{after.get('projected_unresolved')} unresolved"
            ),
            "",
            "Phase-2 locations grouped by count (safe_to_restore)",
        ]
        if report.locations_grouped_by_count:
            for item in report.locations_grouped_by_count:
                lines.append(f"  {item['count']:>4}  {item['label']}")
        else:
            lines.append("  (none)")
        if report.backup_path:
            lines.extend(["", f"Backup: {report.backup_path}"])
        if report.write_blocked_reason:
            lines.extend(["", f"Write blocked: {report.write_blocked_reason}"])
        if report.warnings:
            lines.extend(["", "Warnings:"])
            lines.extend(f"  - {warning}" for warning in report.warnings)
        return "\n".join(lines) + "\n"
    lines = [
        "Historical location propagation (Phase 1)",
        f"Mode: {report.mode}",
        f"Read-only: {report.read_only}",
        f"DB: {report.db_path}",
        f"Reconciliation: {report.reconciliation_path}",
        "",
        "Phase-1 104 (ALREADY_SOLVED_FINAL_CORPUS + OLDER_DB_OCCURRENCE_RESOLVED)",
        f"  targets: {report.phase1_targets}",
        f"  matched: {report.matched_candidates}",
        f"  safe_to_restore: {report.safe_to_restore}",
        f"  already_applied: {report.already_applied}",
        f"  stronger_current_evidence: {report.stronger_current_evidence}",
        f"  conflicting_current_evidence: {report.conflicting_current_evidence}",
        f"  missing: {report.missing_candidates}",
        f"  malformed: {report.malformed_historical_recovery}",
        f"  by bucket: {report.by_bucket}",
        "",
        "Latest accepted universe coverage",
        f"  before: {before.get('resolved')} resolved / {before.get('unresolved')} unresolved / {before.get('total')} total",
        f"  projected after Phase 1: {after.get('projected_resolved')} resolved / {after.get('projected_unresolved')} unresolved",
        "",
        "Phase-1 locations grouped by count (safe_to_restore)",
    ]
    if report.locations_grouped_by_count:
        for item in report.locations_grouped_by_count:
            lines.append(f"  {item['count']:>4}  {item['label']}")
    else:
        lines.append("  (none)")
    lines.extend(
        [
            "",
            "8-6-26",
            f"  rows: {eight.get('phase1_rows')} (expected {eight.get('expected')})",
            f"  matched: {eight.get('matched')}",
            f"  safe_to_restore: {eight.get('safe_to_restore')}",
            f"  all_eighteen_safe: {eight.get('all_eighteen_safe')}",
        ]
    )
    for item in eight.get("locations_grouped_by_count") or []:
        lines.append(f"  {item['count']:>4}  {item['label']}")
    lines.extend(
        [
            "",
            "Source-identity 165 (read-only; not written)",
            f"  targets: {source.get('targets')}",
            f"  safe_to_inherit: {source.get('safe_to_inherit')}",
            f"  ambiguous: {source.get('ambiguous')}",
            f"  conflicting: {source.get('conflicting')}",
            f"  missing_donor: {source.get('missing_donor')}",
            f"  missing_candidate: {source.get('missing_candidate')}",
            f"  safe primary evidence: {source.get('safe_primary_evidence')}",
            f"  safe original evidence sources: {source.get('safe_original_evidence_sources')}",
            f"  safe identity kind: {source.get('safe_identity_kind')}",
            (
                "  projected unresolved if those safe source cases were later "
                f"propagated: {source.get('projected_unresolved_if_safe_later_propagated')}"
            ),
        ]
    )
    for item in source.get("safe_locations_grouped_by_count") or []:
        lines.append(f"  {item['count']:>4}  {item['label']}")
    if report.backup_path:
        lines.extend(["", f"Backup: {report.backup_path}"])
    if report.write_blocked_reason:
        lines.extend(["", f"Write blocked: {report.write_blocked_reason}"])
    if report.warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"  - {warning}" for warning in report.warnings)
    return "\n".join(lines) + "\n"
