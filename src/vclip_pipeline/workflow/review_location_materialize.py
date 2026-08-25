"""Materialize persisted JPG/editorial forensic location knowledge into a new shard root.

Consumes library-audits reports only — does not rediscover location from mounted media.
"""

from __future__ import annotations

import json
import tempfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..db.repository import CatalogRepository
from ..errors import VClipError
from ..stockify.location_recovery import _relabel_project
from ..stockify.naming import TIME_LABELS, event_base_name, project_base_label
from ..util import json_dumps, safe_filename, utc_now
from .catalog import WorkflowCatalog
from .editorial_group_forensics import country_for_admin_area
from .physical_location_coverage import (
    build_physical_audit,
    collect_physical_candidates,
    is_unknown_event_name,
)
from .projected_location_coverage import (
    MUTATION_STATES,
    classify_corpus_candidates,
)
from .review_color_integrity import ReviewColorIntegrityService
from .review_location_recover import (
    JPG_EXIF_REASON,
    LocationRecoveryRow,
    ReviewLocationRecoverService,
    STATUS_FORENSIC_JPG,
    _event_capture_date,
    _structured_location,
)

EDITORIAL_CONSENSUS_REASON = "editorial_group_consensus"
STALE_CORRECTION_REASON = "stale_location_requires_correction"
MATERIALIZE_MODE = "review_location_materialize"

EXPECTED_MUTATION_COUNTS = {
    "recoverable_jpg_exif": 156,
    "stale_location_requires_correction": 8,
    "recoverable_group_consensus": 1,
}
# Physical manifest expectations after a successful materialize write.
PHYSICAL_POST_WRITE_EXPECTED = {
    "total_candidates": 3427,
    "known": 3372,
    "unknown": 55,
    "duplicates": 0,
    "unknown_to_known_mutations": 157,  # 156 jpg + 1 consensus
    "known_correction_mutations": 8,
    "total_mutations": 165,
}
COLOR_BASELINE_AGGREGATES = {
    "db_lut_xml_lut": 1624,
    "db_lut_xml_no_lut": 12,
    "db_no_lut_xml_lut": 1768,
    "db_no_lut_xml_no_lut": 23,
    "unresolved_identities": 0,
    "post_may_2025_db_lut_xml_missing": 8,
}


@dataclass
class ReviewLocationMaterializeReport:
    input_root: str
    output_root: str
    dry_run: bool
    forensic_json: str
    projected_coverage_json: str | None
    plan_json: str | None = None
    plan_text: str | None = None
    total_candidate_mutations: int = 0
    mutations_by_type: dict[str, int] = field(default_factory=dict)
    shards_that_would_change: list[str] = field(default_factory=list)
    shards_changed: int = 0
    shards_unchanged: int = 0
    shards_failed: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)
    candidates_before_known: int = 0
    candidates_before_unknown: int = 0
    candidates_after_known: int = 0
    candidates_after_unknown: int = 0
    accepted_unresolved_drone_clip_ids: list[str] = field(default_factory=list)
    out_of_scope_non_drone_clip_ids: list[str] = field(default_factory=list)
    other_unknown_clip_ids: list[str] = field(default_factory=list)
    unchanged_known_existing: int = 0
    unchanged_accepted_unresolved_drone: int = 0
    unchanged_out_of_scope_non_drone: int = 0
    physical_manifest_audit: dict[str, Any] = field(default_factory=dict)
    remaining_unknown_classification: dict[str, Any] = field(default_factory=dict)
    plan_rows: list[dict[str, Any]] = field(default_factory=list)
    recoveries: list[dict[str, Any]] = field(default_factory=list)
    dry_run_checks: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": MATERIALIZE_MODE,
            "mutates_corpus": not self.dry_run,
            "input_root": self.input_root,
            "output_root": self.output_root,
            "dry_run": self.dry_run,
            "forensic_json": self.forensic_json,
            "projected_coverage_json": self.projected_coverage_json,
            "plan_json": self.plan_json,
            "plan_text": self.plan_text,
            "total_candidate_mutations": self.total_candidate_mutations,
            "mutations_by_type": dict(self.mutations_by_type),
            "shards_that_would_change": list(self.shards_that_would_change),
            "shards_changed": self.shards_changed,
            "shards_unchanged": self.shards_unchanged,
            "shards_failed": self.shards_failed,
            "failures": list(self.failures),
            "candidates_before_physical": {
                "known": self.candidates_before_known,
                "unknown": self.candidates_before_unknown,
            },
            # Backward-compatible aliases.
            "candidates_before": {
                "known": self.candidates_before_known,
                "unknown": self.candidates_before_unknown,
            },
            "candidates_after_physical": {
                "known": self.candidates_after_known,
                "unknown": self.candidates_after_unknown,
            },
            "candidates_after_projected": {
                "known": self.candidates_after_known,
                "unknown": self.candidates_after_unknown,
            },
            "accepted_unresolved_drone_clip_ids": list(
                self.accepted_unresolved_drone_clip_ids
            ),
            "out_of_scope_non_drone_clip_ids": list(
                self.out_of_scope_non_drone_clip_ids
            ),
            "other_unknown_clip_ids": list(self.other_unknown_clip_ids),
            "remaining_unknown_classification": dict(
                self.remaining_unknown_classification
            ),
            "physical_manifest_audit": dict(self.physical_manifest_audit),
            "leave_unchanged": {
                "known_existing": self.unchanged_known_existing,
                "accepted_unresolved_drone": self.unchanged_accepted_unresolved_drone,
                "out_of_scope_non_drone": self.unchanged_out_of_scope_non_drone,
            },
            "plan_rows": list(self.plan_rows),
            "recoveries": list(self.recoveries),
            "dry_run_checks": dict(self.dry_run_checks),
            "warnings": list(self.warnings),
        }


class ReviewLocationMaterializeService:
    """Apply persisted forensic location knowledge without remounting media."""

    def __init__(
        self,
        repository: CatalogRepository,
        catalog: WorkflowCatalog | None = None,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.repository = repository
        self.catalog = catalog
        self.progress = progress
        self._recover = ReviewLocationRecoverService(
            repository, _NullResolver(), catalog, progress=progress
        )

    def run(
        self,
        *,
        input_root: Path,
        output_root: Path,
        forensic_json: Path,
        projected_coverage_json: Path | None,
        plan_json: Path,
        plan_text: Path,
        dry_run: bool = True,
        overwrite: bool = False,
        skip_color_integrity: bool = False,
        refresh_audit: bool = False,
    ) -> ReviewLocationMaterializeReport:
        input_root = input_root.expanduser().resolve()
        output_root = output_root.expanduser().resolve()
        forensic_json = forensic_json.expanduser().resolve()
        plan_json = plan_json.expanduser().resolve()
        plan_text = plan_text.expanduser().resolve()
        projected_path = (
            projected_coverage_json.expanduser().resolve()
            if projected_coverage_json
            else None
        )
        if not input_root.is_dir():
            raise VClipError(f"Input root not found: {input_root}")
        if not forensic_json.is_file():
            raise VClipError(f"Forensic JSON not found: {forensic_json}")
        if refresh_audit:
            if not output_root.is_dir():
                raise VClipError(
                    f"--refresh-audit requires existing output root: {output_root}"
                )
            dry_run = True
        if (
            not dry_run
            and not refresh_audit
            and output_root.exists()
            and any(output_root.iterdir())
            and not overwrite
        ):
            raise VClipError(
                f"Output root is not empty: {output_root} (pass --overwrite)"
            )

        self._announce(
            f"Materializing location forensic knowledge from reports "
            f"(dry_run={dry_run}, refresh_audit={refresh_audit}): {input_root}"
        )
        forensic = json.loads(forensic_json.read_text(encoding="utf-8"))
        projected_report = None
        if projected_path and projected_path.is_file():
            projected_report = json.loads(projected_path.read_text(encoding="utf-8"))

        candidates, appearances, overlay = classify_corpus_candidates(
            input_root=input_root,
            repository=self.repository,
            forensic=forensic,
        )
        appearances_by_clip: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in appearances:
            appearances_by_clip[str(item["stock_clip_id"])].append(item)

        mutation_candidates = [
            item
            for item in candidates
            if item["projected_state"] in MUTATION_STATES
        ]
        recoveries, plan_rows = self._build_mutations(
            mutation_candidates=mutation_candidates,
            appearances_by_clip=appearances_by_clip,
            overlay=overlay,
            forensic=forensic,
        )

        # Physical before/after counts use shard event labels, not projected overlay.
        input_physical = collect_physical_candidates(
            input_root=input_root, repository=self.repository
        )
        before_known = sum(1 for item in input_physical if not item["physical_unknown"])
        before_unknown = len(input_physical) - before_known
        by_state = Counter(item["projected_state"] for item in candidates)

        shards = sorted({item.source_shard for item in recoveries})
        report = ReviewLocationMaterializeReport(
            input_root=str(input_root),
            output_root=str(output_root),
            dry_run=dry_run,
            forensic_json=str(forensic_json),
            projected_coverage_json=str(projected_path) if projected_path else None,
            plan_json=str(plan_json),
            plan_text=str(plan_text),
            total_candidate_mutations=len(recoveries),
            mutations_by_type=dict(
                Counter(row["mutation_type"] for row in plan_rows)
            ),
            shards_that_would_change=shards,
            candidates_before_known=before_known,
            candidates_before_unknown=before_unknown,
            unchanged_known_existing=by_state.get("known_existing", 0),
            unchanged_accepted_unresolved_drone=by_state.get(
                "accepted_unresolved_drone", 0
            ),
            unchanged_out_of_scope_non_drone=by_state.get(
                "out_of_scope_non_drone", 0
            ),
            plan_rows=plan_rows,
            recoveries=[asdict(item) for item in recoveries],
        )

        if refresh_audit:
            # Re-audit an already-written corpus; do not mutate XML/DB.
            self._apply_physical_output_audit(
                report,
                output_root=output_root,
                input_physical=input_physical,
                recoveries=recoveries,
                plan_rows=plan_rows,
                appearances=appearances,
                candidates=candidates,
                projected_report=projected_report,
                skip_color_integrity=skip_color_integrity,
                compare_color_to=input_root,
            )
            self._write_plan_reports(report, plan_json=plan_json, plan_text=plan_text)
            self._announce(
                f"Refresh audit complete against existing output {output_root}."
            )
            return report

        if dry_run:
            simulated = self._simulate_physical_after(
                input_physical=input_physical, recoveries=recoveries
            )
            self._populate_physical_after(report, simulated)
            color_summary = None
            if not skip_color_integrity:
                color_summary = self._color_integrity_summary(
                    corpus_root=input_root,
                    baseline_root=None,
                    role="input",
                )
            report.dry_run_checks = self._build_audit_checks(
                candidates=candidates,
                recoveries=recoveries,
                plan_rows=plan_rows,
                appearances=appearances,
                input_physical=input_physical,
                after_physical=simulated,
                color_summary=color_summary,
                projected_report=projected_report,
                post_write=False,
            )
            self._write_plan_reports(report, plan_json=plan_json, plan_text=plan_text)
            self._announce(
                f"Dry run complete: {len(recoveries)} mutation(s) across "
                f"{len(shards)} shard(s). No corpus write performed."
            )
            report.shards_changed = len(shards)
            report.shards_unchanged = 0
            return report

        shard_entries = self._recover._discover_shards(input_root)
        by_shard: dict[str, list[LocationRecoveryRow]] = defaultdict(list)
        for item in recoveries:
            by_shard[item.source_shard].append(item)
            item.output_xml = str(output_root / item.source_shard)

        output_root.mkdir(parents=True, exist_ok=True)
        changed, unchanged, failures = self._recover._write_corpus(
            shard_entries=shard_entries,
            output_root=output_root,
            recoveries_by_shard=by_shard,
            overwrite=overwrite,
        )
        report.shards_changed = changed
        report.shards_unchanged = unchanged
        report.shards_failed = len(failures)
        report.failures = failures
        if failures:
            self._write_plan_reports(report, plan_json=plan_json, plan_text=plan_text)
            return report

        self._recover._persist_candidate_updates(recoveries)
        if self.catalog is not None:
            self.catalog.record_review_location_recoveries(recoveries=recoveries)

        self._apply_physical_output_audit(
            report,
            output_root=output_root,
            input_physical=input_physical,
            recoveries=recoveries,
            plan_rows=plan_rows,
            appearances=appearances,
            candidates=candidates,
            projected_report=projected_report,
            skip_color_integrity=skip_color_integrity,
            compare_color_to=input_root,
        )
        report.dry_run = False
        self._write_plan_reports(report, plan_json=plan_json, plan_text=plan_text)
        self._announce(
            f"Wrote materialized corpus to {output_root} "
            f"({changed} shard(s) changed, {unchanged} unchanged)."
        )
        return report

    def _apply_physical_output_audit(
        self,
        report: ReviewLocationMaterializeReport,
        *,
        output_root: Path,
        input_physical: list[dict[str, Any]],
        recoveries: list[LocationRecoveryRow],
        plan_rows: list[dict[str, Any]],
        appearances: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        projected_report: dict[str, Any] | None,
        skip_color_integrity: bool,
        compare_color_to: Path,
    ) -> None:
        after_physical = collect_physical_candidates(
            input_root=output_root, repository=self.repository
        )
        self._populate_physical_after(report, after_physical)
        color_summary = None
        if not skip_color_integrity:
            color_summary = self._color_integrity_summary(
                corpus_root=output_root,
                baseline_root=compare_color_to,
                role="output",
            )
        report.dry_run_checks = self._build_audit_checks(
            candidates=candidates,
            recoveries=recoveries,
            plan_rows=plan_rows,
            appearances=appearances,
            input_physical=input_physical,
            after_physical=after_physical,
            color_summary=color_summary,
            projected_report=projected_report,
            post_write=True,
        )
        report.physical_manifest_audit = build_physical_audit(
            input_root=output_root, repository=self.repository
        )
        # Preserve legacy post_write_audit shape for consumers.
        stale_ids = {
            row["stock_clip_id"]
            for row in plan_rows
            if row["mutation_type"] == STALE_CORRECTION_REASON
        }
        known_keys = {
            (item["stockify_run_id"], item["stock_clip_id"])
            for item in input_physical
            if not item["physical_unknown"]
            and item["stock_clip_id"] not in stale_ids
        }
        report.dry_run_checks["post_write_audit"] = self._recover._post_write_audit(
            input_root=Path(report.input_root),
            output_root=output_root,
            recoveries=recoveries,
            known_keys=known_keys,
            appearances=appearances,
        )

    def _populate_physical_after(
        self,
        report: ReviewLocationMaterializeReport,
        after_physical: list[dict[str, Any]],
    ) -> None:
        report.candidates_after_known = sum(
            1 for item in after_physical if not item["physical_unknown"]
        )
        report.candidates_after_unknown = (
            len(after_physical) - report.candidates_after_known
        )
        unresolved = [
            item
            for item in after_physical
            if item["physical_class"] == "accepted_unresolved_drone"
        ]
        oos = [
            item
            for item in after_physical
            if item["physical_class"] == "out_of_scope_non_drone"
        ]
        other = [
            item
            for item in after_physical
            if item["physical_unknown"]
            and item["physical_class"]
            not in {"accepted_unresolved_drone", "out_of_scope_non_drone"}
        ]
        report.accepted_unresolved_drone_clip_ids = sorted(
            item["stock_clip_id"] for item in unresolved
        )
        report.out_of_scope_non_drone_clip_ids = sorted(
            item["stock_clip_id"] for item in oos
        )
        report.other_unknown_clip_ids = sorted(item["stock_clip_id"] for item in other)
        report.remaining_unknown_classification = {
            "total_physical_unknown": report.candidates_after_unknown,
            "accepted_unresolved_drone": len(unresolved),
            "out_of_scope_non_drone": len(oos),
            "other": len(other),
            "accepted_unresolved_drone_clip_ids": list(
                report.accepted_unresolved_drone_clip_ids
            ),
            "out_of_scope_non_drone_clip_ids": list(
                report.out_of_scope_non_drone_clip_ids
            ),
            "other_unknown_clip_ids": list(report.other_unknown_clip_ids),
            "note": (
                "Classification is over remaining physical Unknown Location "
                "candidates only (manifest event labels), not the pre-write "
                "projected overlay."
            ),
        }

    def _simulate_physical_after(
        self,
        *,
        input_physical: list[dict[str, Any]],
        recoveries: list[LocationRecoveryRow],
    ) -> list[dict[str, Any]]:
        rename = {item.stock_clip_id: item.new_event_name for item in recoveries}
        simulated: list[dict[str, Any]] = []
        for item in input_physical:
            event_name = rename.get(item["stock_clip_id"], item["event_name"])
            physical_unknown = is_unknown_event_name(event_name)
            oos = bool(item.get("out_of_scope_non_drone"))
            if physical_unknown and oos:
                physical_class = "out_of_scope_non_drone"
            elif physical_unknown:
                physical_class = "accepted_unresolved_drone"
            else:
                physical_class = "known_location"
            simulated.append(
                {
                    **item,
                    "event_name": event_name,
                    "physical_unknown": physical_unknown,
                    "physical_class": physical_class,
                }
            )
        return simulated

    def _build_mutations(
        self,
        *,
        mutation_candidates: list[dict[str, Any]],
        appearances_by_clip: dict[str, list[dict[str, Any]]],
        overlay: dict[str, Any],
        forensic: dict[str, Any],
    ) -> tuple[list[LocationRecoveryRow], list[dict[str, Any]]]:
        evidence_by_stem: dict[str, dict[str, Any]] = overlay["evidence_by_stem"]
        recovery_by_clip: dict[str, dict[str, Any]] = overlay["recovery_by_clip"]
        approved_consensus: dict[str, dict[str, Any]] = overlay["approved_consensus"]
        # Also index source_level evidence by clip for robust joins.
        evidence_by_clip: dict[str, dict[str, Any]] = {}
        for row in (forensic.get("jpg_exif_forensic") or {}).get(
            "source_level_evidence"
        ) or []:
            if row.get("evidence_kind") != "jpg_exif_same_shoot":
                continue
            for clip_id in row.get("stock_clip_ids") or []:
                evidence_by_clip[str(clip_id)] = row

        recoveries: list[LocationRecoveryRow] = []
        plan_rows: list[dict[str, Any]] = []
        for candidate in sorted(
            mutation_candidates, key=lambda item: item["stock_clip_id"]
        ):
            clip_id = candidate["stock_clip_id"]
            appearance = _select_appearance(
                appearances_by_clip.get(clip_id) or [],
                preferred_relative_xml=candidate.get("relative_xml"),
            )
            if appearance is None:
                raise VClipError(
                    f"Missing shard appearance for materialize candidate {clip_id}"
                )
            state = candidate["projected_state"]
            if state == "recoverable_group_consensus":
                recovery = self._build_consensus_recovery(
                    candidate=candidate,
                    appearance=appearance,
                    consensus=approved_consensus[clip_id],
                )
            else:
                evidence = (
                    evidence_by_clip.get(clip_id)
                    or evidence_by_stem.get(candidate.get("stem") or "")
                    or {}
                )
                prior = recovery_by_clip.get(clip_id)
                recovery = self._build_jpg_recovery(
                    candidate=candidate,
                    appearance=appearance,
                    evidence=evidence,
                    prior_recovery=prior,
                    stale=(state == STALE_CORRECTION_REASON),
                )
            recoveries.append(recovery)
            plan_rows.append(
                self._plan_row(
                    candidate=candidate,
                    appearance=appearance,
                    recovery=recovery,
                    mutation_type=state,
                )
            )
        return recoveries, plan_rows

    def _build_jpg_recovery(
        self,
        *,
        candidate: dict[str, Any],
        appearance: dict[str, Any],
        evidence: dict[str, Any],
        prior_recovery: dict[str, Any] | None,
        stale: bool,
    ) -> LocationRecoveryRow:
        if not evidence or evidence.get("evidence_kind") != "jpg_exif_same_shoot":
            raise VClipError(
                f"Missing persisted jpg_exif_same_shoot evidence for "
                f"{candidate['stock_clip_id']}"
            )
        lat = evidence.get("latitude")
        lon = evidence.get("longitude")
        if lat is None or lon is None:
            raise VClipError(
                f"Persisted JPG evidence lacks coordinates for "
                f"{candidate['stock_clip_id']}"
            )
        lat_f = float(lat)
        lon_f = float(lon)
        location = _location_from_persisted_evidence(evidence, lat_f, lon_f)
        row = appearance.get("row") or {}
        event_name = str(appearance.get("event_name") or candidate["physical_event_name"])
        capture_date = _event_capture_date(event_name, [appearance])
        if prior_recovery and prior_recovery.get("new_event_name"):
            new_event = str(prior_recovery["new_event_name"])
            new_project = str(prior_recovery["new_project_name"])
            prior_prov = prior_recovery.get("provenance") or {}
            if isinstance(prior_prov, str):
                try:
                    prior_prov = json.loads(prior_prov)
                except json.JSONDecodeError:
                    prior_prov = {}
            confidence = str(
                prior_recovery.get("resolution_confidence")
                or evidence.get("confidence")
                or "medium"
            )
            jpg_payload = prior_prov.get("jpg_exif_same_shoot") or _jpg_payload(evidence)
            if prior_prov.get("location"):
                location = dict(prior_prov["location"])
                location["direct_source_gps"] = False
                location["gps_kind"] = "inferred_jpg_exif_same_shoot"
        else:
            new_event = event_base_name(
                location, {"date": capture_date or "Unknown Date"}
            )
            old_project = str(appearance.get("project_name") or "")
            time_of_day = _time_of_day_for_project(old_project, row)
            new_base = project_base_label(location, time_of_day)
            old_label = str(
                row.get("generated_project_label") or old_project.split(" — Clip ")[0]
            )
            new_label = _relabel_project(old_label, new_base)
            if " — Clip " in old_project:
                suffix = old_project.split(" — Clip ", 1)[1]
                new_project = safe_filename(f"{new_label} — Clip {suffix}")
            else:
                new_project = safe_filename(new_label)
            confidence = str(evidence.get("confidence") or "medium")
            jpg_payload = _jpg_payload(evidence)

        provenance: dict[str, Any] = {
            "original_event": event_name,
            "new_event": new_event,
            "source_media": evidence.get("source_basename")
            or candidate.get("source_basename"),
            "srt_paths": [],
            "representative_gps": {
                "lat": lat_f,
                "lon": lon_f,
                "kind": "inferred_jpg_exif_same_shoot",
            },
            "resolution_confidence": confidence,
            "review_required": bool(evidence.get("review_required")),
            "recovery_reason": JPG_EXIF_REASON,
            "resolution_status": STATUS_FORENSIC_JPG,
            "evidence_sources": [
                "jpg_exif_same_shoot",
                "review_location_materialize",
            ],
            "jpg_exif_same_shoot": jpg_payload,
            "location": location,
            "capture_date": capture_date,
            "materialized_from": "jpg-exif-forensic.json",
            "direct_source_gps": False,
            "gps_kind": "inferred_jpg_exif_same_shoot",
            "forensic_only": False,
            "mutates_corpus": True,
        }
        if stale:
            provenance["correction"] = {
                "kind": STALE_CORRECTION_REASON,
                "prior_event_name": event_name,
                "prior_project_name": appearance.get("project_name"),
                "prior_location_label": candidate.get("physical_label"),
                "contradiction": candidate.get("contradiction"),
                "corrected_event_name": new_event,
                "corrected_project_name": new_project,
                "corrected_location_label": location.get("public_label"),
                "note": (
                    "Prior named location contradicted by persisted source-level "
                    "JPG EXIF GPS. Correction preserves the prior label in this "
                    "audit trail rather than overwriting history silently."
                ),
            }
            provenance["recovery_reason"] = STALE_CORRECTION_REASON
            provenance["evidence_sources"] = [
                "jpg_exif_same_shoot",
                STALE_CORRECTION_REASON,
                "review_location_materialize",
            ]

        return LocationRecoveryRow(
            stockify_run_id=str(appearance["stockify_run_id"]),
            stock_clip_id=str(candidate["stock_clip_id"]),
            original_event_name=event_name,
            new_event_name=new_event,
            original_project_name=str(appearance.get("project_name") or ""),
            new_project_name=new_project,
            source_media=str(
                evidence.get("source_basename") or candidate.get("source_basename") or ""
            )
            or None,
            srt_paths=[],
            representative_lat=lat_f,
            representative_lon=lon_f,
            resolution_confidence=confidence,
            recovery_reason=(
                STALE_CORRECTION_REASON if stale else JPG_EXIF_REASON
            ),
            source_shard=str(appearance.get("relative_xml") or ""),
            input_xml=str(appearance.get("xml_path") or ""),
            output_xml=None,
            provenance=provenance,
        )

    def _build_consensus_recovery(
        self,
        *,
        candidate: dict[str, Any],
        appearance: dict[str, Any],
        consensus: dict[str, Any],
    ) -> LocationRecoveryRow:
        row = appearance.get("row") or {}
        event_name = str(appearance.get("event_name") or candidate["physical_event_name"])
        capture_date = _event_capture_date(event_name, [appearance])
        label = str(consensus.get("label") or "")
        location = {
            "status": "resolved",
            "confidence": str(consensus.get("confidence") or "high"),
            "evidence_sources": [EDITORIAL_CONSENSUS_REASON],
            "center_lat": None,
            "center_lon": None,
            "sample_count": 0,
            "valid_sample_count": 0,
            "country": None,
            "state": "Washington" if "Washington" in label else None,
            "region": "Washington" if "Washington" in label else None,
            "city": label.split(",")[0].strip() if "," in label else label,
            "locality": None,
            "neighborhood": None,
            "poi": None,
            "public_label": label,
            "timezone": None,
            "place_provider": EDITORIAL_CONSENSUS_REASON,
            "direct_source_gps": False,
            "gps_kind": None,
            "coordinates_inherited": False,
            "recovery": {
                "method": EDITORIAL_CONSENSUS_REASON,
                "confidence": str(consensus.get("confidence") or "high"),
                "recovered_at": utc_now(),
            },
        }
        # Broader place labels like "Seattle, Washington" should keep city/state.
        if "," in label:
            left, right = [part.strip() for part in label.split(",", 1)]
            location["city"] = left
            location["state"] = right
            location["region"] = right
        location["country"] = country_for_admin_area(
            location.get("state"),
            explicit_country=str(consensus.get("country") or "") or None,
        )
        new_event = event_base_name(location, {"date": capture_date or "Unknown Date"})
        old_project = str(appearance.get("project_name") or "")
        time_of_day = _time_of_day_for_project(old_project, row)
        new_base = project_base_label(location, time_of_day)
        old_label = str(
            row.get("generated_project_label") or old_project.split(" — Clip ")[0]
        )
        new_label = _relabel_project(old_label, new_base)
        if " — Clip " in old_project:
            suffix = old_project.split(" — Clip ", 1)[1]
            new_project = safe_filename(f"{new_label} — Clip {suffix}")
        else:
            new_project = safe_filename(new_label)

        provenance = {
            "original_event": event_name,
            "new_event": new_event,
            "source_media": candidate.get("source_basename"),
            "srt_paths": [],
            "representative_gps": None,
            "resolution_confidence": consensus.get("confidence") or "high",
            "review_required": False,
            "recovery_reason": EDITORIAL_CONSENSUS_REASON,
            "resolution_status": EDITORIAL_CONSENSUS_REASON,
            "evidence_sources": [EDITORIAL_CONSENSUS_REASON, "review_location_materialize"],
            "location": location,
            "capture_date": capture_date,
            "materialized_from": "jpg-exif-forensic.json",
            "editorial_group": consensus.get("event_name"),
            "recommended_label_level": consensus.get("level"),
            "coordinates_inherited": False,
            "note": (
                "Group-level labels are editorial context only. Inherited clips "
                "receive no fabricated precise GPS."
            ),
            "forensic_only": False,
            "mutates_corpus": True,
        }
        return LocationRecoveryRow(
            stockify_run_id=str(appearance["stockify_run_id"]),
            stock_clip_id=str(candidate["stock_clip_id"]),
            original_event_name=event_name,
            new_event_name=new_event,
            original_project_name=old_project,
            new_project_name=new_project,
            source_media=candidate.get("source_basename"),
            srt_paths=[],
            representative_lat=None,
            representative_lon=None,
            resolution_confidence=str(consensus.get("confidence") or "high"),
            recovery_reason=EDITORIAL_CONSENSUS_REASON,
            source_shard=str(appearance.get("relative_xml") or ""),
            input_xml=str(appearance.get("xml_path") or ""),
            output_xml=None,
            provenance=provenance,
        )

    def _plan_row(
        self,
        *,
        candidate: dict[str, Any],
        appearance: dict[str, Any],
        recovery: LocationRecoveryRow,
        mutation_type: str,
    ) -> dict[str, Any]:
        coords = None
        if recovery.representative_lat is not None and recovery.representative_lon is not None:
            coords = {
                "lat": recovery.representative_lat,
                "lon": recovery.representative_lon,
                "kind": "inferred_jpg_exif_same_shoot",
            }
        provenance_label = recovery.recovery_reason
        if mutation_type == "recoverable_jpg_exif":
            provenance_label = JPG_EXIF_REASON
        elif mutation_type == "recoverable_group_consensus":
            provenance_label = EDITORIAL_CONSENSUS_REASON
        return {
            "stock_clip_id": recovery.stock_clip_id,
            "stockify_run_id": recovery.stockify_run_id,
            "mutation_type": mutation_type,
            "current_shard": appearance.get("relative_xml"),
            "current_event": recovery.original_event_name,
            "current_project": recovery.original_project_name,
            "projected_shard": appearance.get("relative_xml"),
            "projected_event": recovery.new_event_name,
            "projected_project": recovery.new_project_name,
            "source_basename": recovery.source_media or candidate.get("source_basename"),
            "current_location": candidate.get("physical_label")
            or (
                recovery.original_event_name.split(" — ")[0]
                if recovery.original_event_name
                else None
            ),
            "projected_location": (
                (recovery.provenance or {}).get("location", {}) or {}
            ).get("public_label")
            or recovery.new_event_name.split(" — ")[0],
            "evidence_kind": (
                JPG_EXIF_REASON
                if mutation_type
                in {"recoverable_jpg_exif", STALE_CORRECTION_REASON}
                else EDITORIAL_CONSENSUS_REASON
            ),
            "confidence": recovery.resolution_confidence,
            "inferred_coordinates": coords,
            "provenance": provenance_label,
            "contradiction": candidate.get("contradiction"),
            "correction_audit": (recovery.provenance or {}).get("correction"),
        }

    def _build_audit_checks(
        self,
        *,
        candidates: list[dict[str, Any]],
        recoveries: list[LocationRecoveryRow],
        plan_rows: list[dict[str, Any]],
        appearances: list[dict[str, Any]],
        input_physical: list[dict[str, Any]],
        after_physical: list[dict[str, Any]],
        color_summary: dict[str, Any] | None,
        projected_report: dict[str, Any] | None,
        post_write: bool,
    ) -> dict[str, Any]:
        by_type = Counter(row["mutation_type"] for row in plan_rows)
        recovery_ids = [item.stock_clip_id for item in recoveries]
        id_counts = Counter(recovery_ids)
        mutation_duplicates = sorted(
            clip for clip, count in id_counts.items() if count != 1
        )
        after_ids = [item["stock_clip_id"] for item in after_physical]
        after_counts = Counter(after_ids)
        after_duplicates = sorted(
            clip for clip, count in after_counts.items() if count != 1
        )
        universe_ids = {item["stock_clip_id"] for item in input_physical}
        missing = sorted(set(recovery_ids) - universe_ids)

        input_by_clip = {item["stock_clip_id"]: item for item in input_physical}
        after_by_clip = {item["stock_clip_id"]: item for item in after_physical}
        unknown_to_known = sorted(
            clip_id
            for clip_id, before in input_by_clip.items()
            if before["physical_unknown"]
            and clip_id in after_by_clip
            and not after_by_clip[clip_id]["physical_unknown"]
        )
        known_corrections = sorted(
            row["stock_clip_id"]
            for row in plan_rows
            if row["mutation_type"] == STALE_CORRECTION_REASON
        )
        # Unintended: physically-known non-stale candidates that were mutated.
        stale_ids = set(known_corrections)
        unintended_known = sorted(
            row["stock_clip_id"]
            for row in plan_rows
            if row["stock_clip_id"] not in stale_ids
            and not is_unknown_event_name(row.get("current_event"))
        )

        troutville_after = sorted(
            item["stock_clip_id"]
            for item in after_physical
            if "troutville" in str(item.get("event_name") or "").casefold()
        )
        projected_troutville = [
            row["stock_clip_id"]
            for row in plan_rows
            if row["mutation_type"] == STALE_CORRECTION_REASON
            and "troutville"
            in str(
                row.get("projected_location") or row.get("projected_event") or ""
            ).casefold()
        ]

        expected_ok = all(
            by_type.get(key, 0) == value for key, value in EXPECTED_MUTATION_COUNTS.items()
        ) and sum(by_type.values()) == sum(EXPECTED_MUTATION_COUNTS.values())

        after_known = sum(1 for item in after_physical if not item["physical_unknown"])
        after_unknown = len(after_physical) - after_known
        unresolved_drone = [
            item
            for item in after_physical
            if item["physical_class"] == "accepted_unresolved_drone"
        ]
        oos = [
            item
            for item in after_physical
            if item["physical_class"] == "out_of_scope_non_drone"
        ]
        other_unknown = [
            item
            for item in after_physical
            if item["physical_unknown"]
            and item["physical_class"]
            not in {"accepted_unresolved_drone", "out_of_scope_non_drone"}
        ]

        physical_assertions = {
            "total_candidates": len(after_physical),
            "known": after_known,
            "unknown": after_unknown,
            "duplicates": len(after_duplicates),
            "unknown_to_known_mutations": len(unknown_to_known),
            "known_correction_mutations": len(known_corrections),
            "total_mutations": len(recoveries),
            "stale_troutville_candidates": len(troutville_after),
            "expected": dict(PHYSICAL_POST_WRITE_EXPECTED),
        }
        physical_assertions["matches_expected"] = (
            physical_assertions["total_candidates"]
            == PHYSICAL_POST_WRITE_EXPECTED["total_candidates"]
            and physical_assertions["known"] == PHYSICAL_POST_WRITE_EXPECTED["known"]
            and physical_assertions["unknown"] == PHYSICAL_POST_WRITE_EXPECTED["unknown"]
            and physical_assertions["duplicates"]
            == PHYSICAL_POST_WRITE_EXPECTED["duplicates"]
            and physical_assertions["unknown_to_known_mutations"]
            == PHYSICAL_POST_WRITE_EXPECTED["unknown_to_known_mutations"]
            and physical_assertions["known_correction_mutations"]
            == PHYSICAL_POST_WRITE_EXPECTED["known_correction_mutations"]
            and physical_assertions["total_mutations"]
            == PHYSICAL_POST_WRITE_EXPECTED["total_mutations"]
            and physical_assertions["stale_troutville_candidates"] == 0
        )

        color_green = True
        if color_summary is not None:
            color_green = bool(color_summary.get("remains_green"))

        # Explain divergence from the earlier projected 13 + 46 overlay counts.
        projected_unresolved = None
        projected_oos = None
        if projected_report is not None:
            projected_unresolved = (
                projected_report.get("entire_corpus", {})
                .get("by_projected_state", {})
                .get("accepted_unresolved_drone")
            )
            projected_oos = (
                projected_report.get("entire_corpus", {})
                .get("by_projected_state", {})
                .get("out_of_scope_non_drone")
            )
        discrepancy = {
            "projected_accepted_unresolved_drone": projected_unresolved,
            "physical_accepted_unresolved_drone": len(unresolved_drone),
            "projected_out_of_scope_non_drone": projected_oos,
            "physical_out_of_scope_non_drone_unknown": len(oos),
            "explanation": (
                "Projected accepted_unresolved_drone included physically-known "
                "named labels inside --unknown-- shards that lacked confirming "
                "source GPS (unconfirmed named labels). Physical unknown count "
                "only includes manifest events still named Unknown Location. "
                "Projected OOS counted all OOS clips corpus-wide; physical OOS "
                "here counts only remaining Unknown Location OOS clips."
            ),
        }

        checks = {
            "mode": (
                "location_materialize_post_write_audit"
                if post_write
                else "location_materialize_dry_run_audit"
            ),
            "total_candidate_mutations": len(recoveries),
            "mutations_by_type": dict(by_type),
            "expected_mutation_counts": dict(EXPECTED_MUTATION_COUNTS),
            "expected_mutation_counts_match": expected_ok,
            "shards_that_would_change": sorted({item.source_shard for item in recoveries}),
            "candidates_exactly_once": len(after_duplicates) == 0,
            "zero_missing_candidates": len(missing) == 0 and len(universe_ids) > 0,
            "zero_duplicate_candidates": len(after_duplicates) == 0,
            "zero_duplicate_mutations": len(mutation_duplicates) == 0,
            "zero_unintended_changes_to_known_existing": len(unintended_known) == 0,
            "unintended_known_existing_changes": unintended_known,
            "zero_stale_troutville_contradictions_remaining_after_projected_write": (
                len(troutville_after) == 0
                and len(projected_troutville) == 0
                and by_type.get(STALE_CORRECTION_REASON, 0)
                == EXPECTED_MUTATION_COUNTS[STALE_CORRECTION_REASON]
            ),
            "stale_troutville_remaining": troutville_after,
            "color_integrity_audit_remains_green": color_green,
            "color_integrity": color_summary,
            "physical_manifest_assertions": physical_assertions,
            "unknown_to_known_clip_ids": unknown_to_known,
            "known_correction_clip_ids": known_corrections,
            "accepted_unresolved_drone_count": len(unresolved_drone),
            "out_of_scope_non_drone_count": len(oos),
            "other_unknown_count": len(other_unknown),
            "remaining_unknown_vs_projected_discrepancy": discrepancy,
            "missing_mutation_clip_ids": missing,
            "duplicate_mutation_clip_ids": mutation_duplicates,
            "duplicate_universe_clip_ids": after_duplicates[:50],
            "appearance_count": len(appearances),
            "universe_unique_stock_clip_ids": len(universe_ids),
            "candidates_overlay_count": len(candidates),
        }
        if projected_report is not None:
            checks["projected_coverage_crosscheck"] = {
                "recoverable_jpg_exif": (
                    projected_report.get("entire_corpus", {})
                    .get("by_projected_state", {})
                    .get("recoverable_jpg_exif")
                ),
                "stale_location_requires_correction": (
                    projected_report.get("entire_corpus", {})
                    .get("by_projected_state", {})
                    .get("stale_location_requires_correction")
                ),
                "recoverable_group_consensus": (
                    projected_report.get("entire_corpus", {})
                    .get("by_projected_state", {})
                    .get("recoverable_group_consensus")
                ),
            }
        checks["all_passed"] = all(
            [
                checks["expected_mutation_counts_match"],
                checks["candidates_exactly_once"],
                checks["zero_missing_candidates"],
                checks["zero_duplicate_candidates"],
                checks["zero_unintended_changes_to_known_existing"],
                checks[
                    "zero_stale_troutville_contradictions_remaining_after_projected_write"
                ],
                checks["color_integrity_audit_remains_green"],
                physical_assertions["matches_expected"],
                checks["other_unknown_count"] == 0,
            ]
        )
        return checks

    def _color_integrity_summary(
        self,
        *,
        corpus_root: Path,
        baseline_root: Path | None,
        role: str,
    ) -> dict[str, Any]:
        self._announce(f"Auditing color integrity on {role} corpus (read-only)")
        with tempfile.TemporaryDirectory(prefix="vclip-color-mat-") as tmp:
            tmp_path = Path(tmp)
            corpus_report = ReviewColorIntegrityService(
                self.repository, progress=self.progress
            ).run(
                input_root=corpus_root,
                report_path=tmp_path / "color.json",
                text_report_path=tmp_path / "color.txt",
                media_roots=[],
            )
            corpus_summary = self._color_report_snapshot(corpus_report)
            baseline_summary = None
            lut_record_diffs = 0
            if baseline_root is not None:
                baseline_report = ReviewColorIntegrityService(
                    self.repository, progress=self.progress
                ).run(
                    input_root=baseline_root,
                    report_path=tmp_path / "color-baseline.json",
                    text_report_path=tmp_path / "color-baseline.txt",
                    media_roots=[],
                )
                baseline_summary = self._color_report_snapshot(baseline_report)
                by_base = {
                    rec.stock_clip_id: (
                        rec.xml_has_lut,
                        rec.db_has_lut,
                        rec.status,
                    )
                    for rec in baseline_report.records
                }
                for rec in corpus_report.records:
                    prior = by_base.get(rec.stock_clip_id)
                    current = (rec.xml_has_lut, rec.db_has_lut, rec.status)
                    if prior is not None and prior != current:
                        lut_record_diffs += 1

            matches_accepted_baseline = (
                corpus_summary["unresolved_identities"]
                == COLOR_BASELINE_AGGREGATES["unresolved_identities"]
                and corpus_summary["db_lut_xml_lut"]
                == COLOR_BASELINE_AGGREGATES["db_lut_xml_lut"]
                and corpus_summary["db_lut_xml_no_lut"]
                == COLOR_BASELINE_AGGREGATES["db_lut_xml_no_lut"]
                and corpus_summary["db_no_lut_xml_lut"]
                == COLOR_BASELINE_AGGREGATES["db_no_lut_xml_lut"]
                and corpus_summary["post_may_2025_db_lut_xml_missing"]
                == COLOR_BASELINE_AGGREGATES["post_may_2025_db_lut_xml_missing"]
            )
            matches_input = (
                baseline_summary is not None
                and lut_record_diffs == 0
                and corpus_summary["unresolved_identities"]
                == baseline_summary["unresolved_identities"]
                and corpus_summary["db_lut_xml_lut"] == baseline_summary["db_lut_xml_lut"]
                and corpus_summary["db_lut_xml_no_lut"]
                == baseline_summary["db_lut_xml_no_lut"]
            )
            # Green iff identities resolve and color state matches the accepted
            # baseline (and, when comparing output→input, has zero LUT diffs).
            remains_green = matches_accepted_baseline and (
                baseline_root is None or matches_input
            )
            return {
                "role": role,
                "corpus_root": str(corpus_root),
                "baseline_root": str(baseline_root) if baseline_root else None,
                **corpus_summary,
                "accepted_baseline": dict(COLOR_BASELINE_AGGREGATES),
                "matches_accepted_baseline": matches_accepted_baseline,
                "matches_input_color_state": matches_input if baseline_root else None,
                "lut_record_diffs_vs_baseline_root": lut_record_diffs,
                "baseline_root_summary": baseline_summary,
                "remains_green": remains_green,
                "note": (
                    "Location materialization must not alter LUT/effect/color state. "
                    "Green means the audited corpus matches the accepted "
                    "post-color-repair aggregates"
                    + (
                        " and has zero per-candidate LUT diffs vs the input root."
                        if baseline_root is not None
                        else "."
                    )
                ),
            }

    def _color_report_snapshot(self, report: Any) -> dict[str, Any]:
        return {
            "manifest_identities": report.manifest_identities,
            "resolved_projects": report.resolved_projects,
            "unresolved_identities": report.unresolved_identities,
            "db_lut_xml_lut": report.db_lut_xml_lut,
            "db_lut_xml_no_lut": report.db_lut_xml_no_lut,
            "db_no_lut_xml_lut": report.db_no_lut_xml_lut,
            "db_no_lut_xml_no_lut": report.db_no_lut_xml_no_lut,
            "post_may_2025_db_lut_xml_missing": len(
                report.post_may_2025_db_lut_xml_missing or []
            ),
            "dlog_classification_counts": (report.dlog_audit or {}).get(
                "classification_counts"
            )
            or {},
        }

    def _write_plan_reports(
        self,
        report: ReviewLocationMaterializeReport,
        *,
        plan_json: Path,
        plan_text: Path,
    ) -> None:
        plan_json.parent.mkdir(parents=True, exist_ok=True)
        plan_text.parent.mkdir(parents=True, exist_ok=True)
        payload = report.as_dict()
        plan_json.write_text(json_dumps(payload), encoding="utf-8")
        plan_text.write_text(format_materialize_plan_text(report), encoding="utf-8")

    def _announce(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)


def _select_appearance(
    appearances: list[dict[str, Any]],
    *,
    preferred_relative_xml: str | None,
) -> dict[str, Any] | None:
    if not appearances:
        return None
    if preferred_relative_xml:
        for item in appearances:
            if item.get("relative_xml") == preferred_relative_xml:
                return item
    return appearances[0]


def _time_of_day_for_project(project_name: str, row: dict[str, Any]) -> dict[str, Any]:
    """Prefer the time token already present in the project label over DB TOD."""
    head = str(project_name or "")
    if " — Clip " in head:
        head = head.split(" — Clip ", 1)[0]
    # Longest display label first so "Blue Hour" wins over "Hour".
    display_to_key = sorted(
        (
            (str(display), str(key))
            for key, display in TIME_LABELS.items()
            if key and key != "unknown" and display and display != "Footage"
        ),
        key=lambda item: -len(item[0]),
    )
    head_cf = head.casefold()
    for display, key in display_to_key:
        token = f" {display}".casefold()
        if head_cf.endswith(token) or token in head_cf:
            return {"label": key}
    tod = row.get("time_of_day") or {"label": "unknown"}
    if isinstance(tod, str):
        return {"label": tod}
    if isinstance(tod, dict):
        return tod
    return {"label": "unknown"}


def _location_from_persisted_evidence(
    evidence: dict[str, Any], lat: float, lon: float
) -> dict[str, Any]:
    place = {
        "neighborhood": evidence.get("neighborhood"),
        "city": evidence.get("city"),
        "state": evidence.get("state"),
        "region": evidence.get("state"),
        "country": evidence.get("country"),
        "public_label": evidence.get("public_label"),
        "poi": None,
        "timezone": None,
        "provider": "persisted_jpg_exif_forensic",
    }
    sample_count = 1
    jpg = (evidence.get("provenance") or {}).get("jpg_exif_same_shoot") or {}
    if jpg.get("sample_count"):
        sample_count = int(jpg["sample_count"])
    return _structured_location(
        place,
        center_lat=lat,
        center_lon=lon,
        sample_count=sample_count,
        evidence_sources=["jpg_exif_same_shoot", "review_location_materialize"],
        recovery_method=JPG_EXIF_REASON,
        place_provider=place.get("provider"),
        confidence=str(evidence.get("confidence") or "medium"),
        review_required=bool(evidence.get("review_required")),
        gps_kind="inferred_jpg_exif_same_shoot",
    )


def _jpg_payload(evidence: dict[str, Any]) -> dict[str, Any]:
    nested = (evidence.get("provenance") or {}).get("jpg_exif_same_shoot")
    if isinstance(nested, dict) and nested:
        payload = dict(nested)
    else:
        payload = {
            "source_basename": evidence.get("source_basename"),
            "source_stem": evidence.get("stem"),
            "latitude": evidence.get("latitude"),
            "longitude": evidence.get("longitude"),
            "confidence": evidence.get("confidence"),
            "review_required": evidence.get("review_required"),
            "evidence_source": "jpg_exif_same_shoot",
            "evidence_files": list(evidence.get("evidence_files") or []),
        }
    payload.setdefault(
        "note",
        "Coordinates are inferred from same-shoot JPG EXIF GPS; not direct source GPS.",
    )
    payload["direct_source_gps"] = False
    return payload


def format_materialize_plan_text(report: ReviewLocationMaterializeReport) -> str:
    checks = report.dry_run_checks or {}
    lines = [
        "LOCATION MATERIALIZATION PLAN",
        "=" * 72,
        f"Mode:                 {'dry-run' if report.dry_run else 'write'}",
        f"Input root:           {report.input_root}",
        f"Output root:          {report.output_root}",
        f"Forensic JSON:        {report.forensic_json}",
        f"Projected coverage:   {report.projected_coverage_json}",
        "",
        "Mutation summary",
        "----------------",
        f"Total candidate mutations: {report.total_candidate_mutations}",
    ]
    for key in (
        "recoverable_jpg_exif",
        "stale_location_requires_correction",
        "recoverable_group_consensus",
    ):
        lines.append(f"  {key}: {report.mutations_by_type.get(key, 0)}")
    lines.extend(
        [
            "",
            "Known / unknown status",
            "----------------------",
            f"Before known (physical):   {report.candidates_before_known}",
            f"Before unknown (physical): {report.candidates_before_unknown}",
            f"After known (physical):    {report.candidates_after_known}",
            f"After unknown (physical):  {report.candidates_after_unknown}",
            "",
            "Remaining physical Unknown classification",
            "-----------------------------------------",
            (
                f"accepted_unresolved_drone: "
                f"{(report.remaining_unknown_classification or {}).get('accepted_unresolved_drone', len(report.accepted_unresolved_drone_clip_ids))}"
            ),
            (
                f"out_of_scope_non_drone:    "
                f"{(report.remaining_unknown_classification or {}).get('out_of_scope_non_drone', len(report.out_of_scope_non_drone_clip_ids))}"
            ),
            (
                f"other:                    "
                f"{(report.remaining_unknown_classification or {}).get('other', len(report.other_unknown_clip_ids))}"
            ),
            "",
            f"Shards that would change ({len(report.shards_that_would_change)})",
            "-----------------------------",
        ]
    )
    for shard in report.shards_that_would_change:
        lines.append(f"  - {shard}")
    lines.extend(
        [
            "",
            f"Accepted unresolved drone IDs ({len(report.accepted_unresolved_drone_clip_ids)})",
            "--------------------------------",
        ]
    )
    for clip_id in report.accepted_unresolved_drone_clip_ids:
        lines.append(f"  - {clip_id}")
    lines.extend(
        [
            "",
            f"Out-of-scope non-drone IDs ({len(report.out_of_scope_non_drone_clip_ids)})",
            "------------------------------",
        ]
    )
    for clip_id in report.out_of_scope_non_drone_clip_ids:
        lines.append(f"  - {clip_id}")
    phys = checks.get("physical_manifest_assertions") or {}
    disc = checks.get("remaining_unknown_vs_projected_discrepancy") or {}
    lines.extend(
        [
            "",
            "Physical manifest assertions",
            "----------------------------",
            f"total candidates:           {phys.get('total_candidates')}",
            f"known:                      {phys.get('known')}",
            f"unknown:                    {phys.get('unknown')}",
            f"duplicates:                 {phys.get('duplicates')}",
            f"unknown→known mutations:    {phys.get('unknown_to_known_mutations')}",
            f"known correction mutations: {phys.get('known_correction_mutations')}",
            f"total mutations:            {phys.get('total_mutations')}",
            f"stale Troutville remaining: {phys.get('stale_troutville_candidates')}",
            f"matches_expected:           {phys.get('matches_expected')}",
            "",
            "Remaining unknown vs prior projected overlay",
            "-------------------------------------------",
            f"projected unresolved drone: {disc.get('projected_accepted_unresolved_drone')}",
            f"physical unresolved drone:  {disc.get('physical_accepted_unresolved_drone')}",
            f"projected OOS (corpus):     {disc.get('projected_out_of_scope_non_drone')}",
            f"physical OOS unknown:       {disc.get('physical_out_of_scope_non_drone_unknown')}",
            f"note: {disc.get('explanation')}",
            "",
            "Audit checks",
            "------------",
            f"candidates_exactly_once:              {checks.get('candidates_exactly_once')}",
            f"zero_missing_candidates:              {checks.get('zero_missing_candidates')}",
            f"zero_duplicate_candidates:            {checks.get('zero_duplicate_candidates')}",
            f"zero_unintended_known_existing:       {checks.get('zero_unintended_changes_to_known_existing')}",
            f"zero_stale_troutville_after_write:    {checks.get('zero_stale_troutville_contradictions_remaining_after_projected_write')}",
            f"color_integrity_remains_green:        {checks.get('color_integrity_audit_remains_green')}",
            f"expected_mutation_counts_match:       {checks.get('expected_mutation_counts_match')}",
            f"all_passed:                           {checks.get('all_passed')}",
            "",
            f"Changed candidates ({len(report.plan_rows)})",
            "-------------------",
        ]
    )
    for index, row in enumerate(report.plan_rows, start=1):
        coords = row.get("inferred_coordinates")
        coord_text = (
            f"{coords['lat']}, {coords['lon']}" if coords else "null (no source GPS)"
        )
        lines.extend(
            [
                (
                    f"{index}. {row['stock_clip_id']} | {row['mutation_type']} | "
                    f"{row['source_basename']}"
                ),
                (
                    f"   shard: {row['current_shard']} "
                    f"(projected same path: {row['projected_shard']})"
                ),
                (
                    f"   event: {row['current_event']} → {row['projected_event']}"
                ),
                (
                    f"   project: {row['current_project']} → {row['projected_project']}"
                ),
                (
                    f"   location: {row['current_location']} → {row['projected_location']}"
                ),
                (
                    f"   evidence={row['evidence_kind']} confidence={row['confidence']} "
                    f"provenance={row['provenance']} coords={coord_text}"
                ),
            ]
        )
        if row.get("correction_audit"):
            correction = row["correction_audit"]
            lines.append(
                f"   correction: {correction.get('prior_location_label')} → "
                f"{correction.get('corrected_location_label')} "
                f"({correction.get('kind')})"
            )
        lines.append("")
    return "\n".join(lines)


class _NullResolver:
    def resolve(self, latitude: float, longitude: float):
        return None
