"""Reconcile human Final Cut edits back into Stockify's durable catalog."""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

from ..db.repository import CatalogRepository
from ..errors import VClipError
from ..stockify.core import format_time, parse_time
from ..stockify.fcpxml import (
    first_direct_child,
    format_project_timecode,
    iter_source_events,
    local_name,
    parse_source,
    read_vclip_metadata,
    video_treatment_signature,
)
from ..util import sha256_file


@dataclass(frozen=True)
class ReviewedOccurrence:
    stockify_run_id: str
    stock_clip_id: str
    source_project_id: str | None
    representation: str
    event_name: str
    project_name: str
    source_start: str
    duration: str
    timeline_offset: str
    effect_signature: str

    def state(self) -> tuple[Fraction, Fraction, str]:
        return (
            parse_time(self.source_start),
            parse_time(self.duration),
            self.effect_signature,
        )

    def as_db(self) -> dict[str, Any]:
        return {
            "stockify_run_id": self.stockify_run_id,
            "stock_clip_id": self.stock_clip_id,
            "representation": self.representation,
            "event_name": self.event_name,
            "project_name": self.project_name,
            "source_start": self.source_start,
            "duration": self.duration,
            "timeline_offset": self.timeline_offset,
            "effect_signature": self.effect_signature,
        }


@dataclass(frozen=True)
class UnidentifiedOccurrence:
    """A reviewed clip whose custom VClip metadata did not survive round-trip."""

    representation: str
    event_name: str
    project_name: str
    source_start: str
    duration: str
    timeline_offset: str
    effect_signature: str


@dataclass
class ReconcileReport:
    reconcile_run_id: str
    stockify_run_id: str
    reviewed_xml: str
    authority: str
    scope: str
    candidates_considered: int = 0
    approved: int = 0
    rejected: int = 0
    modified: int = 0
    conflicts: int = 0
    out_of_scope: int = 0
    unknown_stock_clip_ids: list[str] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class ReconcileService:
    """Compare reviewed FCPXML with Stockify's original candidate proposal."""

    def __init__(
        self,
        repository: CatalogRepository,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.repository = repository
        self.progress = progress

    def run(
        self,
        *,
        reviewed_xml: Path,
        run_id: str | None,
        authority: str,
        scope: str,
        report_path: Path | None,
        allow_conflicts: bool,
    ) -> ReconcileReport:
        if authority not in {"auto", "compilation", "individual"}:
            raise VClipError(f"Unsupported review authority: {authority}")
        if scope not in {"observed-projects", "full-run"}:
            raise VClipError(f"Unsupported reconcile scope: {scope}")

        self._announce(f"Reading reviewed Final Cut XML: {reviewed_xml.name}")
        (
            occurrences,
            unidentified_occurrences,
            observed_projects,
            embedded_run_ids,
        ) = self._read_reviewed_xml(reviewed_xml)
        resolved_run_id = self._resolve_run_id(run_id, embedded_run_ids)
        self.repository.get_stockify_run(resolved_run_id)

        reconcile_id = f"RECONCILE_{uuid.uuid4().hex.upper()}"
        self.repository.start_reconcile_run(
            reconcile_id=reconcile_id,
            stockify_run_id=resolved_run_id,
            reviewed_xml_path=str(reviewed_xml),
            reviewed_xml_sha256=sha256_file(reviewed_xml),
            authority=authority,
            scope=scope,
            report_path=str(report_path) if report_path else None,
        )

        report = ReconcileReport(
            reconcile_run_id=reconcile_id,
            stockify_run_id=resolved_run_id,
            reviewed_xml=str(reviewed_xml),
            authority=authority,
            scope=scope,
        )
        try:
            candidates = self.repository.candidates_for_run(
                resolved_run_id,
                accepted_only=True,
            )
            generated_individual_ids = {
                str(row["stock_clip_id"])
                for row in self.repository.generated_occurrences(resolved_run_id)
                if row.get("representation") == "individual"
            }
            baseline_effect_signatures = self._baseline_effect_signatures(
                resolved_run_id,
                reviewed_xml,
            )
            recovered = self._recover_individual_occurrences(
                candidates=candidates,
                occurrences=occurrences,
                unidentified=unidentified_occurrences,
                run_id=resolved_run_id,
            )
            if recovered:
                report.warnings.append(
                    f"Recovered {recovered} candidate ID(s) from exact individual "
                    "project names because custom clip metadata was missing."
                )
            known_ids = {str(candidate["stock_clip_id"]) for candidate in candidates}
            reviewed_ids = {occurrence.stock_clip_id for occurrence in occurrences}
            report.unknown_stock_clip_ids = sorted(reviewed_ids - known_ids)
            if report.unknown_stock_clip_ids:
                report.warnings.append(
                    "The reviewed XML contains VClip IDs not present in this Stockify run."
                )

            occurrences_by_id: dict[str, list[ReviewedOccurrence]] = defaultdict(list)
            for occurrence in occurrences:
                if occurrence.stockify_run_id == resolved_run_id:
                    occurrences_by_id[occurrence.stock_clip_id].append(occurrence)

            candidates_by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for candidate in candidates:
                candidates_by_project[str(candidate["source_project_id"])].append(candidate)
            dedupe_removed_ids = self._review_dedupe_removed_ids(resolved_run_id)

            decisions: list[dict[str, Any]] = []
            for source_project_id, project_candidates in candidates_by_project.items():
                project_in_scope, individual_collection_observed = self._project_scope(
                    project_candidates,
                    observed_projects,
                    scope,
                )
                if not project_in_scope:
                    report.out_of_scope += len(project_candidates)
                    continue
                compilation_observed = any(
                    str(candidate.get("generated_compilation_name") or "")
                    in observed_projects
                    for candidate in project_candidates
                )
                for candidate in project_candidates:
                    clip_id = str(candidate["stock_clip_id"])
                    # Exact-duplicate projects removed by review-dedupe stay
                    # out-of-scope / not observed — never auto-rejected.
                    if clip_id in dedupe_removed_ids and not occurrences_by_id.get(
                        clip_id
                    ):
                        report.out_of_scope += 1
                        continue
                    candidate_occurrences = occurrences_by_id.get(clip_id, [])
                    individual_name = str(
                        candidate.get("generated_clip_project_name") or ""
                    )
                    if (
                        scope == "observed-projects"
                        and not compilation_observed
                        and individual_name not in observed_projects
                        and not candidate_occurrences
                    ):
                        report.out_of_scope += 1
                        continue
                    decision = self._decide(
                        candidate=candidate,
                        reviewed=candidate_occurrences,
                        observed_projects=observed_projects,
                        individual_collection_observed=individual_collection_observed,
                        authority=authority,
                        individual_was_generated=clip_id in generated_individual_ids,
                        proposal_effect_signature=baseline_effect_signatures.get(
                            clip_id
                        ),
                    )
                    decisions.append(decision)
                    report.candidates_considered += 1
                    report.approved += decision["review_status"] == "approved"
                    report.rejected += decision["review_status"] == "rejected"
                    report.conflicts += decision["review_status"] == "conflict"
                    report.modified += (
                        decision["review_status"] == "approved"
                        and bool(decision.get("manually_modified"))
                    )
                    report.decisions.append(decision)

            status = "complete_with_conflicts" if report.conflicts else "complete"
            persisted_occurrences = [
                occurrence.as_db()
                for occurrence in occurrences
                if occurrence.stockify_run_id == resolved_run_id
                and occurrence.stock_clip_id in known_ids
            ]
            self.repository.apply_reconciliation(
                reconcile_id=reconcile_id,
                stockify_run_id=resolved_run_id,
                decisions=decisions,
                occurrences=persisted_occurrences,
                status=status,
            )

            if report_path is not None:
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(
                    json.dumps(asdict(report), indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            if report.conflicts and not allow_conflicts:
                raise VClipError(
                    f"Reconcile found {report.conflicts} conflicting candidate(s). "
                    "Non-conflicting decisions were saved; resolve the conflicts or rerun "
                    "with --allow-conflicts."
                )
            return report
        except Exception as exc:
            if "Non-conflicting decisions were saved" not in str(exc):
                self.repository.fail_reconcile_run(reconcile_id, str(exc))
            raise

    def _read_reviewed_xml(
        self,
        path: Path,
    ) -> tuple[
        list[ReviewedOccurrence],
        list[UnidentifiedOccurrence],
        set[str],
        set[str],
    ]:
        root = parse_source(path).getroot()
        occurrences: list[ReviewedOccurrence] = []
        unidentified: list[UnidentifiedOccurrence] = []
        observed_projects: set[str] = set()
        embedded_run_ids: set[str] = set()

        for event in iter_source_events(root):
            event_name = event.get("name", "<unnamed event>")
            for project in [
                child for child in list(event) if local_name(child.tag) == "project"
            ]:
                project_name = project.get("name", "<unnamed project>")
                observed_projects.add(project_name)
                sequence = first_direct_child(project, "sequence")
                spine = first_direct_child(sequence, "spine") if sequence is not None else None
                if spine is None:
                    continue
                for clip in spine.iter():
                    if clip is spine or local_name(clip.tag) not in {"asset-clip", "video"}:
                        continue
                    metadata = read_vclip_metadata(clip)
                    stock_clip_id = metadata.get("com.vclip.stock_clip_id")
                    embedded_run_id = metadata.get("com.vclip.stockify_run_id", "")
                    if embedded_run_id:
                        embedded_run_ids.add(embedded_run_id)
                    representation = metadata.get("com.vclip.representation")
                    if representation not in {"compilation", "individual"}:
                        representation = (
                            "compilation"
                            if "Stock Compilation" in project_name
                            else "individual"
                        )
                    if not stock_clip_id:
                        if representation == "individual":
                            unidentified.append(
                                UnidentifiedOccurrence(
                                    representation=representation,
                                    event_name=event_name,
                                    project_name=project_name,
                                    source_start=clip.get("start", "0s"),
                                    duration=clip.get("duration", "0s"),
                                    timeline_offset=clip.get("offset", "0s"),
                                    effect_signature=video_treatment_signature(clip),
                                )
                            )
                        continue
                    occurrences.append(
                        ReviewedOccurrence(
                            stockify_run_id=embedded_run_id,
                            stock_clip_id=stock_clip_id,
                            source_project_id=metadata.get("com.vclip.source_project_id"),
                            representation=representation,
                            event_name=event_name,
                            project_name=project_name,
                            source_start=clip.get("start", "0s"),
                            duration=clip.get("duration", "0s"),
                            timeline_offset=clip.get("offset", "0s"),
                            effect_signature=video_treatment_signature(clip),
                        )
                    )
        return occurrences, unidentified, observed_projects, embedded_run_ids

    @staticmethod
    def _recover_individual_occurrences(
        *,
        candidates: list[dict[str, Any]],
        occurrences: list[ReviewedOccurrence],
        unidentified: list[UnidentifiedOccurrence],
        run_id: str,
    ) -> int:
        """Recover IDs safely from unique generated individual project names."""
        candidates_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for candidate in candidates:
            name = str(candidate.get("generated_clip_project_name") or "")
            if name:
                candidates_by_name[name].append(candidate)

        unidentified_by_name: dict[str, list[UnidentifiedOccurrence]] = defaultdict(list)
        for occurrence in unidentified:
            unidentified_by_name[occurrence.project_name].append(occurrence)

        identified_individual_ids = {
            occurrence.stock_clip_id
            for occurrence in occurrences
            if occurrence.representation == "individual"
        }
        recovered = 0
        for project_name, raw_occurrences in unidentified_by_name.items():
            matching_candidates = candidates_by_name.get(project_name, [])
            if len(raw_occurrences) != 1 or len(matching_candidates) != 1:
                continue
            candidate = matching_candidates[0]
            clip_id = str(candidate["stock_clip_id"])
            if clip_id in identified_individual_ids:
                continue
            raw = raw_occurrences[0]
            occurrences.append(
                ReviewedOccurrence(
                    stockify_run_id=run_id,
                    stock_clip_id=clip_id,
                    source_project_id=str(candidate["source_project_id"]),
                    representation="individual",
                    event_name=raw.event_name,
                    project_name=raw.project_name,
                    source_start=raw.source_start,
                    duration=raw.duration,
                    timeline_offset=raw.timeline_offset,
                    effect_signature=raw.effect_signature,
                )
            )
            identified_individual_ids.add(clip_id)
            recovered += 1
        return recovered

    def _resolve_run_id(self, requested: str | None, embedded: set[str]) -> str:
        if requested:
            if embedded and embedded != {requested}:
                raise VClipError(
                    "The reviewed XML's embedded Stockify run ID does not match --run-id."
                )
            return requested
        if len(embedded) == 1:
            return next(iter(embedded))
        if len(embedded) > 1:
            raise VClipError(
                "The reviewed XML contains candidates from multiple Stockify runs."
            )
        return str(self.repository.latest_stockify_run()["id"])

    def _review_dedupe_removed_ids(self, run_id: str) -> set[str]:
        """Clip IDs removed by pre-review dedupe or short-candidate prune."""
        try:
            from ..workflow.catalog import WorkflowCatalog
        except ImportError:
            return set()
        return WorkflowCatalog(
            self.repository.database
        ).all_pre_review_dedupe_removed_ids(run_id)

    @staticmethod
    def _project_scope(
        project_candidates: list[dict[str, Any]],
        observed_projects: set[str],
        scope: str,
    ) -> tuple[bool, bool]:
        compilation_names = {
            str(candidate.get("generated_compilation_name") or "")
            for candidate in project_candidates
        }
        individual_names = {
            str(candidate.get("generated_clip_project_name") or "")
            for candidate in project_candidates
        }
        compilation_observed = bool(compilation_names & observed_projects)
        individual_observed = bool(individual_names & observed_projects)
        if scope == "full-run":
            # Full-run means every candidate is in scope. It does not imply that
            # the Stockify layout contained both representations.
            return True, individual_observed
        return compilation_observed or individual_observed, individual_observed

    def _baseline_effect_signatures(
        self,
        run_id: str,
        reviewed_xml: Path,
    ) -> dict[str, str]:
        """Recompute proposal treatment hashes from the Stockify-emitted review XML.

        Prefer individual-project clips. This heals older catalogs whose stored
        effect_signature still reflected pre-sanitization source effects or an
        unstable raw-XML hash, while keeping comparison aligned with what the
        reviewer actually saw.
        """
        run = self.repository.get_stockify_run(run_id)
        output_path = self._resolve_stockify_output_xml(
            run.get("output_xml_path"),
            reviewed_xml,
        )
        if output_path is None:
            return {}
        try:
            root = parse_source(output_path).getroot()
        except Exception:
            return {}

        by_id: dict[str, dict[str, str]] = defaultdict(dict)
        for event in iter_source_events(root):
            for project in [
                child for child in list(event) if local_name(child.tag) == "project"
            ]:
                sequence = first_direct_child(project, "sequence")
                spine = first_direct_child(sequence, "spine") if sequence is not None else None
                if spine is None:
                    continue
                for clip in spine.iter():
                    if clip is spine or local_name(clip.tag) not in {"asset-clip", "video"}:
                        continue
                    metadata = read_vclip_metadata(clip)
                    clip_id = metadata.get("com.vclip.stock_clip_id")
                    if not clip_id:
                        continue
                    representation = metadata.get("com.vclip.representation")
                    if representation not in {"compilation", "individual"}:
                        representation = (
                            "compilation"
                            if "Stock Compilation" in project.get("name", "")
                            else "individual"
                        )
                    by_id[clip_id][representation] = video_treatment_signature(clip)
        return {
            clip_id: signatures.get("individual") or signatures.get("compilation") or ""
            for clip_id, signatures in by_id.items()
            if signatures.get("individual") or signatures.get("compilation")
        }

    @staticmethod
    def _resolve_stockify_output_xml(
        output_xml_path: str | None,
        reviewed_xml: Path,
    ) -> Path | None:
        if not output_xml_path:
            return None
        candidates = [Path(output_xml_path)]
        reviewed = reviewed_xml.resolve()
        parents = [reviewed.parent]
        if reviewed.parent.suffix == ".fcpxmld":
            parents.append(reviewed.parent.parent)
        for parent in parents:
            candidates.append(parent / Path(output_xml_path).name)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
            if candidate.is_dir() and (candidate / "Info.fcpxml").is_file():
                return candidate / "Info.fcpxml"
        return None

    def _decide(
        self,
        *,
        candidate: dict[str, Any],
        reviewed: list[ReviewedOccurrence],
        observed_projects: set[str],
        individual_collection_observed: bool,
        authority: str,
        individual_was_generated: bool,
        proposal_effect_signature: str | None = None,
    ) -> dict[str, Any]:
        clip_id = str(candidate["stock_clip_id"])
        proposal = (
            parse_time(candidate.get("proposed_start") or "0s"),
            parse_time(candidate.get("proposed_duration") or "0s"),
            str(
                proposal_effect_signature
                if proposal_effect_signature is not None
                else candidate.get("effect_signature")
                or ""
            ),
        )
        compilation = next(
            (item for item in reviewed if item.representation == "compilation"),
            None,
        )
        individual = next(
            (item for item in reviewed if item.representation == "individual"),
            None,
        )
        compilation_name = str(candidate.get("generated_compilation_name") or "")
        individual_name = str(candidate.get("generated_clip_project_name") or "")
        compilation_expected_observed = compilation_name in observed_projects
        individual_expected_observed = individual_name in observed_projects
        compilation_deleted = compilation_expected_observed and compilation is None
        # An individual project is deleted when its generated project is gone while
        # related review material for the same source project is still present
        # (sibling individuals and/or the informational Stock Compilation).
        individual_deleted = bool(individual_name) and individual is None and (
            individual_expected_observed
            or individual_collection_observed
            or compilation_expected_observed
        )

        # Normal Stockify layout emits one individual project per candidate. That
        # individual project is authoritative: delete it to reject, leave it to
        # approve, trim it to approve/modify. Stock Compilation is informational
        # and must not create conflicts against an individual-project decision.
        if authority == "compilation":
            return self._decision_from_authority(
                clip_id,
                candidate,
                occurrence=compilation,
                deleted=compilation_deleted,
                proposal=proposal,
                authority="compilation",
            )
        if authority == "individual" or (
            authority == "auto" and individual_was_generated
        ):
            return self._decision_from_authority(
                clip_id,
                candidate,
                occurrence=individual,
                deleted=individual_deleted,
                proposal=proposal,
                authority="individual",
            )

        # Compilation-only Stockify layouts (no generated individual projects).
        return self._decision_from_authority(
            clip_id,
            candidate,
            occurrence=compilation,
            deleted=compilation_deleted,
            proposal=proposal,
            authority="compilation",
        )

    def _decision_from_authority(
        self,
        clip_id: str,
        candidate: dict[str, Any],
        *,
        occurrence: ReviewedOccurrence | None,
        deleted: bool,
        proposal: tuple[Fraction, Fraction, str],
        authority: str,
    ) -> dict[str, Any]:
        if deleted:
            reason = (
                "deleted_individual_project"
                if authority == "individual"
                else "deleted_from_compilation"
            )
            return self._rejected_decision(clip_id, reason=reason)
        if occurrence is None:
            return self._rejected_decision(clip_id, reason=f"missing_{authority}")
        return self._approved_decision(clip_id, candidate, occurrence, proposal, authority)

    @staticmethod
    def _approved_decision(
        clip_id: str,
        candidate: dict[str, Any],
        occurrence: ReviewedOccurrence | None,
        proposal: tuple[Fraction, Fraction, str],
        authority: str,
    ) -> dict[str, Any]:
        if occurrence is None:
            return ReconcileService._rejected_decision(
                clip_id, reason="missing_authoritative_occurrence"
            )
        state = occurrence.state()
        changes: dict[str, Any] = {"authority": authority}
        if state[0] != proposal[0]:
            changes["start"] = {
                "proposed": format_time(proposal[0]),
                "final": format_time(state[0]),
            }
        if state[1] != proposal[1]:
            changes["duration"] = {
                "proposed": format_time(proposal[1]),
                "final": format_time(state[1]),
            }
        if state[2] != proposal[2]:
            changes["video_treatment_changed"] = True
        modified = len(changes) > 1
        final_timeline_offset = None
        final_project_timecode = None
        if occurrence.representation == "compilation":
            final_timeline_offset = occurrence.timeline_offset
            fps = int(candidate.get("source_fps") or 30)
            final_project_timecode = format_project_timecode(
                parse_time(occurrence.timeline_offset),
                fps,
            )
        return {
            "stock_clip_id": clip_id,
            "review_status": "approved",
            "final_start": format_time(state[0]),
            "final_duration": format_time(state[1]),
            "final_duration_seconds": float(state[1]),
            "final_effect_signature": state[2],
            "final_compilation_timeline_offset": final_timeline_offset,
            "final_project_timecode": final_project_timecode,
            "manually_modified": modified,
            "manual_change": changes,
        }

    @staticmethod
    def _rejected_decision(clip_id: str, *, reason: str) -> dict[str, Any]:
        return {
            "stock_clip_id": clip_id,
            "review_status": "rejected",
            "final_start": None,
            "final_duration": None,
            "final_duration_seconds": None,
            "final_effect_signature": None,
            "final_compilation_timeline_offset": None,
            "final_project_timecode": None,
            "manually_modified": True,
            "manual_change": {"reason": reason},
        }

    @staticmethod
    def _conflict_decision(
        clip_id: str,
        reason: str,
        compilation: ReviewedOccurrence | None,
        individual: ReviewedOccurrence | None,
    ) -> dict[str, Any]:
        return {
            "stock_clip_id": clip_id,
            "review_status": "conflict",
            "final_start": None,
            "final_duration": None,
            "final_duration_seconds": None,
            "final_effect_signature": None,
            "final_compilation_timeline_offset": None,
            "final_project_timecode": None,
            "manually_modified": True,
            "manual_change": {
                "reason": reason,
                "compilation": asdict(compilation) if compilation else None,
                "individual": asdict(individual) if individual else None,
            },
        }

    def _announce(self, message: str) -> None:
        if self.progress:
            self.progress(message)
