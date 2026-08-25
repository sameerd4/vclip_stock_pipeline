"""Validate and restore historical review-location materialization into SQLite.

Consumes the historical materialization plan plus jpg-exif-forensic.json.
Does not remount media, recompute JPG inference, or rewrite FCPXML.

Validate is read-only. Write requires explicit --write, a pre-mutation backup,
zero missing candidates, and zero conflicting current evidence.
"""

from __future__ import annotations

import json
import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..db.repository import CatalogRepository
from ..errors import VClipError
from ..stockify.core import local_name
from ..stockify.fcpxml import read_vclip_metadata
from ..stockify.metadata import is_usable_gps
from ..stockify.sidecars import normalized_stem
from ..util import json_dumps, json_loads, utc_now
from .catalog import WorkflowCatalog
from .physical_location_coverage import is_unknown_event_name
from .review_location_materialize import (
    EDITORIAL_CONSENSUS_REASON,
    JPG_EXIF_REASON,
    STALE_CORRECTION_REASON,
    _jpg_payload,
    _location_from_persisted_evidence,
)
from .review_location_recover import (
    LocationRecoveryRow,
    persist_review_location_candidate_updates,
)

MODE = "historical_location_restore_validation"
WRITE_MODE = "historical_location_restore_write"
MATERIALIZE_EVIDENCE = "review_location_materialize"
GPS_KIND_JPG = "inferred_jpg_exif_same_shoot"

SAFE_TO_RESTORE = "safe_to_restore"
ALREADY_APPLIED = "already_applied"
STRONGER_CURRENT = "stronger_current_evidence"
CONFLICTING_CURRENT = "conflicting_current_evidence"
MISSING_CANDIDATE = "missing_candidate"
MALFORMED = "malformed_historical_recovery"
SAFETY_CLASSES = (
    SAFE_TO_RESTORE,
    ALREADY_APPLIED,
    STRONGER_CURRENT,
    CONFLICTING_CURRENT,
    MISSING_CANDIDATE,
    MALFORMED,
)

RANK_MANUAL = 100
RANK_DIRECT_SRT = 80
RANK_DIRECT_CATALOG = 70
RANK_JPG = 40
RANK_CONSENSUS = 20
RANK_UNKNOWN = 0

JPG_REASONS = frozenset({JPG_EXIF_REASON, STALE_CORRECTION_REASON})
MANUAL_MARKERS = frozenset(
    {"manual_gps_override", "manual_confirmed", "manual_override"}
)
SRT_SOURCE_MARKERS = frozenset({"srt_gps", "srt_gps_review_recovery"})
DIRECT_GPS_KINDS = frozenset(
    {
        "source_srt",
        "srt_gps",
        "direct_source_gps",
        "catalog_gps",
        "existing_direct_gps",
        "flight_trajectory",
    }
)
INFERRED_GPS_KINDS = frozenset(
    {
        GPS_KIND_JPG,
        "inferred_jpg_exif",
        JPG_EXIF_REASON,
    }
)

HOURS_IN_SILENCE_RUN_ID = "STOCKIFY_162E6595D8234853BDAAAA20BA0EF1F7"
HOURS_IN_SILENCE_SESSION_ID = "SESSION_BFB0BCB49BFE9B82325D"
HOURS_IN_SILENCE_CAPTURE_DATE = "2025-11-08"
HOURS_IN_SILENCE_SCREEN_RECORDING = "ScreenRecording_11-07-2025 04-26-47_1.mov"

COORD_MATCH_EPSILON = 1e-5

RESTORE_MUTATION_UNIVERSE = {
    "universe_name": "historical_restore_plan_targets",
    "dedupe_key": ["stockify_run_id", "stock_clip_id"],
    "latest_run_semantics": (
        "none — identities are the exact historical plan pairs, not "
        "latest-run-per-library"
    ),
    "accepted_eligibility_semantics": (
        "Match by exact (stockify_run_id, stock_clip_id). eligibility_status "
        "is reported but does not redefine membership."
    ),
    "sql": (
        "SELECT ... FROM stock_candidates c "
        "LEFT JOIN source_media m ON m.id=c.source_media_id "
        "LEFT JOIN shoot_sessions s ON s.id=c.session_id "
        "WHERE (c.run_id, c.stock_clip_id) IN (plan pairs)"
    ),
}

ALL_CANDIDATES_UNIVERSE = {
    "universe_name": "all_stock_candidates_rows",
    "dedupe_key": ["run_id", "stock_clip_id"],
    "latest_run_semantics": "none — every stock_candidates row, all runs",
    "accepted_eligibility_semantics": "none — eligibility is not filtered",
    "sql": (
        "SELECT run_id, stock_clip_id, location_json, generated_event_name, "
        "generated_clip_project_name FROM stock_candidates"
    ),
}


@dataclass
class HistoricalPlanRow:
    stockify_run_id: str
    stock_clip_id: str
    original_event_name: str
    new_event_name: str
    original_project_name: str
    new_project_name: str
    source_media: str | None
    srt_paths: list[str]
    representative_lat: float | None
    representative_lon: float | None
    resolution_confidence: str
    recovery_reason: str
    source_shard: str
    input_xml: str
    output_xml: str | None
    provenance: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def identity(self) -> tuple[str, str]:
        return (self.stockify_run_id, self.stock_clip_id)

    def as_recovery_row(self, *, location: dict[str, Any] | None = None) -> LocationRecoveryRow:
        provenance = dict(self.provenance or {})
        if location is not None:
            provenance["location"] = location
        return LocationRecoveryRow(
            stockify_run_id=self.stockify_run_id,
            stock_clip_id=self.stock_clip_id,
            original_event_name=self.original_event_name,
            new_event_name=self.new_event_name,
            original_project_name=self.original_project_name,
            new_project_name=self.new_project_name,
            source_media=self.source_media,
            srt_paths=list(self.srt_paths or []),
            representative_lat=self.representative_lat,
            representative_lon=self.representative_lon,
            resolution_confidence=self.resolution_confidence,
            recovery_reason=self.recovery_reason,
            source_shard=self.source_shard,
            input_xml=self.input_xml,
            output_xml=self.output_xml,
            provenance=provenance,
        )


@dataclass
class HistoricalLocationRestoreReport:
    mode: str = MODE
    read_only: bool = True
    dry_run: bool = True
    db_path: str = ""
    plan_path: str = ""
    forensic_json: str = ""
    review_root: str | None = None
    backup_path: str | None = None
    plan_mutations: int = 0
    matched_candidates: int = 0
    missing_candidates: int = 0
    safe_to_restore: int = 0
    already_applied: int = 0
    stronger_current_evidence: int = 0
    conflicting_current_evidence: int = 0
    malformed_historical_recovery: int = 0
    by_recovery_reason: dict[str, int] = field(default_factory=dict)
    by_safety_class: dict[str, int] = field(default_factory=dict)
    candidate_universe: dict[str, Any] = field(default_factory=dict)
    hours_in_silence: dict[str, Any] = field(default_factory=dict)
    fcpxml_cross_check: dict[str, Any] = field(default_factory=dict)
    coverage_before: dict[str, Any] = field(default_factory=dict)
    coverage_after: dict[str, Any] = field(default_factory=dict)
    post_write_audit: dict[str, Any] = field(default_factory=dict)
    write_blocked_reason: str | None = None
    mutations: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "read_only": self.read_only,
            "dry_run": self.dry_run,
            "db_path": self.db_path,
            "plan_path": self.plan_path,
            "forensic_json": self.forensic_json,
            "review_root": self.review_root,
            "backup_path": self.backup_path,
            "plan_mutations": self.plan_mutations,
            "matched_candidates": self.matched_candidates,
            "missing_candidates": self.missing_candidates,
            "safe_to_restore": self.safe_to_restore,
            "already_applied": self.already_applied,
            "stronger_current_evidence": self.stronger_current_evidence,
            "conflicts": self.conflicting_current_evidence,
            "conflicting_current_evidence": self.conflicting_current_evidence,
            "malformed_historical_recovery": self.malformed_historical_recovery,
            "missing": self.missing_candidates,
            "by_recovery_reason": dict(self.by_recovery_reason),
            "by_safety_class": dict(self.by_safety_class),
            "candidate_universe": dict(self.candidate_universe),
            "hours_in_silence": dict(self.hours_in_silence),
            "fcpxml_cross_check": dict(self.fcpxml_cross_check),
            "coverage_before": dict(self.coverage_before),
            "coverage_after": dict(self.coverage_after),
            "post_write_audit": dict(self.post_write_audit),
            "write_blocked_reason": self.write_blocked_reason,
            "mutations": [
                {key: value for key, value in item.items() if not str(key).startswith("_")}
                for item in self.mutations
            ],
            "warnings": list(self.warnings),
        }


def parse_historical_plan(payload: dict[str, Any] | list[Any]) -> list[HistoricalPlanRow]:
    """Parse materialization-plan JSON into deterministic recovery rows."""
    if isinstance(payload, list):
        raw_rows = payload
    elif isinstance(payload, dict):
        raw_rows = payload.get("recoveries")
        if raw_rows is None:
            raw_rows = payload.get("plan_recoveries") or payload.get("mutations") or []
    else:
        raise VClipError("Historical location plan is not a JSON object or list.")
    if not isinstance(raw_rows, list):
        raise VClipError("Historical location plan recoveries must be a list.")
    parsed: list[HistoricalPlanRow] = []
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, dict):
            raise VClipError(f"Plan recovery {index} is not an object.")
        provenance = raw.get("provenance") or {}
        if isinstance(provenance, str):
            provenance = json_loads(provenance, {})
        if not isinstance(provenance, dict):
            provenance = {}
        parsed.append(
            HistoricalPlanRow(
                stockify_run_id=str(raw.get("stockify_run_id") or ""),
                stock_clip_id=str(raw.get("stock_clip_id") or ""),
                original_event_name=str(raw.get("original_event_name") or ""),
                new_event_name=str(raw.get("new_event_name") or ""),
                original_project_name=str(raw.get("original_project_name") or ""),
                new_project_name=str(raw.get("new_project_name") or ""),
                source_media=raw.get("source_media"),
                srt_paths=list(raw.get("srt_paths") or []),
                representative_lat=_optional_float(raw.get("representative_lat")),
                representative_lon=_optional_float(raw.get("representative_lon")),
                resolution_confidence=str(raw.get("resolution_confidence") or ""),
                recovery_reason=str(raw.get("recovery_reason") or ""),
                source_shard=str(raw.get("source_shard") or ""),
                input_xml=str(raw.get("input_xml") or ""),
                output_xml=raw.get("output_xml"),
                provenance=provenance,
                raw=dict(raw),
            )
        )
    return parsed


def load_historical_plan(path: Path) -> list[HistoricalPlanRow]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VClipError(f"Could not read historical plan {path}: {exc}") from exc
    return parse_historical_plan(payload)


def load_forensic_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VClipError(f"Could not read forensic JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise VClipError(f"Forensic JSON {path} is not an object.")
    return payload


def index_forensic_jpg_evidence(forensic: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Index source-level JPG evidence by stem, basename, and stock_clip_id."""
    block = forensic.get("jpg_exif_forensic") or forensic
    evidence_list = []
    if isinstance(block, dict):
        evidence_list = list(block.get("source_level_evidence") or [])
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for evidence in evidence_list:
        if not isinstance(evidence, dict):
            continue
        keys: set[str] = set()
        stem = normalized_stem(str(evidence.get("stem") or evidence.get("source_basename") or ""))
        basename = str(evidence.get("source_basename") or "")
        if stem:
            keys.add(f"stem:{stem}")
        if basename:
            keys.add(f"basename:{basename}")
            keys.add(f"stem:{normalized_stem(basename)}")
        for clip_id in evidence.get("stock_clip_ids") or []:
            if clip_id:
                keys.add(f"clip:{clip_id}")
        for key in keys:
            index[key].append(evidence)
    return index


def lookup_forensic_evidence(
    index: dict[str, list[dict[str, Any]]],
    *,
    stock_clip_id: str,
    source_media: str | None,
) -> dict[str, Any] | None:
    ordered_keys = [f"clip:{stock_clip_id}"]
    if source_media:
        ordered_keys.append(f"basename:{source_media}")
        ordered_keys.append(f"stem:{normalized_stem(source_media)}")
    seen: set[int] = set()
    matches: list[dict[str, Any]] = []
    for key in ordered_keys:
        for evidence in index.get(key) or []:
            marker = id(evidence)
            if marker in seen:
                continue
            seen.add(marker)
            matches.append(evidence)
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    for evidence in matches:
        clip_ids = {str(value) for value in (evidence.get("stock_clip_ids") or [])}
        if stock_clip_id in clip_ids:
            return evidence
    if source_media:
        wanted = normalized_stem(source_media)
        for evidence in matches:
            stem = normalized_stem(str(evidence.get("stem") or evidence.get("source_basename") or ""))
            if stem == wanted:
                return evidence
    return matches[0]


def create_pre_restore_backup(
    db_path: Path,
    *,
    backup_path: Path | None = None,
    clock: Callable[[], datetime] | None = None,
    name_prefix: str = "pre-location-restore",
) -> Path:
    """Create a consistent SQLite snapshot next to the DB. Never overwrite."""
    resolved = db_path.expanduser().resolve()
    if not resolved.is_file():
        raise VClipError(f"Cannot backup missing database {resolved}")
    if backup_path is None:
        stamp = (clock or datetime.now)().strftime("%Y%m%d-%H%M%S")
        backup_path = resolved.with_name(
            f"{resolved.name}.{name_prefix}-{stamp}.bak"
        )
    dest = backup_path.expanduser().resolve()
    if dest.exists():
        raise VClipError(f"Restore backup already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dest.as_posix())
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
    finally:
        source.close()
    return dest


def current_evidence_rank(location: dict[str, Any] | None, *, recovery_reason: str | None = None) -> tuple[int, str]:
    """Rank currently persisted catalog evidence. Higher wins."""
    payload = location if isinstance(location, dict) else {}
    sources = {str(value) for value in (payload.get("evidence_sources") or [])}
    gps_kind = str(payload.get("gps_kind") or "")
    method = str((payload.get("recovery") or {}).get("method") or "")
    reason = str(recovery_reason or payload.get("recovery_reason") or method or "")
    direct = payload.get("direct_source_gps")
    has_gps = is_usable_gps(payload.get("center_lat"), payload.get("center_lon"))

    if (
        sources & MANUAL_MARKERS
        or reason in MANUAL_MARKERS
        or method in MANUAL_MARKERS
        or str(payload.get("status") or "") in MANUAL_MARKERS
    ):
        return RANK_MANUAL, "manual_override"

    inferred = gps_kind in INFERRED_GPS_KINDS or bool(sources & {JPG_EXIF_REASON, GPS_KIND_JPG})
    if direct is True and has_gps:
        if sources & SRT_SOURCE_MARKERS or reason in SRT_SOURCE_MARKERS or gps_kind in DIRECT_GPS_KINDS:
            return RANK_DIRECT_SRT, "direct_source_srt_gps"
        if not inferred:
            return RANK_DIRECT_CATALOG, "direct_catalog_gps"

    if (
        has_gps
        and not inferred
        and direct is not False
        and (
            gps_kind in DIRECT_GPS_KINDS
            or sources & SRT_SOURCE_MARKERS
            or reason in SRT_SOURCE_MARKERS
        )
    ):
        return RANK_DIRECT_SRT, "direct_source_srt_gps"

    if has_gps and not inferred and direct is not False and JPG_EXIF_REASON not in sources:
        if EDITORIAL_CONSENSUS_REASON not in sources and reason != EDITORIAL_CONSENSUS_REASON:
            if has_gps and (
                gps_kind in DIRECT_GPS_KINDS
                or sources & {"catalog_gps", "existing_direct_gps", "flight_trajectory"}
            ):
                return RANK_DIRECT_CATALOG, "direct_catalog_gps"
            if has_gps and sources - {"missing_srt_gps"} and "missing_srt_gps" not in sources:
                return RANK_DIRECT_CATALOG, "direct_catalog_gps"

    if inferred and has_gps:
        return RANK_JPG, "inferred_jpg_exif"

    if EDITORIAL_CONSENSUS_REASON in sources or reason == EDITORIAL_CONSENSUS_REASON:
        return RANK_CONSENSUS, "editorial_group_consensus"

    return RANK_UNKNOWN, "unknown_or_empty"


def historical_evidence_rank(reason: str) -> tuple[int, str]:
    if reason in JPG_REASONS:
        return RANK_JPG, "inferred_jpg_exif"
    if reason == EDITORIAL_CONSENSUS_REASON:
        return RANK_CONSENSUS, "editorial_group_consensus"
    if reason in SRT_SOURCE_MARKERS:
        return RANK_DIRECT_SRT, "direct_source_srt_gps"
    if reason in MANUAL_MARKERS:
        return RANK_MANUAL, "manual_override"
    return RANK_UNKNOWN, reason or "unknown"


class HistoricalLocationRestoreService:
    """Validate historical location recoveries and optionally persist them."""

    def __init__(
        self,
        repository: CatalogRepository,
        catalog: WorkflowCatalog | None = None,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.repository = repository
        self.catalog = catalog or WorkflowCatalog(repository.database)
        self._announce = progress or (lambda _message: None)

    def validate(
        self,
        *,
        plan_path: Path,
        forensic_json: Path,
        review_root: Path | None = None,
        hours_in_silence_run_id: str = HOURS_IN_SILENCE_RUN_ID,
        hours_in_silence_capture_date: str = HOURS_IN_SILENCE_CAPTURE_DATE,
        hours_in_silence_session_id: str = HOURS_IN_SILENCE_SESSION_ID,
    ) -> HistoricalLocationRestoreReport:
        plan_rows = load_historical_plan(plan_path)
        forensic = load_forensic_json(forensic_json)
        forensic_index = index_forensic_jpg_evidence(forensic)
        self._announce(f"Loaded {len(plan_rows)} historical recoveries (read-only).")
        candidates = self._load_candidates_readonly({row.identity for row in plan_rows})
        mutations = [
            self._classify_row(row, candidates.get(row.identity), forensic_index)
            for row in plan_rows
        ]
        report = self._build_report(
            plan_path=plan_path,
            forensic_json=forensic_json,
            review_root=review_root,
            plan_rows=plan_rows,
            mutations=mutations,
            read_only=True,
            dry_run=True,
            hours_in_silence_run_id=hours_in_silence_run_id,
            hours_in_silence_capture_date=hours_in_silence_capture_date,
            hours_in_silence_session_id=hours_in_silence_session_id,
        )
        if review_root is not None:
            report.fcpxml_cross_check = self._cross_check_review_root(
                review_root=review_root, plan_rows=plan_rows
            )
        return report

    def restore(
        self,
        *,
        plan_path: Path,
        forensic_json: Path,
        review_root: Path | None = None,
        write: bool = False,
        backup_path: Path | None = None,
        fail_after: int | None = None,
        hours_in_silence_run_id: str = HOURS_IN_SILENCE_RUN_ID,
        hours_in_silence_capture_date: str = HOURS_IN_SILENCE_CAPTURE_DATE,
        hours_in_silence_session_id: str = HOURS_IN_SILENCE_SESSION_ID,
    ) -> HistoricalLocationRestoreReport:
        report = self.validate(
            plan_path=plan_path,
            forensic_json=forensic_json,
            review_root=review_root,
            hours_in_silence_run_id=hours_in_silence_run_id,
            hours_in_silence_capture_date=hours_in_silence_capture_date,
            hours_in_silence_session_id=hours_in_silence_session_id,
        )
        if not write:
            return report
        blocked = self._write_block_reason(report)
        if blocked:
            report.write_blocked_reason = blocked
            raise VClipError(blocked)
        db_path = Path(self.repository.database.path)
        created_backup = create_pre_restore_backup(db_path, backup_path=backup_path)
        report.backup_path = str(created_backup)
        self._announce(f"Backup written: {created_backup}")
        to_write = [
            mutation
            for mutation in report.mutations
            if mutation["safety_class"] == SAFE_TO_RESTORE
        ]
        recoveries = [
            _recovery_from_mutation(mutation) for mutation in to_write
        ]
        before = self._fingerprint_all_candidates()
        recoveries_before = self._recovery_table_snapshot()
        try:
            self._persist_restore(
                recoveries,
                mutations=to_write,
                fail_after=fail_after,
            )
        except Exception:
            report.mode = WRITE_MODE
            report.read_only = False
            report.dry_run = False
            report.backup_path = str(created_backup)
            raise
        after = self._fingerprint_all_candidates()
        recoveries_after = self._recovery_table_snapshot()
        written_keys = {(item.stockify_run_id, item.stock_clip_id) for item in recoveries}
        report.mode = WRITE_MODE
        report.read_only = False
        report.dry_run = False
        report.backup_path = str(created_backup)
        report.coverage_after = self._coverage_for_mutations(report.mutations, after_write=True)
        report.post_write_audit = self._post_write_audit(
            mutations=report.mutations,
            written_keys=written_keys,
            before=before,
            after=after,
            recoveries_before=recoveries_before,
            recoveries_after=recoveries_after,
            hours_in_silence_run_id=hours_in_silence_run_id,
            hours_in_silence_capture_date=hours_in_silence_capture_date,
        )
        return report

    def _write_block_reason(self, report: HistoricalLocationRestoreReport) -> str | None:
        if report.missing_candidates:
            return (
                "Refusing historical location restore: "
                f"{report.missing_candidates} missing candidate(s)."
            )
        if report.conflicting_current_evidence:
            return (
                "Refusing historical location restore: "
                f"{report.conflicting_current_evidence} conflicting_current_evidence row(s)."
            )
        if report.malformed_historical_recovery:
            return (
                "Refusing historical location restore: "
                f"{report.malformed_historical_recovery} malformed_historical_recovery row(s)."
            )
        return None

    def _persist_restore(
        self,
        recoveries: list[LocationRecoveryRow],
        *,
        mutations: list[dict[str, Any]],
        fail_after: int | None = None,
    ) -> None:
        session_summaries = _conservative_session_summaries(mutations)
        with self.repository.database.transaction() as connection:
            for index, recovery in enumerate(recoveries, start=1):
                persist_review_location_candidate_updates(connection, [recovery])
                if fail_after is not None and index >= fail_after:
                    raise RuntimeError("injected restore failure")
            self.catalog.record_review_location_recoveries(
                recoveries=recoveries,
                connection=connection,
            )
            _persist_session_summaries(connection, session_summaries)

    def _classify_row(
        self,
        row: HistoricalPlanRow,
        candidate: dict[str, Any] | None,
        forensic_index: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        proposed, provenance_notes, malformed_reason = _proposed_location(
            row, forensic_index
        )
        current_location = dict((candidate or {}).get("location") or {})
        current_event = str((candidate or {}).get("generated_event_name") or "")
        current_project = str(
            (candidate or {}).get("generated_clip_project_name")
            or (candidate or {}).get("generated_project_name")
            or ""
        )
        existing_recovery_reason = (candidate or {}).get("existing_recovery_reason")
        current_rank, current_kind = current_evidence_rank(
            current_location, recovery_reason=existing_recovery_reason
        )
        historical_rank, historical_kind = historical_evidence_rank(row.recovery_reason)
        safety = SAFE_TO_RESTORE
        safety_detail = "historical recovery is strictly stronger than current catalog state"
        if not row.stockify_run_id or not row.stock_clip_id:
            safety = MALFORMED
            safety_detail = "missing stockify_run_id or stock_clip_id"
        elif malformed_reason:
            safety = MALFORMED
            safety_detail = malformed_reason
        elif candidate is None:
            safety = MISSING_CANDIDATE
            safety_detail = "no stock_candidates row for (stockify_run_id, stock_clip_id)"
        elif current_rank > historical_rank:
            safety = STRONGER_CURRENT
            safety_detail = (
                f"current {current_kind} outranks historical {historical_kind}"
            )
        elif _already_applied(candidate, row, proposed):
            safety = ALREADY_APPLIED
            safety_detail = "catalog already matches the historical recovery"
        elif current_rank == historical_rank and current_rank > RANK_UNKNOWN:
            if not _locations_equivalent(current_location, proposed, row):
                safety = CONFLICTING_CURRENT
                safety_detail = (
                    f"current {current_kind} disagrees with historical {historical_kind}"
                )
        elif current_rank == RANK_UNKNOWN:
            safety = SAFE_TO_RESTORE
            safety_detail = "current catalog location is unknown/empty"
        elif current_rank < historical_rank:
            safety = SAFE_TO_RESTORE
            safety_detail = (
                f"historical {historical_kind} outranks current {current_kind}"
            )

        source_identity = _source_identity(row, candidate)
        return {
            "run_id": row.stockify_run_id,
            "stockify_run_id": row.stockify_run_id,
            "stock_clip_id": row.stock_clip_id,
            "session_id": (candidate or {}).get("session_id"),
            "source_media": row.source_media or (candidate or {}).get("source_filename"),
            "source_identity": source_identity,
            "eligibility_status": (candidate or {}).get("eligibility_status"),
            "recovery_reason": row.recovery_reason,
            "confidence": row.resolution_confidence,
            "provenance": dict(row.provenance or {}),
            "historical_new_event_name": row.new_event_name,
            "historical_new_project_name": row.new_project_name,
            "historical_original_event_name": row.original_event_name,
            "historical_original_project_name": row.original_project_name,
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
                "generated_event_name": row.new_event_name,
                "generated_project_name": row.new_project_name,
                "location": proposed,
            },
            "representative_lat": row.representative_lat,
            "representative_lon": row.representative_lon,
            "safety_class": safety,
            "safety_detail": safety_detail,
            "provenance_notes": provenance_notes,
            "capture_date": _capture_date_for_row(row, candidate),
            "matched": candidate is not None,
            "recovery_row": row.as_recovery_row(location=proposed),
        }

    def _build_report(
        self,
        *,
        plan_path: Path,
        forensic_json: Path,
        review_root: Path | None,
        plan_rows: list[HistoricalPlanRow],
        mutations: list[dict[str, Any]],
        read_only: bool,
        dry_run: bool,
        hours_in_silence_run_id: str,
        hours_in_silence_capture_date: str,
        hours_in_silence_session_id: str,
    ) -> HistoricalLocationRestoreReport:
        by_class = Counter(str(item["safety_class"]) for item in mutations)
        by_reason = Counter(str(item["recovery_reason"] or "") for item in mutations)
        matched = sum(1 for item in mutations if item["matched"])
        serializable = [_public_mutation(item) for item in mutations]
        report = HistoricalLocationRestoreReport(
            mode=MODE,
            read_only=read_only,
            dry_run=dry_run,
            db_path=str(self.repository.database.path),
            plan_path=str(plan_path),
            forensic_json=str(forensic_json),
            review_root=str(review_root) if review_root else None,
            plan_mutations=len(plan_rows),
            matched_candidates=matched,
            missing_candidates=by_class.get(MISSING_CANDIDATE, 0),
            safe_to_restore=by_class.get(SAFE_TO_RESTORE, 0),
            already_applied=by_class.get(ALREADY_APPLIED, 0),
            stronger_current_evidence=by_class.get(STRONGER_CURRENT, 0),
            conflicting_current_evidence=by_class.get(CONFLICTING_CURRENT, 0),
            malformed_historical_recovery=by_class.get(MALFORMED, 0),
            by_recovery_reason=dict(sorted(by_reason.items())),
            by_safety_class={key: by_class.get(key, 0) for key in SAFETY_CLASSES},
            candidate_universe=dict(RESTORE_MUTATION_UNIVERSE),
            hours_in_silence=_hours_in_silence_section(
                mutations,
                run_id=hours_in_silence_run_id,
                capture_date=hours_in_silence_capture_date,
                session_id=hours_in_silence_session_id,
            ),
            coverage_before=_coverage_from_snapshots(mutations, use_current=True),
            coverage_after=_coverage_from_snapshots(mutations, use_current=False),
            mutations=serializable,
        )
        # Keep recovery_row attached for the write path.
        for public, original in zip(report.mutations, mutations, strict=True):
            public["_recovery_row"] = original["recovery_row"]
            public["_session_id"] = original.get("session_id")
        return report

    def _load_candidates_readonly(
        self, pairs: set[tuple[str, str]]
    ) -> dict[tuple[str, str], dict[str, Any]]:
        unique = sorted(pair for pair in pairs if pair[0] and pair[1])
        if not unique:
            return {}
        placeholders = ", ".join("(?, ?)" for _ in unique)
        parameters: list[Any] = []
        for run_id, clip_id in unique:
            parameters.extend([run_id, clip_id])
        query = f"""
            SELECT
                c.run_id,
                c.stock_clip_id,
                c.session_id,
                c.eligibility_status,
                c.location_json,
                c.generated_event_name,
                c.generated_project_label,
                c.generated_clip_project_name,
                c.source_name,
                m.original_filename AS source_filename,
                m.normalized_stem AS source_normalized_stem,
                s.capture_date AS session_capture_date,
                r.recovery_reason AS existing_recovery_reason
            FROM stock_candidates c
            LEFT JOIN source_media m ON m.id=c.source_media_id
            LEFT JOIN shoot_sessions s ON s.id=c.session_id
            LEFT JOIN review_location_recoveries r
                ON r.stockify_run_id=c.run_id AND r.stock_clip_id=c.stock_clip_id
            WHERE (c.run_id, c.stock_clip_id) IN ({placeholders})
        """
        connection = self._readonly_connect()
        try:
            rows = connection.execute(query, parameters).fetchall()
        finally:
            connection.close()
        loaded: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            item["location"] = json_loads(item.pop("location_json"), {})
            loaded[(str(item["run_id"]), str(item["stock_clip_id"]))] = item
        return loaded

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

    def _coverage_for_mutations(
        self,
        mutations: list[dict[str, Any]],
        *,
        after_write: bool,
    ) -> dict[str, Any]:
        if not after_write:
            return _coverage_from_snapshots(mutations, use_current=True)
        pairs = [
            (item["stockify_run_id"], item["stock_clip_id"]) for item in mutations
        ]
        current = self._load_candidates_readonly(set(pairs))
        known = 0
        unknown = 0
        for item in mutations:
            row = current.get((item["stockify_run_id"], item["stock_clip_id"]))
            event = str((row or {}).get("generated_event_name") or "")
            if is_unknown_event_name(event):
                unknown += 1
            else:
                known += 1
        return {
            "universe": dict(RESTORE_MUTATION_UNIVERSE),
            "known": known,
            "unknown": unknown,
            "total": len(mutations),
        }

    def _post_write_audit(
        self,
        *,
        mutations: list[dict[str, Any]],
        written_keys: set[tuple[str, str]],
        before: dict[tuple[str, str], tuple[str, str, str]],
        after: dict[tuple[str, str], tuple[str, str, str]],
        recoveries_before: dict[tuple[str, str], dict[str, Any]],
        recoveries_after: dict[tuple[str, str], dict[str, Any]],
        hours_in_silence_run_id: str,
        hours_in_silence_capture_date: str,
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
        jpg_direct_flags = []
        provenance_ok = True
        for item in mutations:
            if item["safety_class"] != SAFE_TO_RESTORE:
                continue
            key = (item["stockify_run_id"], item["stock_clip_id"])
            persisted = recoveries_after.get(key) or {}
            if persisted.get("recovery_reason") != item["recovery_reason"]:
                provenance_ok = False
            location = ((persisted.get("provenance") or {}).get("location") or {})
            if item["recovery_reason"] in JPG_REASONS:
                if location.get("direct_source_gps") is not False:
                    jpg_direct_flags.append(f"{key[0]}:{key[1]}")
                if location.get("gps_kind") != GPS_KIND_JPG:
                    jpg_direct_flags.append(f"{key[0]}:{key[1]}:gps_kind")
        hours_rows = [
            item
            for item in mutations
            if item["stockify_run_id"] == hours_in_silence_run_id
            and item.get("capture_date") == hours_in_silence_capture_date
        ]
        hours_written = sum(
            1
            for item in hours_rows
            if (item["stockify_run_id"], item["stock_clip_id"]) in written_keys
            or item["safety_class"] == ALREADY_APPLIED
        )
        return {
            "intended_rows_written": len(written_keys),
            "intended_rows_unchanged": intended_missing,
            "unintended_rows_changed": unintended,
            "unintended_change_universe": dict(ALL_CANDIDATES_UNIVERSE),
            "review_location_recoveries_before": len(recoveries_before),
            "review_location_recoveries_after": len(recoveries_after),
            "review_location_recoveries_upserted": len(written_keys),
            "provenance_round_trip_ok": provenance_ok,
            "jpg_direct_source_gps_violations": jpg_direct_flags,
            "hours_in_silence_restored": hours_written,
            "hours_in_silence_expected": len(hours_rows),
            "restore_impact_universe": dict(RESTORE_MUTATION_UNIVERSE),
        }

    def _cross_check_review_root(
        self,
        *,
        review_root: Path,
        plan_rows: list[HistoricalPlanRow],
    ) -> dict[str, Any]:
        root = review_root.expanduser().resolve()
        if not root.is_dir():
            raise VClipError(f"Review root does not exist: {root}")
        represented: set[tuple[str, str]] = set()
        names: dict[tuple[str, str], dict[str, str]] = {}
        xml_identities: set[tuple[str, str]] = set()
        for xml_path in sorted(root.rglob("*.fcpxml")):
            relative = xml_path.relative_to(root).as_posix()
            manifest_path = xml_path.with_name(f"{xml_path.stem}-shard-manifest.json")
            if manifest_path.is_file():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise VClipError(
                        f"Could not read shard manifest {manifest_path}: {exc}"
                    ) from exc
                run_id = str(manifest.get("stockify_run_id") or "")
                for project in manifest.get("projects") or []:
                    if project.get("representation") == "compilation":
                        continue
                    if "Stock Compilation" in str(project.get("project_name") or ""):
                        continue
                    event_name = str(project.get("event_name") or "")
                    project_name = str(project.get("project_name") or "")
                    for clip_id in project.get("stock_clip_ids") or []:
                        key = (run_id, str(clip_id))
                        represented.add(key)
                        names[key] = {
                            "event_name": event_name,
                            "project_name": project_name,
                            "relative_xml": relative,
                            "source": "shard_manifest",
                        }
            try:
                tree = ET.parse(xml_path)
            except ET.ParseError:
                continue
            root_el = tree.getroot()
            parent_map = {child: parent for parent in root_el.iter() for child in parent}
            for node in root_el.iter():
                metadata = read_vclip_metadata(node)
                clip_id = metadata.get("com.vclip.stock_clip_id")
                run_id = metadata.get("com.vclip.stockify_run_id")
                if clip_id and run_id:
                    xml_identities.add((run_id, clip_id))
                    represented.add((run_id, clip_id))
                    if (run_id, clip_id) not in names:
                        event = _nearest_named_ancestor(node, "event", parent_map)
                        project = _nearest_named_ancestor(node, "project", parent_map)
                        names[(run_id, clip_id)] = {
                            "event_name": event.get("name") if event is not None else "",
                            "project_name": project.get("name") if project is not None else "",
                            "relative_xml": relative,
                            "source": "fcpxml_vclip_metadata",
                        }
        missing = []
        mismatches = []
        present = 0
        for row in plan_rows:
            key = row.identity
            if key not in represented:
                missing.append(
                    {
                        "stockify_run_id": row.stockify_run_id,
                        "stock_clip_id": row.stock_clip_id,
                    }
                )
                continue
            present += 1
            observed = names.get(key) or {}
            observed_event = str(observed.get("event_name") or "")
            if (
                row.new_event_name
                and observed_event
                and observed_event != row.new_event_name
            ):
                mismatches.append(
                    {
                        "stockify_run_id": row.stockify_run_id,
                        "stock_clip_id": row.stock_clip_id,
                        "historical_new_event_name": row.new_event_name,
                        "corpus_event_name": observed_event,
                        "relative_xml": observed.get("relative_xml"),
                    }
                )
        return {
            "read_only": True,
            "review_root": str(root),
            "plan_rows_represented_in_final_corpus": present,
            "missing_from_final_corpus": missing,
            "missing_from_final_corpus_count": len(missing),
            "name_mismatches": mismatches,
            "name_mismatch_count": len(mismatches),
            "xml_vclip_metadata_identities": len(xml_identities),
        }


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coords_match(
    lat_a: float | None, lon_a: float | None, lat_b: float | None, lon_b: float | None
) -> bool:
    if not is_usable_gps(lat_a, lon_a) and not is_usable_gps(lat_b, lon_b):
        return True
    if not is_usable_gps(lat_a, lon_a) or not is_usable_gps(lat_b, lon_b):
        return False
    return (
        abs(float(lat_a) - float(lat_b)) <= COORD_MATCH_EPSILON
        and abs(float(lon_a) - float(lon_b)) <= COORD_MATCH_EPSILON
    )


def _source_identity(
    row: HistoricalPlanRow, candidate: dict[str, Any] | None
) -> dict[str, Any]:
    source_media = row.source_media or (candidate or {}).get("source_filename")
    stem = normalized_stem(
        str(source_media or (candidate or {}).get("source_normalized_stem") or "")
    )
    return {
        "source_media": source_media,
        "normalized_stem": stem,
        "stock_clip_id": row.stock_clip_id,
    }


def _capture_date_for_row(
    row: HistoricalPlanRow, candidate: dict[str, Any] | None
) -> str | None:
    provenance = row.provenance or {}
    if provenance.get("capture_date"):
        return str(provenance["capture_date"])[:10]
    for name in (row.new_event_name, row.original_event_name):
        if " — " in name:
            tail = name.rsplit(" — ", 1)[-1].strip()
            if len(tail) >= 10 and tail[0:4].isdigit():
                return tail[:10]
    session_date = (candidate or {}).get("session_capture_date")
    if session_date:
        return str(session_date)[:10]
    return None


def _proposed_location(
    row: HistoricalPlanRow,
    forensic_index: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any], list[str], str | None]:
    notes: list[str] = []
    reason = row.recovery_reason
    forensic = lookup_forensic_evidence(
        forensic_index,
        stock_clip_id=row.stock_clip_id,
        source_media=row.source_media,
    )
    location = dict((row.provenance or {}).get("location") or {})
    if reason in JPG_REASONS:
        if forensic is None and not location:
            return {}, notes, "JPG recovery lacks plan location and forensic source evidence"
        if forensic is not None:
            forensic_lat = _optional_float(forensic.get("latitude"))
            forensic_lon = _optional_float(forensic.get("longitude"))
            plan_lat = row.representative_lat
            plan_lon = row.representative_lon
            if plan_lat is None:
                plan_lat = _optional_float(((row.provenance or {}).get("representative_gps") or {}).get("lat"))
            if plan_lon is None:
                plan_lon = _optional_float(((row.provenance or {}).get("representative_gps") or {}).get("lon"))
            if is_usable_gps(plan_lat, plan_lon) and is_usable_gps(forensic_lat, forensic_lon):
                if not _coords_match(plan_lat, plan_lon, forensic_lat, forensic_lon):
                    return (
                        location,
                        notes,
                        "plan coordinates disagree with forensic JPG evidence",
                    )
            plan_conf = str(row.resolution_confidence or "").lower()
            forensic_conf = str(forensic.get("confidence") or "").lower()
            if plan_conf and forensic_conf and plan_conf != forensic_conf:
                return (
                    location,
                    notes,
                    "plan confidence disagrees with forensic JPG evidence",
                )
            if not location:
                if not is_usable_gps(forensic_lat, forensic_lon):
                    return {}, notes, "forensic JPG evidence lacks usable coordinates"
                location = _location_from_persisted_evidence(
                    forensic, float(forensic_lat), float(forensic_lon)
                )
                notes.append("constructed location from forensic source-level evidence")
            jpg_payload = (row.provenance or {}).get("jpg_exif_same_shoot")
            if not isinstance(jpg_payload, dict) or not jpg_payload:
                loc_prov = dict(row.provenance or {})
                loc_prov["jpg_exif_same_shoot"] = _jpg_payload(forensic)
                row.provenance = loc_prov
                notes.append("joined jpg_exif_same_shoot payload from forensic JSON")
        if not is_usable_gps(location.get("center_lat"), location.get("center_lon")):
            if is_usable_gps(row.representative_lat, row.representative_lon) and forensic is not None:
                location = _location_from_persisted_evidence(
                    forensic, float(row.representative_lat), float(row.representative_lon)
                )
            else:
                return location, notes, "JPG recovery has no usable inferred coordinates"
        location["gps_kind"] = GPS_KIND_JPG
        location["direct_source_gps"] = False
        sources = list(location.get("evidence_sources") or [])
        for required in (JPG_EXIF_REASON, MATERIALIZE_EVIDENCE):
            if required not in sources:
                sources.append(required)
        if reason == STALE_CORRECTION_REASON and STALE_CORRECTION_REASON not in sources:
            sources.append(STALE_CORRECTION_REASON)
        location["evidence_sources"] = sources
        if location.get("direct_source_gps") is True:
            return location, notes, "JPG recovery must not claim direct_source_gps"
        row.provenance = dict(row.provenance or {})
        row.provenance["location"] = location
        row.provenance["direct_source_gps"] = False
        row.provenance["gps_kind"] = GPS_KIND_JPG
        row.provenance.setdefault("evidence_sources", list(sources))
        return location, notes, None

    if reason == EDITORIAL_CONSENSUS_REASON:
        if not location:
            return {}, notes, "editorial_group_consensus recovery lacks provenance.location"
        sources = list(location.get("evidence_sources") or [])
        if JPG_EXIF_REASON in sources or location.get("gps_kind") in INFERRED_GPS_KINDS:
            return location, notes, "editorial_group_consensus must not claim JPG evidence"
        if location.get("direct_source_gps") is True:
            return location, notes, "editorial_group_consensus must not claim direct_source_gps"
        if EDITORIAL_CONSENSUS_REASON not in sources:
            sources.append(EDITORIAL_CONSENSUS_REASON)
        if MATERIALIZE_EVIDENCE not in sources:
            sources.append(MATERIALIZE_EVIDENCE)
        location["evidence_sources"] = sources
        location["direct_source_gps"] = False
        location["gps_kind"] = location.get("gps_kind") or None
        row.provenance = dict(row.provenance or {})
        row.provenance["location"] = location
        row.provenance["recovery_reason"] = EDITORIAL_CONSENSUS_REASON
        return location, notes, None

    if not reason:
        return location, notes, "historical recovery_reason is missing"
    if not location:
        return {}, notes, f"historical recovery {reason} lacks provenance.location"
    return location, notes, None


def _already_applied(
    candidate: dict[str, Any],
    row: HistoricalPlanRow,
    proposed: dict[str, Any],
) -> bool:
    current = dict(candidate.get("location") or {})
    current_event = str(candidate.get("generated_event_name") or "")
    current_project = str(
        candidate.get("generated_clip_project_name")
        or candidate.get("generated_project_name")
        or ""
    )
    if current_event != row.new_event_name:
        return False
    if current_project and row.new_project_name and current_project != row.new_project_name:
        return False
    return _locations_equivalent(current, proposed, row)


def _locations_equivalent(
    current: dict[str, Any],
    proposed: dict[str, Any],
    row: HistoricalPlanRow,
) -> bool:
    if is_unknown_event_name(str((current or {}).get("public_label") or "")):
        if not is_usable_gps(current.get("center_lat"), current.get("center_lon")):
            return False
    if not _coords_match(
        current.get("center_lat"),
        current.get("center_lon"),
        proposed.get("center_lat"),
        proposed.get("center_lon"),
    ):
        return False
    current_kind = str(current.get("gps_kind") or "")
    proposed_kind = str(proposed.get("gps_kind") or "")
    if proposed_kind and current_kind and current_kind != proposed_kind:
        return False
    current_label = str(current.get("public_label") or "")
    proposed_label = str(proposed.get("public_label") or "")
    if proposed_label and current_label and current_label != proposed_label:
        return False
    if row.recovery_reason in JPG_REASONS:
        sources = {str(value) for value in (current.get("evidence_sources") or [])}
        if JPG_EXIF_REASON not in sources and current.get("gps_kind") != GPS_KIND_JPG:
            return False
        if current.get("direct_source_gps") is True:
            return False
    if row.recovery_reason == EDITORIAL_CONSENSUS_REASON:
        sources = {str(value) for value in (current.get("evidence_sources") or [])}
        if EDITORIAL_CONSENSUS_REASON not in sources:
            return False
    return True


def _hours_in_silence_section(
    mutations: list[dict[str, Any]],
    *,
    run_id: str,
    capture_date: str,
    session_id: str,
) -> dict[str, Any]:
    scoped = [
        item
        for item in mutations
        if item["stockify_run_id"] == run_id and item.get("capture_date") == capture_date
    ]
    jpg_rows = [item for item in scoped if item["recovery_reason"] == JPG_EXIF_REASON]
    consensus_rows = [
        item for item in scoped if item["recovery_reason"] == EDITORIAL_CONSENSUS_REASON
    ]
    screen = [
        item
        for item in scoped
        if HOURS_IN_SILENCE_SCREEN_RECORDING.lower()
        in str(item.get("source_media") or "").lower()
    ]
    source_stems = sorted(
        {
            str((item.get("source_identity") or {}).get("normalized_stem") or "")
            for item in scoped
            if (item.get("source_identity") or {}).get("normalized_stem")
        }
    )
    source_media = sorted(
        {
            str(item.get("source_media") or "")
            for item in scoped
            if item.get("source_media")
        }
    )
    screen_ok = True
    screen_detail = []
    for item in screen:
        proposed = (item.get("proposed_location_snapshot") or {}).get("location") or {}
        sources = {str(value) for value in (proposed.get("evidence_sources") or [])}
        claims_jpg = JPG_EXIF_REASON in sources or proposed.get("gps_kind") in INFERRED_GPS_KINDS
        claims_direct = proposed.get("direct_source_gps") is True
        if item["recovery_reason"] != EDITORIAL_CONSENSUS_REASON or claims_jpg or claims_direct:
            screen_ok = False
        screen_detail.append(
            {
                "stock_clip_id": item["stock_clip_id"],
                "recovery_reason": item["recovery_reason"],
                "claims_jpg_evidence": claims_jpg,
                "direct_source_gps": proposed.get("direct_source_gps"),
                "gps_kind": proposed.get("gps_kind"),
            }
        )
    return {
        "stockify_run_id": run_id,
        "session_id": session_id,
        "capture_date": capture_date,
        "historical_mutations": len(scoped),
        "matched_candidates": sum(1 for item in scoped if item.get("matched")),
        "jpg_mutations": len(jpg_rows),
        "consensus_mutations": len(consensus_rows),
        "missing": sum(1 for item in scoped if item["safety_class"] == MISSING_CANDIDATE),
        "unique_stock_clip_ids": len({item["stock_clip_id"] for item in scoped}),
        "unique_source_identities": len(source_stems),
        "unique_source_media": source_media,
        "source_normalized_stems": source_stems,
        "screen_recording": {
            "filename": HOURS_IN_SILENCE_SCREEN_RECORDING,
            "rows": screen_detail,
            "editorial_group_consensus": bool(screen)
            and all(
                item["recovery_reason"] == EDITORIAL_CONSENSUS_REASON for item in screen
            ),
            "no_fake_direct_or_jpg_gps": screen_ok,
        },
        "safety_classes": dict(Counter(item["safety_class"] for item in scoped)),
    }


def _coverage_from_snapshots(
    mutations: list[dict[str, Any]], *, use_current: bool
) -> dict[str, Any]:
    known = 0
    unknown = 0
    for item in mutations:
        snapshot = (
            item.get("old_location_snapshot")
            if use_current
            else item.get("proposed_location_snapshot")
        ) or {}
        event = str(snapshot.get("generated_event_name") or "")
        if is_unknown_event_name(event):
            unknown += 1
        else:
            known += 1
    return {
        "universe": dict(RESTORE_MUTATION_UNIVERSE),
        "known": known,
        "unknown": unknown,
        "total": len(mutations),
    }


def _public_mutation(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": item["run_id"],
        "stockify_run_id": item["stockify_run_id"],
        "stock_clip_id": item["stock_clip_id"],
        "session_id": item.get("session_id"),
        "source_media": item.get("source_media"),
        "source_identity": item.get("source_identity"),
        "eligibility_status": item.get("eligibility_status"),
        "recovery_reason": item.get("recovery_reason"),
        "confidence": item.get("confidence"),
        "provenance": item.get("provenance"),
        "old_location_snapshot": item.get("old_location_snapshot"),
        "proposed_location_snapshot": item.get("proposed_location_snapshot"),
        "historical_new_event_name": item.get("historical_new_event_name"),
        "historical_new_project_name": item.get("historical_new_project_name"),
        "current_generated_event_name": item.get("current_generated_event_name"),
        "current_generated_project_name": item.get("current_generated_project_name"),
        "current_evidence_kind": item.get("current_evidence_kind"),
        "safety_class": item.get("safety_class"),
        "safety_detail": item.get("safety_detail"),
        "provenance_notes": item.get("provenance_notes"),
        "capture_date": item.get("capture_date"),
        "matched": item.get("matched"),
        "representative_lat": item.get("representative_lat"),
        "representative_lon": item.get("representative_lon"),
    }


def _recovery_from_mutation(mutation: dict[str, Any]) -> LocationRecoveryRow:
    row = mutation.get("_recovery_row")
    if isinstance(row, LocationRecoveryRow):
        proposed = (mutation.get("proposed_location_snapshot") or {}).get("location")
        if proposed:
            row.provenance = dict(row.provenance or {})
            row.provenance["location"] = proposed
        return row
    raise VClipError("Restore mutation is missing the historical recovery row.")


def _conservative_session_summaries(
    mutations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in mutations:
        session_id = item.get("_session_id") or item.get("session_id")
        if not session_id:
            continue
        by_session[str(session_id)].append(item)
    summaries: list[dict[str, Any]] = []
    for session_id, members in by_session.items():
        locations = [
            (item.get("proposed_location_snapshot") or {}).get("location") or {}
            for item in members
        ]
        cities = {str(loc.get("city") or "") for loc in locations if loc.get("city")}
        states = {str(loc.get("state") or "") for loc in locations if loc.get("state")}
        countries = {str(loc.get("country") or "") for loc in locations if loc.get("country")}
        neighborhoods = {
            str(loc.get("neighborhood") or "")
            for loc in locations
            if loc.get("neighborhood")
        }
        if len(cities) != 1:
            continue
        city = next(iter(cities))
        state = next(iter(states)) if len(states) == 1 else None
        country = next(iter(countries)) if len(countries) == 1 else None
        neighborhood = next(iter(neighborhoods)) if len(neighborhoods) == 1 else None
        if neighborhood and any(not loc.get("neighborhood") for loc in locations):
            neighborhood = None
        public_label = (
            f"{neighborhood}, {city}"
            if neighborhood
            else (f"{city}, {state}" if state else city)
        )
        coords = [
            (float(loc["center_lat"]), float(loc["center_lon"]))
            for loc in locations
            if is_usable_gps(loc.get("center_lat"), loc.get("center_lon"))
        ]
        center_lat = None
        center_lon = None
        if neighborhood and coords:
            center_lat = round(sum(lat for lat, _lon in coords) / len(coords), 6)
            center_lon = round(sum(lon for _lat, lon in coords) / len(coords), 6)
        summaries.append(
            {
                "session_id": session_id,
                "city": city,
                "state": state,
                "country": country,
                "neighborhood": neighborhood,
                "public_label": public_label,
                "center_lat": center_lat,
                "center_lon": center_lon,
                "location_json": {
                    "status": "resolved",
                    "confidence": "medium",
                    "evidence_sources": ["historical_location_restore_session_summary"],
                    "direct_source_gps": False,
                    "gps_kind": None,
                    "city": city,
                    "state": state,
                    "country": country,
                    "neighborhood": neighborhood,
                    "public_label": public_label,
                    "center_lat": center_lat,
                    "center_lon": center_lon,
                    "note": (
                        "Conservative session summary derived from restored clip "
                        "members. Clip-level geography remains authoritative."
                    ),
                },
            }
        )
    return summaries


def _persist_session_summaries(
    connection: sqlite3.Connection, summaries: list[dict[str, Any]]
) -> None:
    now = utc_now()
    for item in summaries:
        connection.execute(
            """
            UPDATE shoot_sessions
            SET city=?,
                state=?,
                country=?,
                neighborhood=?,
                public_label=?,
                center_lat=?,
                center_lon=?,
                location_json=?,
                updated_at=?
            WHERE id=?
            """,
            (
                item["city"],
                item["state"],
                item["country"],
                item["neighborhood"],
                item["public_label"],
                item["center_lat"],
                item["center_lon"],
                json_dumps(item["location_json"]),
                now,
                item["session_id"],
            ),
        )


def _nearest_named_ancestor(
    node: ET.Element,
    tag: str,
    parent_map: dict[ET.Element, ET.Element],
) -> ET.Element | None:
    current: ET.Element | None = node
    while current is not None:
        if local_name(current.tag) == tag:
            return current
        current = parent_map.get(current)
    return None


def format_restore_report_text(report: HistoricalLocationRestoreReport) -> str:
    hours = report.hours_in_silence or {}
    screen = hours.get("screen_recording") or {}
    universe = report.candidate_universe or {}
    lines = [
        "HISTORICAL LOCATION RESTORE VALIDATION",
        "=" * 72,
        f"Mode:                    {report.mode}",
        f"Read only:               {report.read_only}",
        f"Database:                {report.db_path}",
        f"Plan:                    {report.plan_path}",
        f"Forensic JSON:           {report.forensic_json}",
        f"Backup:                  {report.backup_path or '(none; validate/dry-run)'}",
        "",
        "Candidate universe",
        "------------------",
        f"Name:                    {universe.get('universe_name')}",
        f"Dedupe key:              {universe.get('dedupe_key')}",
        f"Latest-run semantics:    {universe.get('latest_run_semantics')}",
        f"Accepted eligibility:    {universe.get('accepted_eligibility_semantics')}",
        "",
        "Plan vs catalog",
        "---------------",
        f"Plan mutations:          {report.plan_mutations}",
        f"Matched candidates:      {report.matched_candidates}",
        f"Missing candidates:      {report.missing_candidates}",
        "",
        "Safety classification",
        "---------------------",
        f"safe_to_restore:              {report.safe_to_restore}",
        f"already_applied:              {report.already_applied}",
        f"stronger_current_evidence:    {report.stronger_current_evidence}",
        f"conflicting_current_evidence: {report.conflicting_current_evidence}",
        f"malformed_historical_recovery: {report.malformed_historical_recovery}",
        "",
        "By recovery reason",
        "------------------",
    ]
    for reason, count in sorted((report.by_recovery_reason or {}).items()):
        lines.append(f"  {reason}: {count}")
    lines.extend(
        [
            "",
            "Hours in Silence",
            "----------------",
            f"Run:                     {hours.get('stockify_run_id')}",
            f"Capture date:            {hours.get('capture_date')}",
            f"Historical mutations:    {hours.get('historical_mutations')}",
            f"Matched candidates:      {hours.get('matched_candidates')}",
            f"JPG mutations:           {hours.get('jpg_mutations')}",
            f"Consensus mutations:     {hours.get('consensus_mutations')}",
            f"Missing:                 {hours.get('missing')}",
            f"Unique stock_clip_ids:   {hours.get('unique_stock_clip_ids')}",
            f"Unique source identities:{hours.get('unique_source_identities')}",
            f"Screen recording:        {screen.get('filename')}",
            f"  editorial_group_consensus: {screen.get('editorial_group_consensus')}",
            f"  no fake JPG/direct GPS:    {screen.get('no_fake_direct_or_jpg_gps')}",
        ]
    )
    if report.fcpxml_cross_check:
        xml = report.fcpxml_cross_check
        lines.extend(
            [
                "",
                "Final FCPXML corpus (read-only)",
                "-------------------------------",
                f"Represented:             {xml.get('plan_rows_represented_in_final_corpus')}",
                f"Missing from corpus:     {xml.get('missing_from_final_corpus_count')}",
                f"Name mismatches:         {xml.get('name_mismatch_count')}",
            ]
        )
    if report.coverage_before:
        lines.extend(
            [
                "",
                "Coverage on restore-target universe",
                "-----------------------------------",
                (
                    "Before:                  "
                    f"known={report.coverage_before.get('known')} "
                    f"unknown={report.coverage_before.get('unknown')}"
                ),
                (
                    "After (projected):       "
                    f"known={report.coverage_after.get('known')} "
                    f"unknown={report.coverage_after.get('unknown')}"
                ),
            ]
        )
    if report.post_write_audit:
        audit = report.post_write_audit
        lines.extend(
            [
                "",
                "Post-write audit",
                "----------------",
                f"Intended rows written:   {audit.get('intended_rows_written')}",
                f"Unintended rows changed: {len(audit.get('unintended_rows_changed') or [])}",
                f"Recoveries upserted:     {audit.get('review_location_recoveries_upserted')}",
                f"Provenance round-trip:   {audit.get('provenance_round_trip_ok')}",
                (
                    "Hours in Silence:        "
                    f"{audit.get('hours_in_silence_restored')}/"
                    f"{audit.get('hours_in_silence_expected')}"
                ),
            ]
        )
    if report.write_blocked_reason:
        lines.extend(["", f"Write blocked:           {report.write_blocked_reason}"])
    return "\n".join(lines) + "\n"
