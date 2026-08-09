"""Recover locations for Stockify sessions stuck on Unknown Location."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from ..db.repository import CatalogRepository
from ..errors import VClipError
from ..geo import LocationResolver, haversine_meters
from ..util import safe_filename, utc_now
from .core import local_name, parse_time
from .flight_location import (
    FlightIdentity,
    TrajectorySample,
    resolve_flight_trajectory,
)
from .metadata import (
    extract_gps_summary,
    is_usable_gps,
    parse_iso_local_datetime,
)
from .naming import (
    TIME_LABELS,
    disambiguate_event_names,
    event_base_name,
    project_base_label,
)
from .sidecars import normalized_stem, parse_srt_info
from .volume_index import (
    VolumeFileIndex,
    build_volume_file_index,
    first_existing_path,
    sibling_srt_for_media,
)

NEARBY_SESSION_METERS = 2000.0


@dataclass
class LocationRecoveryReport:
    stockify_runs_scanned: int = 0
    stockify_run_ids: list[str] = field(default_factory=list)
    unknown_sessions_before: int = 0
    resolved_by_srt_consensus: int = 0
    still_unknown: int = 0
    clips_recovered: int = 0
    review_xmls_rewritten: int = 0
    rewritten_review_xmls: list[str] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    sessions: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def format_location_recovery_report(report: LocationRecoveryReport) -> list[str]:
    """Human-readable aggregate summary lines for the CLI."""
    return [
        f"Stockify runs scanned:       {report.stockify_runs_scanned}",
        f"Unknown sessions before:     {report.unknown_sessions_before}",
        f"Resolved by SRT consensus:   {report.resolved_by_srt_consensus}",
        f"Still unknown:               {report.still_unknown}",
        f"Clips recovered:             {report.clips_recovered}",
        f"Review XMLs rewritten:       {report.review_xmls_rewritten}",
    ]


class LocationRecoveryService:
    """Post-Stockify pass that fills Unknown Location sessions from flight GPS."""

    def __init__(
        self,
        repository: CatalogRepository,
        location_resolver: LocationResolver,
        *,
        session_gap_hours: float = 4.0,
        scan_roots: Iterable[Path] | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.repository = repository
        self.location_resolver = location_resolver
        self.session_gap_hours = session_gap_hours
        self.scan_roots = [Path(root) for root in (scan_roots or [Path("/Volumes")])]
        self.progress = progress
        self._srt_cache: dict[str, Any] = {}
        self._volume_index: VolumeFileIndex = VolumeFileIndex()

    def run(
        self,
        *,
        run_id: str | None,
        dry_run: bool,
        rewrite_review_xml: bool,
        report_path: Path | None,
        refresh_resolved: bool = False,
    ) -> LocationRecoveryReport:
        if run_id:
            runs = [self.repository.get_stockify_run(run_id)]
        else:
            runs = self.repository.list_stockify_runs(completed_only=True)
            if not runs:
                raise VClipError("The database does not contain a Stockify run.")

        report = LocationRecoveryReport(
            stockify_runs_scanned=len(runs),
            stockify_run_ids=[str(run["id"]) for run in runs],
        )
        self._announce(
            f"Scanning {len(runs)} Stockify run(s) for "
            + (
                "session location refresh."
                if refresh_resolved
                else "Unknown Location sessions."
            )
        )
        self._volume_index = self._build_volume_index_for_runs(
            runs,
            include_resolved=refresh_resolved,
        )

        for run in runs:
            try:
                run_summary = self._recover_run(
                    run=run,
                    dry_run=dry_run,
                    rewrite_review_xml=rewrite_review_xml,
                    refresh_resolved=refresh_resolved,
                )
            except Exception as exc:
                run_id_value = str(run.get("id") or "unknown")
                warning = f"{run_id_value}: run recovery failed: {exc}"
                self._announce(warning)
                run_summary = {
                    "stockify_run_id": run_id_value,
                    "unknown_sessions_before": 0,
                    "resolved_by_srt_consensus": 0,
                    "still_unknown": 0,
                    "clips_recovered": 0,
                    "rewritten_review_xml": None,
                    "sessions": [],
                    "warnings": [warning],
                    "error": str(exc),
                }
            report.runs.append(run_summary)
            report.unknown_sessions_before += int(run_summary["unknown_sessions_before"])
            report.resolved_by_srt_consensus += int(
                run_summary["resolved_by_srt_consensus"]
            )
            report.still_unknown += int(run_summary["still_unknown"])
            report.clips_recovered += int(run_summary["clips_recovered"])
            report.sessions.extend(run_summary["sessions"])
            report.warnings.extend(run_summary.get("warnings") or [])
            rewritten = run_summary.get("rewritten_review_xml")
            if rewritten:
                report.rewritten_review_xmls.append(str(rewritten))

        report.review_xmls_rewritten = len(report.rewritten_review_xmls)

        if report_path is not None:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(asdict(report), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        return report

    def _recover_run(
        self,
        *,
        run: dict[str, Any],
        dry_run: bool,
        rewrite_review_xml: bool,
        refresh_resolved: bool = False,
    ) -> dict[str, Any]:
        """Recover unknown sessions for one Stockify run without cross-run merging."""
        resolved_run_id = str(run["id"])
        sessions = self.repository.sessions_for_run(resolved_run_id)
        candidates = self.repository.candidates_for_run(
            resolved_run_id,
            accepted_only=True,
        )
        candidates_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for candidate in candidates:
            session_id = str(candidate.get("session_id") or "")
            if session_id:
                candidates_by_session[session_id].append(candidate)

        unknown = [session for session in sessions if _is_unknown_session(session)]
        targets = list(sessions) if refresh_resolved else unknown
        summary: dict[str, Any] = {
            "stockify_run_id": resolved_run_id,
            "unknown_sessions_before": len(unknown),
            "resolved_by_srt_consensus": 0,
            "still_unknown": 0,
            "clips_recovered": 0,
            "rewritten_review_xml": None,
            "sessions": [],
            "warnings": [],
        }
        if not targets:
            return summary

        self._announce(
            f"{resolved_run_id}: recovering {len(targets)} session(s)"
            + (" (refresh resolved)" if refresh_resolved else "")
            + "."
        )

        outcomes: list[dict[str, Any]] = []
        for session in targets:
            session_id = str(session["id"])
            try:
                outcome = self._recover_session(
                    session=session,
                    candidates=candidates_by_session.get(session_id, []),
                    # Keep nearby/sibling evidence inside this run only.
                    all_sessions=sessions,
                    all_candidates=candidates,
                )
            except Exception as exc:
                warning = (
                    f"{resolved_run_id}: session {session_id} recovery failed: {exc}"
                )
                self._announce(warning)
                summary["warnings"].append(warning)
                outcome = {
                    "session_id": session_id,
                    "status": "error",
                    "method": None,
                    "reason": "recovery_exception",
                    "error": str(exc),
                    "contributing_clip_ids": [],
                }
            outcome["stockify_run_id"] = resolved_run_id
            outcomes.append(outcome)

        resolved = [item for item in outcomes if item["status"] == "resolved"]
        self._apply_event_disambiguation(sessions, resolved)

        for outcome in resolved:
            if not dry_run:
                try:
                    self.repository.apply_location_recovery(
                        run_id=resolved_run_id,
                        session_id=str(outcome["session_id"]),
                        location=outcome["location"],
                        generated_event_name=str(outcome["generated_event_name"]),
                        generated_base_label=str(outcome["generated_base_label"]),
                        candidate_updates=outcome["candidate_updates"],
                        project_updates=outcome["project_updates"],
                    )
                except Exception as exc:
                    warning = (
                        f"{resolved_run_id}: session {outcome['session_id']} "
                        f"persist failed: {exc}"
                    )
                    summary["warnings"].append(warning)
                    outcome["status"] = "error"
                    outcome["reason"] = "persist_exception"
                    outcome["error"] = str(exc)
                    continue
            summary["resolved_by_srt_consensus"] += 1
            summary["clips_recovered"] += len(outcome["candidate_updates"])

        persisted = [item for item in outcomes if item["status"] == "resolved"]
        rename_events, rename_projects = _rename_maps_for_outcomes(
            sessions=targets,
            outcomes=persisted,
            candidates_by_session=candidates_by_session,
        )

        summary["sessions"] = outcomes
        if refresh_resolved:
            # After refresh, recount unknowns from outcomes + untouched sessions.
            resolved_ids = {
                str(item["session_id"])
                for item in outcomes
                if item.get("status") == "resolved"
            }
            still = 0
            for session in sessions:
                if str(session["id"]) in resolved_ids:
                    continue
                if _is_unknown_session(session):
                    still += 1
            summary["still_unknown"] = still
        else:
            summary["still_unknown"] = max(
                0,
                summary["unknown_sessions_before"]
                - summary["resolved_by_srt_consensus"],
            )

        if rewrite_review_xml and (rename_events or rename_projects):
            output_xml = Path(str(run.get("output_xml_path") or ""))
            if not output_xml.is_file():
                summary["warnings"].append(
                    f"{resolved_run_id}: cannot rewrite review XML; "
                    f"file missing: {output_xml}"
                )
            elif dry_run:
                summary["rewritten_review_xml"] = str(output_xml)
                summary["warnings"].append(
                    f"{resolved_run_id}: dry run; review XML rewrite skipped."
                )
            else:
                try:
                    changed = rewrite_review_xml_names(
                        output_xml,
                        event_renames=rename_events,
                        project_renames=rename_projects,
                    )
                except Exception as exc:
                    summary["warnings"].append(
                        f"{resolved_run_id}: review XML rewrite failed: {exc}"
                    )
                else:
                    if changed:
                        summary["rewritten_review_xml"] = str(output_xml)
                    else:
                        summary["warnings"].append(
                            f"{resolved_run_id}: review XML rewrite found no matching "
                            "event/project names to update."
                        )
        elif rewrite_review_xml and not persisted and unknown:
            summary["warnings"].append(
                f"{resolved_run_id}: no sessions resolved; review XML left unchanged."
            )

        return summary

    def _recover_session(
        self,
        *,
        session: dict[str, Any],
        candidates: list[dict[str, Any]],
        all_sessions: list[dict[str, Any]],
        all_candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        identity = FlightIdentity(
            run_id=str(session.get("run_id") or ""),
            session_id=str(session["id"]),
            flight_id=None,  # Reserved for future DJI flight-log ids.
        )
        trajectory_samples = self._trajectory_samples_for_session(
            candidates=candidates,
            session_candidates=candidates,
            all_candidates=all_candidates,
        )
        gps_observations = [
            {
                "stock_clip_id": sample.stock_clip_id,
                "lat": sample.latitude,
                "lon": sample.longitude,
                "sample_count": sample.sample_count,
                "source": sample.source,
            }
            for sample in trajectory_samples
        ]
        nearby_support = self._nearby_session_support(
            session,
            gps_observations,
            all_sessions=all_sessions,
            all_candidates=all_candidates,
        )
        trajectory = resolve_flight_trajectory(
            trajectory_samples,
            self.location_resolver,
            identity=identity,
        )

        if trajectory.status == "unresolved":
            return {
                "session_id": session["id"],
                "status": "still_unknown",
                "method": "flight_trajectory",
                "reason": "no_reliable_clip_gps"
                if not trajectory_samples
                else "gps_unresolved_no_city",
                "contributing_clip_ids": trajectory.contributing_clip_ids,
                "trajectory": trajectory.diagnostics(),
                "nearby_support": nearby_support,
            }
        if trajectory.status == "multi_location":
            return {
                "session_id": session["id"],
                "status": "still_unknown",
                "method": "flight_trajectory",
                "reason": "multi_location_conflict",
                "contributing_clip_ids": trajectory.contributing_clip_ids,
                "trajectory": trajectory.diagnostics(),
                "geo_clusters": [
                    {
                        "cluster_id": cluster.cluster_id,
                        "source_count": cluster.source_count,
                        "center_lat": cluster.center_lat,
                        "center_lon": cluster.center_lon,
                        "source_keys": list(cluster.source_keys),
                        "place_labels": list(cluster.place_labels),
                    }
                    for cluster in trajectory.geo_clusters
                ],
                "nearby_support": nearby_support,
            }
        if trajectory.status == "conflict":
            return {
                "session_id": session["id"],
                "status": "still_unknown",
                "method": "flight_trajectory",
                "reason": "conflicting_gps",
                "contributing_clip_ids": trajectory.contributing_clip_ids,
                "trajectory": trajectory.diagnostics(),
                "nearby_support": nearby_support,
            }

        assert trajectory.location is not None
        location = dict(trajectory.location)
        if location.get("timezone") is None and session.get("timezone"):
            location["timezone"] = session.get("timezone")
        name_corroboration = _name_corroborates_place(candidates, location)
        evidence_sources = sorted(
            {
                *list(location.get("evidence_sources") or []),
                "flight_session_trajectory",
                *(["name_hint_corroboration"] if name_corroboration else []),
                *(["nearby_session"] if nearby_support else []),
            }
        )
        location["evidence_sources"] = evidence_sources
        location["recovery"] = {
            "method": "flight_trajectory",
            "confidence": location.get("confidence") or "medium",
            "coherence": trajectory.coherence,
            "contributing_clip_ids": trajectory.contributing_clip_ids,
            "contributing_source_keys": trajectory.contributing_source_keys,
            "evidence_sources": evidence_sources,
            "nearby_session_ids": [item["session_id"] for item in nearby_support],
            "flight_id": identity.flight_id,
            "recovered_at": utc_now(),
            "trajectory": trajectory.diagnostics(),
        }

        capture = dict(session.get("capture") or {})
        if session.get("capture_date"):
            capture.setdefault("date", session.get("capture_date"))
        if session.get("captured_at_local"):
            capture.setdefault("captured_at_local", session.get("captured_at_local"))
        if location.get("timezone"):
            capture["timezone"] = location.get("timezone")

        time_of_day = {
            "label": session.get("time_of_day") or "unknown",
            "confidence": session.get("time_of_day_confidence") or "low",
        }
        generated_event_name = event_base_name(location, capture)
        generated_base_label = project_base_label(location, time_of_day)

        project_labels: dict[str, str] = {}
        project_updates: list[dict[str, Any]] = []
        project_renames: list[dict[str, str]] = []
        candidate_updates: list[dict[str, Any]] = []
        contributing = set(trajectory.contributing_clip_ids)
        # Also treat clips on contributing source recordings as direct contributors.
        source_keys_with_gps = set(trajectory.contributing_source_keys)
        for candidate in candidates:
            source_key = _source_key(candidate)
            old_label = str(candidate.get("generated_project_label") or "")
            if old_label not in project_labels:
                new_label = _relabel_project(old_label, generated_base_label)
                project_labels[old_label] = new_label
                project_renames.append(
                    {
                        "old_project_label": old_label,
                        "new_project_label": new_label,
                    }
                )
                project_updates.append(
                    {
                        "source_project_id": candidate["source_project_id"],
                        "generated_event_name": generated_event_name,
                        "generated_project_label": new_label,
                        "generated_compilation_name": safe_filename(
                            f"{new_label} — Stock Compilation"
                        ),
                    }
                )
            new_label = project_labels[old_label]
            seq = candidate.get("clip_sequence")
            clip_name = (
                safe_filename(f"{new_label} — Clip {int(seq):02d}")
                if seq is not None
                else candidate.get("generated_clip_project_name")
            )
            compilation_name = safe_filename(f"{new_label} — Stock Compilation")
            # Flight location applies to every clip. Fragments whose exact trim
            # window has no usable GPS (early (0,0), pre-lock, etc.) inherit it.
            clip_window_has_gps = self._clip_window_has_usable_gps(candidate)
            on_gps_source = source_key in source_keys_with_gps
            clip_location = dict(location)
            if not clip_window_has_gps:
                clip_location = {
                    **location,
                    "evidence_sources": [
                        *evidence_sources,
                        "inherited_flight_trajectory",
                    ],
                    "recovery": {
                        **location["recovery"],
                        "inherited": True,
                        "inherited_from_source": on_gps_source,
                    },
                }
            candidate_updates.append(
                {
                    "stock_clip_id": candidate["stock_clip_id"],
                    "location": clip_location,
                    "generated_event_name": generated_event_name,
                    "generated_project_label": new_label,
                    "generated_clip_project_name": clip_name,
                    "generated_compilation_name": compilation_name,
                    "expected_export_basename": clip_name,
                }
            )

        return {
            "session_id": session["id"],
            "status": "resolved",
            "method": "flight_trajectory",
            "location": location,
            "generated_event_name": generated_event_name,
            "generated_base_label": generated_base_label,
            "contributing_clip_ids": sorted(contributing),
            "candidate_updates": candidate_updates,
            "project_updates": project_updates,
            "project_renames": project_renames,
            "trajectory": trajectory.diagnostics(),
            "nearby_support": nearby_support,
        }

    def _trajectory_samples_for_session(
        self,
        *,
        candidates: list[dict[str, Any]],
        session_candidates: list[dict[str, Any]],
        all_candidates: list[dict[str, Any]],
    ) -> list[TrajectorySample]:
        """Collect usable GPS from each source recording in the flight/session."""
        samples: list[TrajectorySample] = []
        seen_source_paths: set[str] = set()
        for candidate in candidates:
            source_key = _source_key(candidate)
            clip_id = str(candidate["stock_clip_id"])
            filename = (
                candidate.get("source_filename")
                or candidate.get("source_name")
                or source_key
            )

            # Prefer full-sidecar usable GPS so early (0,0) windows still contribute.
            summary = self._gps_from_srt_path(candidate, prefer_full_sidecar=True)
            if summary is None:
                existing = candidate.get("location") or {}
                if _has_gps(existing):
                    summary = {
                        "lat": float(existing["center_lat"]),
                        "lon": float(existing["center_lon"]),
                        "sample_count": existing.get("sample_count") or 1,
                        "source": "existing_candidate_srt_gps",
                    }
            if summary is None:
                # Same source asset elsewhere in the session/run.
                media_id = candidate.get("source_media_id")
                if media_id:
                    for sibling in session_candidates + all_candidates:
                        if sibling is candidate:
                            continue
                        if sibling.get("source_media_id") != media_id:
                            continue
                        sibling_summary = self._gps_from_srt_path(
                            sibling,
                            prefer_full_sidecar=True,
                        )
                        if sibling_summary is None:
                            sibling_loc = sibling.get("location") or {}
                            if _has_gps(sibling_loc):
                                sibling_summary = {
                                    "lat": float(sibling_loc["center_lat"]),
                                    "lon": float(sibling_loc["center_lon"]),
                                    "sample_count": sibling_loc.get("sample_count") or 1,
                                    "source": "same_asset_sibling_srt_gps",
                                }
                        if sibling_summary is not None:
                            summary = sibling_summary
                            break
            if summary is None:
                continue
            path_token = str(
                candidate.get("sidecar_path")
                or candidate.get("source_srt_path")
                or source_key
            )
            dedupe_key = f"{source_key}:{path_token}"
            if dedupe_key in seen_source_paths and any(
                item.source_key == source_key for item in samples
            ):
                # Still attach clip id to an existing source sample via a light copy.
                samples.append(
                    TrajectorySample(
                        latitude=float(summary["lat"]),
                        longitude=float(summary["lon"]),
                        source_key=source_key,
                        stock_clip_id=clip_id,
                        source_media_id=candidate.get("source_media_id"),
                        sample_count=int(summary.get("sample_count") or 1),
                        source=str(summary.get("source") or "srt"),
                        filename=str(filename),
                    )
                )
                continue
            seen_source_paths.add(dedupe_key)
            samples.append(
                TrajectorySample(
                    latitude=float(summary["lat"]),
                    longitude=float(summary["lon"]),
                    source_key=source_key,
                    stock_clip_id=clip_id,
                    source_media_id=candidate.get("source_media_id"),
                    sample_count=int(summary.get("sample_count") or 1),
                    source=str(summary.get("source") or "srt"),
                    filename=str(filename),
                )
            )
        return samples

    def _clip_window_has_usable_gps(self, candidate: dict[str, Any]) -> bool:
        srt_path = self._resolve_srt_path(candidate)
        if not srt_path:
            existing = candidate.get("location") or {}
            return _has_gps(existing)
        path = Path(str(srt_path))
        if not path.is_file():
            existing = candidate.get("location") or {}
            return _has_gps(existing)
        try:
            srt_info = self._srt_cache.get(str(path))
            if srt_info is None:
                srt_info = parse_srt_info(path)
                self._srt_cache[str(path)] = srt_info
        except OSError:
            return False
        start_raw = candidate.get("final_start") or candidate.get("proposed_start") or "0s"
        duration_raw = (
            candidate.get("final_duration")
            or candidate.get("proposed_duration")
            or "0s"
        )
        try:
            start = parse_time(str(start_raw))
            duration = parse_time(str(duration_raw))
        except ValueError:
            start = Fraction(0)
            duration = Fraction(0)
        summary = extract_gps_summary(
            srt_info,
            start=start,
            duration=duration,
            allow_full_sidecar_fallback=False,
        )
        return summary is not None

    def _build_volume_index_for_runs(
        self,
        runs: list[dict[str, Any]],
        *,
        include_resolved: bool = False,
    ) -> VolumeFileIndex:
        needed_stems: set[str] = set()
        for run in runs:
            run_id = str(run["id"])
            sessions = {
                str(session["id"])
                for session in self.repository.sessions_for_run(run_id)
                if include_resolved or _is_unknown_session(session)
            }
            if not sessions:
                continue
            for candidate in self.repository.candidates_for_run(
                run_id,
                accepted_only=True,
            ):
                if str(candidate.get("session_id") or "") not in sessions:
                    continue
                stem = _candidate_stem(candidate)
                if stem:
                    needed_stems.add(stem)
        return build_volume_file_index(
            needed_stems,
            self.scan_roots,
            progress=self.progress,
        )

    def _resolve_srt_path(self, candidate: dict[str, Any]) -> str | None:
        """Catalog sidecar, media sibling, then volume-scan match."""
        catalog = candidate.get("sidecar_path") or candidate.get("source_srt_path")
        stem = _candidate_stem(candidate)
        volume_matches = (
            self._volume_index.srt_by_stem.get(stem) or [] if stem else []
        )
        return first_existing_path(
            catalog,
            sibling_srt_for_media(candidate.get("source_media_path")),
            *volume_matches,
        )

    def _gps_from_srt_path(
        self,
        candidate: dict[str, Any],
        *,
        prefer_full_sidecar: bool = False,
    ) -> dict[str, Any] | None:
        srt_path = self._resolve_srt_path(candidate)
        if not srt_path:
            return None
        path = Path(str(srt_path))
        if not path.is_file():
            return None
        try:
            srt_info = self._srt_cache.get(str(path))
            if srt_info is None:
                srt_info = parse_srt_info(path)
                self._srt_cache[str(path)] = srt_info
        except OSError:
            return None
        if prefer_full_sidecar:
            summary = extract_gps_summary(
                srt_info,
                allow_full_sidecar_fallback=True,
            )
        else:
            start_raw = (
                candidate.get("final_start") or candidate.get("proposed_start") or "0s"
            )
            duration_raw = (
                candidate.get("final_duration")
                or candidate.get("proposed_duration")
                or "0s"
            )
            try:
                start = parse_time(str(start_raw))
                duration = parse_time(str(duration_raw))
            except ValueError:
                start = Fraction(0)
                duration = Fraction(0)
            summary = extract_gps_summary(
                srt_info,
                start=start,
                duration=duration,
                allow_full_sidecar_fallback=True,
            )
        if summary is None:
            return None
        return {
            "lat": float(summary["center_lat"]),
            "lon": float(summary["center_lon"]),
            "radius_meters": summary.get("radius_meters"),
            "sample_count": summary.get("valid_sample_count")
            or summary.get("sample_count")
            or 0,
            "source": "srt_full" if prefer_full_sidecar else "srt_window",
            "srt_path": srt_path,
        }

    def _nearby_session_support(
        self,
        session: dict[str, Any],
        gps_observations: list[dict[str, Any]],
        *,
        all_sessions: list[dict[str, Any]],
        all_candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Find clearly-same-shoot sessions; used only as corroboration."""
        support: list[dict[str, Any]] = []
        session_date = session.get("capture_date")
        session_time = parse_iso_local_datetime(session.get("captured_at_local"))
        max_gap = self.session_gap_hours * 3600
        media_ids = {
            candidate.get("source_media_id")
            for candidate in all_candidates
            if candidate.get("session_id") == session.get("id")
            and candidate.get("source_media_id")
        }

        for other in all_sessions:
            if other.get("id") == session.get("id"):
                continue
            if _is_unknown_session(other):
                continue
            if other.get("capture_date") != session_date:
                continue
            other_time = parse_iso_local_datetime(other.get("captured_at_local"))
            if session_time and other_time:
                try:
                    gap = abs((session_time - other_time).total_seconds())
                except TypeError:
                    gap = abs(
                        (
                            session_time.replace(tzinfo=None)
                            - other_time.replace(tzinfo=None)
                        ).total_seconds()
                    )
                if gap > max_gap:
                    continue
            other_lat = other.get("center_lat")
            other_lon = other.get("center_lon")
            shared_media = False
            if media_ids:
                shared_media = any(
                    candidate.get("session_id") == other.get("id")
                    and candidate.get("source_media_id") in media_ids
                    for candidate in all_candidates
                )
            near_gps = False
            if (
                isinstance(other_lat, (int, float))
                and isinstance(other_lon, (int, float))
                and gps_observations
            ):
                near_gps = any(
                    haversine_meters(
                        float(item["lat"]),
                        float(item["lon"]),
                        float(other_lat),
                        float(other_lon),
                    )
                    <= NEARBY_SESSION_METERS
                    for item in gps_observations
                )
            if shared_media or near_gps:
                support.append(
                    {
                        "session_id": other["id"],
                        "public_label": other.get("public_label"),
                        "shared_media": shared_media,
                        "near_gps": near_gps,
                    }
                )
        return support

    def _apply_event_disambiguation(
        self,
        existing_sessions: list[dict[str, Any]],
        resolved: list[dict[str, Any]],
    ) -> None:
        if not resolved:
            return
        working: list[dict[str, Any]] = []
        resolved_ids = {item["session_id"] for item in resolved}
        for session in existing_sessions:
            if session["id"] in resolved_ids:
                continue
            if _is_unknown_session(session):
                continue
            location = session.get("location") or {}
            capture = session.get("capture") or {}
            working.append(
                {
                    "id": session["id"],
                    "event_base_name": event_base_name(location, capture)
                    if location.get("city") or location.get("public_label")
                    else session.get("generated_event_name"),
                    "captured_at_local": session.get("captured_at_local"),
                    "time_of_day": session.get("time_of_day"),
                    "generated_event_name": session.get("generated_event_name"),
                    "_resolved": False,
                }
            )
        for outcome in resolved:
            location = outcome["location"]
            session = next(
                item for item in existing_sessions if item["id"] == outcome["session_id"]
            )
            capture = dict(session.get("capture") or {})
            if session.get("capture_date"):
                capture.setdefault("date", session.get("capture_date"))
            working.append(
                {
                    "id": outcome["session_id"],
                    "event_base_name": event_base_name(location, capture),
                    "captured_at_local": session.get("captured_at_local"),
                    "time_of_day": session.get("time_of_day"),
                    "generated_event_name": outcome["generated_event_name"],
                    "_resolved": True,
                    "_outcome": outcome,
                }
            )
        disambiguate_event_names(working)
        for item in working:
            if not item.get("_resolved"):
                continue
            outcome = item["_outcome"]
            new_event = str(item["generated_event_name"])
            outcome["generated_event_name"] = new_event
            for update in outcome["candidate_updates"]:
                update["generated_event_name"] = new_event
            for update in outcome["project_updates"]:
                update["generated_event_name"] = new_event

    def _announce(self, message: str) -> None:
        if self.progress:
            self.progress(message)


def _rename_maps_for_outcomes(
    *,
    sessions: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    candidates_by_session: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, str], dict[str, str]]:
    rename_events: dict[str, str] = {}
    rename_projects: dict[str, str] = {}
    for outcome in outcomes:
        session = next(
            item for item in sessions if item["id"] == outcome["session_id"]
        )
        old_event = str(session.get("generated_event_name") or "")
        new_event = str(outcome["generated_event_name"])
        if old_event and new_event and old_event != new_event:
            rename_events[old_event] = new_event
        for mapping in outcome.get("project_renames", []):
            old_label = mapping["old_project_label"]
            new_label = mapping["new_project_label"]
            if old_label and new_label and old_label != new_label:
                rename_projects[old_label] = new_label
        for update in outcome.get("candidate_updates", []):
            old_candidate = next(
                (
                    item
                    for item in candidates_by_session.get(outcome["session_id"], [])
                    if item["stock_clip_id"] == update["stock_clip_id"]
                ),
                None,
            )
            if old_candidate is None:
                continue
            for old_key, new_key in (
                ("generated_clip_project_name", "generated_clip_project_name"),
                ("generated_compilation_name", "generated_compilation_name"),
            ):
                old_name = old_candidate.get(old_key)
                new_name = update.get(new_key)
                if old_name and new_name and old_name != new_name:
                    rename_projects[str(old_name)] = str(new_name)
    return rename_events, rename_projects


def rewrite_review_xml_names(
    path: Path,
    *,
    event_renames: dict[str, str],
    project_renames: dict[str, str],
) -> int:
    """Rename generated event/project titles in an existing review FCPXML."""
    tree = ET.parse(path)
    root = tree.getroot()
    changed = 0
    label_prefixes = {
        old: new
        for old, new in project_renames.items()
        if " — Clip " not in old and not old.endswith(" — Stock Compilation")
    }
    for node in root.iter():
        tag = local_name(node.tag)
        name = node.get("name")
        if not name:
            continue
        if tag == "event" and name in event_renames:
            node.set("name", event_renames[name])
            changed += 1
            continue
        if tag != "project":
            continue
        if name in project_renames:
            node.set("name", project_renames[name])
            changed += 1
            continue
        for old, new in sorted(label_prefixes.items(), key=lambda item: len(item[0]), reverse=True):
            if name.startswith(f"{old} — "):
                node.set("name", f"{new}{name[len(old):]}")
                changed += 1
                break
    if changed:
        ET.indent(root)
        tree.write(path, encoding="utf-8", xml_declaration=True)
    return changed


def _is_unknown_session(session: dict[str, Any]) -> bool:
    event = str(session.get("generated_event_name") or "")
    if "Unknown Location" in event:
        return True
    if session.get("city") or session.get("public_label"):
        return False
    location = session.get("location") or {}
    return not (location.get("city") or location.get("public_label"))


def _has_gps(location: dict[str, Any]) -> bool:
    return is_usable_gps(location.get("center_lat"), location.get("center_lon"))


def _candidate_stem(candidate: dict[str, Any]) -> str:
    stem = candidate.get("source_normalized_stem")
    if stem:
        return str(stem)
    for value in (
        candidate.get("source_filename"),
        candidate.get("source_name"),
        candidate.get("source_media_path"),
        candidate.get("sidecar_path"),
        candidate.get("source_srt_path"),
    ):
        normalized = normalized_stem(str(value) if value else None)
        if normalized:
            return normalized
    return ""


def _source_key(candidate: dict[str, Any]) -> str:
    media_id = candidate.get("source_media_id")
    if media_id:
        return str(media_id)
    for value in (
        candidate.get("source_normalized_stem"),
        candidate.get("source_filename"),
        candidate.get("source_srt_path"),
        candidate.get("sidecar_path"),
        candidate.get("stock_clip_id"),
    ):
        if value:
            return str(value)
    return "unknown_source"


def _name_corroborates_place(
    candidates: list[dict[str, Any]],
    place: dict[str, object],
) -> bool:
    aliases = {
        str(value).casefold()
        for value in (
            place.get("city"),
            place.get("neighborhood"),
            place.get("poi"),
            place.get("state"),
            *(place.get("aliases") or []),
        )
        if value
    }
    aliases.discard("")
    if not aliases:
        return False
    for candidate in candidates:
        haystack = " ".join(
            str(value)
            for value in (
                candidate.get("source_event_name"),
                candidate.get("source_project_name"),
            )
            if value
        ).casefold()
        if any(alias in haystack for alias in aliases):
            return True
    return False


def _relabel_project(old_label: str, new_base: str) -> str:
    """Replace Unknown Location base while preserving graded/treatment suffixes."""
    if not old_label:
        return new_base
    if " — " not in old_label:
        return new_base
    _base, suffix = old_label.split(" — ", 1)
    if suffix.lower() in {str(label).lower() for label in TIME_LABELS.values()}:
        return new_base
    return safe_filename(f"{new_base} — {suffix}")
