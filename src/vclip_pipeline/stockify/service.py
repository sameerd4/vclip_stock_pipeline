"""Stockify finished Final Cut libraries into review XML and durable catalog rows."""

from __future__ import annotations

import copy
import json
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, replace
from fractions import Fraction
from pathlib import Path
from typing import Any

from .. import __version__
from ..db.records import (
    CandidateRecord,
    GeneratedOccurrenceRecord,
    ProjectFamilyRecord,
    ShootSessionRecord,
    SourceEventRecord,
    SourceMediaRecord,
    SourceProjectRecord,
    StockifySnapshot,
)
from ..db.repository import CatalogRepository
from ..errors import VClipError
from ..geo import LocationResolver, as_place_resolver
from ..util import json_dumps, safe_filename, sha256_file, stable_id, utc_now
from .clips import (
    candidate_skip_reason,
    clean_clip_for_stock,
    is_primary_storyline_candidate,
    recover_short_clip,
    sanitize_review_clip_effects,
)
from .constants import VIDEO_CLIP_TAGS
from .core import (
    commandpost_filename_prefix,
    format_seconds,
    format_time,
    parse_time,
    safe_name,
    stable_uid,
)
from .domain import CandidateBuild, ProgressCallback, ProjectBuild, StockifyOptions
from .families import apply_emission_gates, select_project_families
from .fcpxml import (
    add_vclip_metadata,
    asset_conversion_lut,
    asset_media_paths,
    build_malformed_asset_replacement_map,
    build_resource_index,
    clone_resources,
    drop_unreferenced_malformed_assets,
    first_direct_child,
    format_metadata,
    format_project_timecode,
    has_custom_lut_effect,
    iter_source_events,
    local_name,
    make_project,
    output_resources,
    parse_source,
    prune_unreferenced_effect_resources,
    resource_report,
    sequence_settings,
    validate_fcpxml,
    video_effect_names,
    video_treatment_signature,
    write_fcpxml,
)
from .libraries import resolve_source_library
from .metadata import (
    classify_time_of_day,
    default_weather_metadata,
    parse_iso_local_datetime,
    resolve_capture_time,
    resolve_clip_location,
)
from .models import (
    InputReport,
    ProjectReport,
    RunReport,
    SegmentReport,
    SidecarIndex,
    SrtInfo,
    StockifyError,
)
from .naming import (
    assign_project_labels,
    disambiguate_event_names,
    event_base_name,
    project_base_label,
)
from .scoring import preflight_visual_scoring
from .sidecars import (
    build_sidecar_index,
    normalized_stem,
    parse_srt_info,
    sidecar_match_for_asset,
)


class StockifyService:
    """Coordinate analysis, session inference, XML generation, and persistence."""

    def __init__(
        self,
        repository: CatalogRepository,
        location_resolver: LocationResolver,
        *,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.repository = repository
        self.location_resolver = location_resolver
        self.progress = progress
        self.srt_cache: dict[Path, SrtInfo] = {}
        self.report: RunReport | None = None

    def run(self, options: StockifyOptions) -> RunReport:
        """Run the complete non-destructive Stockify lifecycle."""
        self.srt_cache.clear()
        self.report = None
        if options.layout not in {"timeline-batch", "project-per-clip", "both"}:
            raise VClipError(f"Unsupported output layout: {options.layout}")

        self._announce(f"Reading Final Cut Pro XML: {options.input_path.name}")
        tree = parse_source(options.input_path)
        source_root = tree.getroot()
        version = source_root.get("version", "unknown")
        source_xml_hash = sha256_file(options.input_path)
        run_id = f"STOCKIFY_{uuid.uuid4().hex.upper()}"

        self.repository.start_stockify_run(
            run_id=run_id,
            source_xml_path=str(options.input_path),
            source_xml_sha256=source_xml_hash,
            source_fcpxml_version=version,
            output_xml_path=str(options.output_path),
            report_path=str(options.report_path),
            manifest_path=str(options.manifest_path) if options.manifest_path else None,
            pipeline_version=__version__,
            options=options.as_json(),
        )

        try:
            report = self._execute(
                options=options,
                run_id=run_id,
                source_root=source_root,
                version=version,
            )
            self.repository.complete_stockify_run(run_id)
            source_library = resolve_source_library(
                requested_path=options.requested_path or options.input_path,
                input_path=options.input_path,
                source_root=source_root,
            )
            if source_library is not None:
                library_name, library_path = source_library
                self.repository.mark_library_processed(
                    library_name=library_name,
                    library_path=library_path,
                    stockify_run_id=run_id,
                )
                self._announce(f"Recorded processed library: {library_name}")
            return report
        except Exception as exc:
            self.repository.fail_stockify_run(run_id, str(exc))
            raise

    def _execute(
        self,
        *,
        options: StockifyOptions,
        run_id: str,
        source_root: ET.Element,
        version: str,
    ) -> RunReport:
        source_resources = first_direct_child(source_root, "resources")
        if source_resources is None:
            raise StockifyError("Input FCPXML does not contain <resources>.")

        resource_index = build_resource_index(source_resources)
        assets = [
            resource
            for resource in resource_index.values()
            if local_name(resource.tag) == "asset"
        ]
        replacement_refs = build_malformed_asset_replacement_map(assets)
        used_replacement_refs: dict[str, str] = {}

        report = RunReport(
            input_file=str(options.input_path),
            output_file=str(options.output_path),
            output_library_name=options.library_name,
            fcpxml_version=version,
            layout=options.layout,
            export_manifest_file=(
                str(options.manifest_path) if options.manifest_path else None
            ),
            assets_in_resources=len(assets),
            assets_with_conversion_lut=sum(
                1 for asset in assets if asset_conversion_lut(asset)
            ),
            stockify_run_id=run_id,
            database_file=str(options.database_path),
        )
        self.report = report
        report.input = InputReport(
            requested_path=str(options.requested_path or options.input_path),
            resolved_xml_path=str(options.input_path),
            fcpxml_version=version,
        )
        report.color_policy.require_camera_lut = options.require_camera_lut
        report.color_policy.require_custom_lut = options.require_custom_lut

        self._announce("Checking optional visual-analysis dependencies.")
        report.visual_preflight = preflight_visual_scoring(
            requested=options.visual_score,
            required_for_expansion=options.require_visual_for_expansion,
        )
        if report.visual_preflight.blockers:
            blockers = ", ".join(report.visual_preflight.blockers)
            raise StockifyError(f"Visual scoring preflight failed: {blockers}.")

        self._announce(
            "Indexing DJI SRT sidecars beside source media and under archive roots."
        )
        sidecar_index = build_sidecar_index(assets, options.sidecar_roots)
        report.sidecars = sidecar_index.summary
        report.warnings.extend(sidecar_index.summary.scan_errors)
        if sidecar_index.summary.ambiguous_asset_stems:
            report.warnings.append(
                f"{sidecar_index.summary.ambiguous_asset_stems} asset(s) had "
                "ambiguous duplicate-basename SRT matches and were left unmatched."
            )

        source_events = list(iter_source_events(source_root))
        report.source_events = len(source_events)
        self._announce(
            f"Found {len(assets)} media assets and {len(source_events)} Final Cut event(s)."
        )

        event_records: list[SourceEventRecord] = []
        project_builds: list[ProjectBuild] = []
        media_records: dict[str, SourceMediaRecord] = {}

        for event_index, source_event in enumerate(source_events, start=1):
            source_event_name = safe_name(
                source_event.get("name", ""),
                f"Event {event_index:03d}",
            )
            source_event_id = stable_id(
                "EVENT",
                run_id,
                source_event.get("uid") or source_event_name,
                event_index,
            )
            event_records.append(
                SourceEventRecord(
                    id=source_event_id,
                    run_id=run_id,
                    source_index=event_index,
                    source_name=source_event_name,
                    source_uid=source_event.get("uid"),
                )
            )
            source_projects = [
                child
                for child in list(source_event)
                if local_name(child.tag) == "project"
            ]
            self._announce(
                f"Event {event_index}/{len(source_events)}: {source_event_name} "
                f"({len(source_projects)} project(s))."
            )

            for project_index, source_project in enumerate(source_projects, start=1):
                source_project_name = safe_name(
                    source_project.get("name", ""),
                    f"Project {project_index:03d}",
                )
                if (
                    options.project_names is not None
                    and source_project_name not in options.project_names
                ):
                    continue

                project = self._analyze_project(
                    options=options,
                    run_id=run_id,
                    source_event_id=source_event_id,
                    source_event_name=source_event_name,
                    source_project=source_project,
                    source_project_name=source_project_name,
                    source_project_index=project_index,
                    resource_index=resource_index,
                    replacement_refs=replacement_refs,
                    used_replacement_refs=used_replacement_refs,
                    sidecar_index=sidecar_index,
                    media_records=media_records,
                )
                project_builds.append(project)

        sessions = self._assign_sessions(
            run_id=run_id,
            projects=project_builds,
            session_gap_hours=options.session_gap_hours,
        )
        report.shoot_sessions_generated = len(sessions)
        family_records = self._apply_project_family_selection(
            run_id=run_id,
            projects=project_builds,
        )
        self._finalize_project_names(project_builds, sessions)

        output_root, manifest, occurrence_records = self._build_output_xml(
            source_root=source_root,
            project_builds=project_builds,
            sessions=sessions,
            options=options,
            run_id=run_id,
        )

        generated_resources = output_resources(output_root)
        report.resources = resource_report(source_resources, generated_resources)
        report.resources.replaced_malformed_refs = used_replacement_refs
        drop_unreferenced_malformed_assets(generated_resources, output_root, report)
        dropped_ids = list(report.resources.dropped_malformed_asset_ids)
        report.resources = resource_report(source_resources, generated_resources)
        report.resources.dropped_malformed_asset_ids = dropped_ids
        report.resources.replaced_malformed_refs = used_replacement_refs

        self._announce("Validating the generated Final Cut Pro XML.")
        report.validation = validate_fcpxml(output_root)
        if not report.validation.passed:
            first_error = report.validation.errors[0]
            raise StockifyError(
                f"Generated FCPXML failed validation: {first_error} No output was written."
            )

        snapshot = self._build_snapshot(
            run_id=run_id,
            events=event_records,
            sessions=sessions,
            projects=project_builds,
            media_records=media_records,
            occurrences=occurrence_records,
            families=family_records,
        )
        self._announce(f"Persisting analysis to SQLite: {options.database_path}")
        self.repository.persist_stockify_snapshot(snapshot)

        options.output_path.parent.mkdir(parents=True, exist_ok=True)
        options.report_path.parent.mkdir(parents=True, exist_ok=True)
        self._announce(f"Writing review XML: {options.output_path}")
        write_fcpxml(output_root, options.output_path)

        if options.manifest_path is not None:
            options.manifest_path.parent.mkdir(parents=True, exist_ok=True)
            options.manifest_path.write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        options.report_path.write_text(
            json.dumps(asdict(report), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return report

    def _analyze_project(
        self,
        *,
        options: StockifyOptions,
        run_id: str,
        source_event_id: str,
        source_event_name: str,
        source_project: ET.Element,
        source_project_name: str,
        source_project_index: int,
        resource_index: dict[str, ET.Element],
        replacement_refs: dict[str, str],
        used_replacement_refs: dict[str, str],
        sidecar_index: SidecarIndex,
        media_records: dict[str, SourceMediaRecord],
    ) -> ProjectBuild:
        assert self.report is not None
        report = self.report
        report.source_projects += 1
        report.project_summary.source_projects += 1

        source_project_id = stable_id(
            "PROJECT",
            run_id,
            source_event_id,
            source_project.get("uid") or source_project_name,
            source_project_index,
        )
        project_report = ProjectReport(
            event=source_event_name,
            project=source_project_name,
        )
        report.projects.append(project_report)
        project = ProjectBuild(
            source_event_id=source_event_id,
            source_event_name=source_event_name,
            source_project_id=source_project_id,
            source_project_name=source_project_name,
            source_project_uid=source_project.get("uid"),
            source_project_index=source_project_index,
            source_project=source_project,
            project_report=project_report,
        )

        source_sequence = first_direct_child(source_project, "sequence")
        if source_sequence is None:
            project_report.classification = "unsupported_project"
            project_report.warnings.append("Project has no sequence and was skipped.")
            report.project_summary.unsupported_projects += 1
            return project
        source_spine = first_direct_child(source_sequence, "spine")
        if source_spine is None:
            project_report.classification = "unsupported_project"
            project_report.warnings.append("Project has no primary spine and was skipped.")
            report.project_summary.unsupported_projects += 1
            return project

        try:
            fmt, tc_format, audio_layout, audio_rate = sequence_settings(source_sequence)
        except StockifyError as exc:
            project_report.classification = "unsupported_project"
            project_report.warnings.append(str(exc))
            report.project_summary.unsupported_projects += 1
            return project
        project.sequence_format = fmt
        project.tc_format = tc_format
        project.audio_layout = audio_layout
        project.audio_rate = audio_rate
        project.format_info = format_metadata(resource_index, fmt)

        parent_map = {
            child: parent
            for parent in source_project.iter()
            for child in list(parent)
        }
        source_candidates = [
            node
            for node in source_spine.iter()
            if node is not source_spine
            and local_name(node.tag) in VIDEO_CLIP_TAGS
            and is_primary_storyline_candidate(node, parent_map)
        ]
        project_report.segments_found = len(source_candidates)
        report.timeline_segments_found += len(source_candidates)
        report.segment_summary.found += len(source_candidates)
        self._announce(
            f"Project {source_project_name}: evaluating "
            f"{len(source_candidates)} primary-storyline segment(s)."
        )

        for segment_index, source_clip in enumerate(source_candidates, start=1):
            if (
                options.max_segments_per_project is not None
                and len(project.accepted) >= options.max_segments_per_project
            ):
                project_report.warnings.append(
                    f"Stopped after {options.max_segments_per_project} accepted segments."
                )
                break
            candidate = self._analyze_candidate(
                options=options,
                run_id=run_id,
                project=project,
                source_clip=source_clip,
                segment_index=segment_index,
                resource_index=resource_index,
                replacement_refs=replacement_refs,
                used_replacement_refs=used_replacement_refs,
                sidecar_index=sidecar_index,
                media_records=media_records,
            )
            project.candidates.append(candidate)
            if candidate.eligibility_status == "accepted":
                candidate.clip_sequence = len(project.accepted) + 1
                project.accepted.append(candidate)
                if project.anchor is None:
                    # Deliberately use the first accepted timeline clip as the project anchor.
                    project.anchor = candidate
                    project_report.anchor_segment_index = candidate.segment_index

        if project.accepted:
            project_report.classification = "eligible_video_project"
            project_report.segments_written = len(project.accepted)
            report.project_summary.eligible_video_projects += 1
        elif not source_candidates:
            project_report.classification = "empty_project"
            report.project_summary.empty_projects += 1
        elif project_report.still_segments == len(source_candidates):
            project_report.classification = "photo_only_project"
            report.project_summary.photo_only_projects += 1
        else:
            project_report.classification = "unsupported_project"
            report.project_summary.unsupported_projects += 1
        return project

    def _analyze_candidate(
        self,
        *,
        options: StockifyOptions,
        run_id: str,
        project: ProjectBuild,
        source_clip: ET.Element,
        segment_index: int,
        resource_index: dict[str, ET.Element],
        replacement_refs: dict[str, str],
        used_replacement_refs: dict[str, str],
        sidecar_index: SidecarIndex,
        media_records: dict[str, SourceMediaRecord],
    ) -> CandidateBuild:
        assert self.report is not None
        report = self.report
        source_name = safe_name(
            source_clip.get("name", ""),
            f"Segment {segment_index:03d}",
        )
        clip_id = stable_id(
            "VCLIP",
            project.source_event_name,
            project.source_project_uid or project.source_project_name,
            project.source_project_index,
            segment_index,
            source_clip.get("ref") or "",
            source_name,
        )
        self._announce(
            f"  Segment {segment_index}: {source_name} ({clip_id[-8:]})."
        )

        original_start = source_clip.get("start", "0s")
        original_duration = source_clip.get("duration")
        original_duration_seconds: float | None = None
        try:
            parsed_duration = parse_time(original_duration)
            original_duration_seconds = float(parsed_duration)
        except ValueError as exc:
            return self._reject_candidate(
                run_id=run_id,
                project=project,
                clip_id=clip_id,
                source_clip=source_clip,
                source_name=source_name,
                segment_index=segment_index,
                reason="invalid_duration",
                detail=str(exc),
                original_start=original_start,
                original_duration=original_duration,
                original_duration_seconds=None,
            )
        if parsed_duration <= 0:
            return self._reject_candidate(
                run_id=run_id,
                project=project,
                clip_id=clip_id,
                source_clip=source_clip,
                source_name=source_name,
                segment_index=segment_index,
                reason="invalid_duration",
                detail="Timeline segment has non-positive duration.",
                original_start=original_start,
                original_duration=original_duration,
                original_duration_seconds=original_duration_seconds,
            )
        # Absolute input floor: do not attempt recovery for sub-floor fragments.
        if float(parsed_duration) < options.min_duration_seconds:
            return self._reject_candidate(
                run_id=run_id,
                project=project,
                clip_id=clip_id,
                source_clip=source_clip,
                source_name=source_name,
                segment_index=segment_index,
                reason="short_duration",
                detail=(
                    f"Duration {float(parsed_duration):.3f}s is below the "
                    f"{options.min_duration_seconds:.3f}s absolute input floor."
                ),
                original_start=original_start,
                original_duration=original_duration,
                original_duration_seconds=original_duration_seconds,
            )

        reason, asset, replacement_ref = candidate_skip_reason(
            source_clip,
            resource_index,
            replacement_refs,
        )
        media_id, media_record = self._ensure_media_record(
            run_id=run_id,
            asset=asset,
            resource_index=resource_index,
            sidecar_index=sidecar_index,
            media_records=media_records,
        )
        if reason:
            if reason == "photo_asset":
                project.project_report.still_segments += 1
            return self._reject_candidate(
                run_id=run_id,
                project=project,
                clip_id=clip_id,
                source_clip=source_clip,
                source_name=source_name,
                segment_index=segment_index,
                reason=reason,
                detail=f"Segment skipped: {reason.replace('_', ' ')}.",
                original_start=original_start,
                original_duration=original_duration,
                original_duration_seconds=original_duration_seconds,
                asset=asset,
                source_media_id=media_id,
                media_record=media_record,
            )

        camera_lut = asset_conversion_lut(asset)
        creative_effects = video_effect_names(source_clip, resource_index)
        has_custom_lut = has_custom_lut_effect(creative_effects)
        self._record_color_evaluation(camera_lut, has_custom_lut)

        if options.require_camera_lut and not camera_lut:
            report.color_policy.skipped_missing_camera_lut += 1
            return self._reject_candidate(
                run_id=run_id,
                project=project,
                clip_id=clip_id,
                source_clip=source_clip,
                source_name=source_name,
                segment_index=segment_index,
                reason="missing_camera_lut",
                detail="Source asset has no camera conversion LUT.",
                original_start=original_start,
                original_duration=original_duration,
                original_duration_seconds=original_duration_seconds,
                asset=asset,
                source_media_id=media_id,
                media_record=media_record,
            )
        if options.require_custom_lut and not has_custom_lut:
            report.color_policy.skipped_missing_custom_lut += 1
            return self._reject_candidate(
                run_id=run_id,
                project=project,
                clip_id=clip_id,
                source_clip=source_clip,
                source_name=source_name,
                segment_index=segment_index,
                reason="missing_custom_lut",
                detail="Timeline clip has no Custom LUT video effect.",
                original_start=original_start,
                original_duration=original_duration,
                original_duration_seconds=original_duration_seconds,
                asset=asset,
                source_media_id=media_id,
                media_record=media_record,
            )

        try:
            clean_clip, warnings, had_time_map, normalized = clean_clip_for_stock(
                source_clip,
                resource_index=resource_index,
                force_disable_audio=options.force_disable_audio,
                replacement_ref=replacement_ref,
            )
            recovery = recover_short_clip(
                clean_clip,
                source_clip,
                asset,
                sidecar_index=sidecar_index,
                srt_cache=self.srt_cache,
                enabled=options.recover_short_clips,
                short_clip_threshold_seconds=options.short_clip_threshold_seconds,
                minimum_duration_seconds=options.expanded_minimum_duration_seconds,
                preferred_duration_seconds=options.expanded_preferred_duration_seconds,
                ideal_duration_seconds=options.expanded_ideal_duration_seconds,
                require_srt_for_expansion=options.require_srt_for_expansion,
                visual_score=options.visual_score,
                require_visual_for_expansion=options.require_visual_for_expansion,
                visual_fps=options.visual_fps,
                visual_width=options.visual_width,
                visual_height=options.visual_height,
                visual_reject_shift_px=options.visual_reject_shift_px,
                visual_reject_frame_diff=options.visual_reject_frame_diff,
                visual_timeout_seconds=options.visual_timeout_seconds,
                progress=(
                    lambda message: self._announce(f"    {source_name}: {message}")
                ),
            )
        except (OSError, ValueError, StockifyError) as exc:
            return self._reject_candidate(
                run_id=run_id,
                project=project,
                clip_id=clip_id,
                source_clip=source_clip,
                source_name=source_name,
                segment_index=segment_index,
                reason="stockify_analysis_error",
                detail=str(exc),
                original_start=original_start,
                original_duration=original_duration,
                original_duration_seconds=original_duration_seconds,
                asset=asset,
                source_media_id=media_id,
                media_record=media_record,
            )

        warnings.extend(recovery.warnings)
        self._record_recovery(parsed_duration, recovery, options)
        output_start = clean_clip.get("start", "0s")
        output_duration = clean_clip.get("duration", "0s")
        output_duration_fraction = parse_time(output_duration)
        output_seconds = float(output_duration_fraction)
        original_seconds = float(parsed_duration)
        # Clips shorter than the short-clip threshold must be successfully
        # expanded to at least expanded_minimum_duration_seconds. Failed or
        # disabled recovery rejects; the original short clip is never emitted.
        needs_recovery = original_seconds < options.short_clip_threshold_seconds
        recovered_enough = (
            recovery.status in {"expanded", "expanded_review"}
            and output_seconds >= options.expanded_minimum_duration_seconds
        )
        if needs_recovery and not recovered_enough:
            return self._reject_candidate(
                run_id=run_id,
                project=project,
                clip_id=clip_id,
                source_clip=source_clip,
                source_name=source_name,
                segment_index=segment_index,
                reason="short_clip_unrecovered",
                detail=(
                    f"Duration {original_seconds:.3f}s is below the "
                    f"{options.short_clip_threshold_seconds:.3f}s short-clip "
                    f"threshold and recovery status {recovery.status!r} did not "
                    f"produce at least {options.expanded_minimum_duration_seconds:.3f}s "
                    f"(output {output_seconds:.3f}s)."
                ),
                original_start=original_start,
                original_duration=original_duration,
                original_duration_seconds=original_duration_seconds,
                asset=asset,
                source_media_id=media_id,
                media_record=media_record,
                proposed_start=output_start,
                proposed_duration=output_duration,
                short_clip_recovery=recovery.status,
                candidate_tier=recovery.candidate_tier,
                sidecar_path=recovery.sidecar_path,
                srt_status=recovery.srt_status,
                srt_window_status=recovery.srt_window_status,
                srt_reasons=list(recovery.smoothness_reasons),
                visual_status=recovery.visual_status,
                visual_reasons=list(recovery.visual_reasons),
                visual_metrics=recovery.visual_metrics,
            )
        if output_seconds < options.min_duration_seconds:
            return self._reject_candidate(
                run_id=run_id,
                project=project,
                clip_id=clip_id,
                source_clip=source_clip,
                source_name=source_name,
                segment_index=segment_index,
                reason="short_duration",
                detail=(
                    f"Duration {output_seconds:.3f}s remains below "
                    f"the {options.min_duration_seconds:.3f}s minimum after "
                    f"{recovery.status}."
                ),
                original_start=original_start,
                original_duration=original_duration,
                original_duration_seconds=original_duration_seconds,
                asset=asset,
                source_media_id=media_id,
                media_record=media_record,
                proposed_start=output_start,
                proposed_duration=output_duration,
                short_clip_recovery=recovery.status,
                candidate_tier=recovery.candidate_tier,
                sidecar_path=recovery.sidecar_path,
                srt_status=recovery.srt_status,
                srt_window_status=recovery.srt_window_status,
                srt_reasons=list(recovery.smoothness_reasons),
                visual_status=recovery.visual_status,
                visual_reasons=list(recovery.visual_reasons),
                visual_metrics=recovery.visual_metrics,
            )

        if replacement_ref and source_clip.get("ref"):
            used_replacement_refs[source_clip.get("ref", "")] = replacement_ref

        segment_srt_info = self._srt_info_for_path(recovery.sidecar_path)
        location = self._resolve_location(
            srt_info=segment_srt_info,
            start=parse_time(output_start),
            duration=output_duration_fraction,
            event_name=project.source_event_name,
            project_name=project.source_project_name,
            sidecar_path=recovery.sidecar_path,
        )
        capture_time = resolve_capture_time(
            srt_info=segment_srt_info,
            start=parse_time(output_start),
            duration=output_duration_fraction,
            source_name=source_name,
            sidecar_path=recovery.sidecar_path,
            location=location,
        )
        time_of_day = classify_time_of_day(
            parse_iso_local_datetime(str(capture_time.get("captured_at_local") or ""))
        )
        # Fingerprint the review-facing treatment (Custom LUT kept; other plugins
        # stripped) so Reconcile does not treat emission sanitization as a human edit.
        review_treatment_clip = sanitize_review_clip_effects(
            copy.deepcopy(clean_clip),
            resource_index,
        )
        effect_signature = video_treatment_signature(review_treatment_clip)

        add_vclip_metadata(
            clean_clip,
            {
                "com.vclip.stockify_run_id": run_id,
                "com.vclip.stock_clip_id": clip_id,
                "com.vclip.source_event": project.source_event_name,
                "com.vclip.source_project": project.source_project_name,
                "com.vclip.source_project_id": project.source_project_id,
                "com.vclip.source_segment_index": str(segment_index),
                "com.vclip.candidate_tier": recovery.candidate_tier,
            },
        )

        report_segment = SegmentReport(
            stock_clip_id=clip_id,
            source_event=project.source_event_name,
            source_project=project.source_project_name,
            source_segment_index=segment_index,
            output_project="",
            timeline_project=None,
            timeline_offset=None,
            project_timecode=None,
            source_ref=source_clip.get("ref"),
            source_name=source_name,
            start=original_start,
            duration=original_duration or "0s",
            output_start=output_start,
            output_duration=output_duration,
            had_time_map=had_time_map,
            retime_normalized=normalized,
            short_clip_recovery=recovery.status,
            original_duration_seconds=original_duration_seconds or 0.0,
            output_duration_seconds=float(output_duration_fraction),
            sidecar_path=recovery.sidecar_path,
            srt_match_method=(
                media_record.srt_match_method if media_record is not None else None
            ),
            srt_status=recovery.srt_status,
            srt_window_status=recovery.srt_window_status,
            visual_status=recovery.visual_status,
            visual_reasons=list(recovery.visual_reasons),
            visual_metrics=recovery.visual_metrics,
            smoothness_reasons=list(recovery.smoothness_reasons),
            candidate_tier=recovery.candidate_tier,
            location=location,
            capture_time=capture_time,
            time_of_day=time_of_day,
            weather=default_weather_metadata(),
            replacement_ref=replacement_ref,
            creative_video_effects=creative_effects,
            asset_conversion_lut=camera_lut,
            source_project_id=project.source_project_id,
            source_media_id=media_id,
            effect_signature=effect_signature,
            warnings=warnings,
        )
        report.segments.append(report_segment)
        report.segment_summary.written += 1
        report.stock_projects_written += 1
        self._record_written_color(camera_lut, has_custom_lut, creative_effects)

        candidate_record = CandidateRecord(
            run_id=run_id,
            stock_clip_id=clip_id,
            source_project_id=project.source_project_id,
            source_media_id=media_id,
            session_id=None,
            source_segment_index=segment_index,
            source_ref=source_clip.get("ref"),
            source_name=source_name,
            eligibility_status="accepted",
            rejection_reason=None,
            rejection_detail=None,
            original_start=original_start,
            original_duration=original_duration,
            original_duration_seconds=original_duration_seconds,
            proposed_start=output_start,
            proposed_duration=output_duration,
            proposed_duration_seconds=float(output_duration_fraction),
            short_clip_recovery=recovery.status,
            candidate_tier=recovery.candidate_tier,
            sidecar_path=recovery.sidecar_path,
            srt_status=recovery.srt_status,
            srt_window_status=recovery.srt_window_status,
            srt_reasons=list(recovery.smoothness_reasons),
            visual_status=recovery.visual_status,
            visual_reasons=list(recovery.visual_reasons),
            visual_metrics=recovery.visual_metrics,
            location=location,
            capture_time=capture_time,
            time_of_day=time_of_day,
            weather=default_weather_metadata(),
            replacement_ref=replacement_ref,
            creative_effects=creative_effects,
            camera_lut=camera_lut,
            effect_signature=effect_signature,
        )

        if media_record is not None and not media_record.location and location:
            media_record = replace(
                media_record,
                captured_at_local=str(capture_time.get("captured_at_local") or "") or None,
                captured_at_utc=str(capture_time.get("captured_at_utc") or "") or None,
                capture_date=str(capture_time.get("date") or "") or None,
                timezone=str(capture_time.get("timezone") or "") or None,
                location=location,
            )
            media_records[media_record.id] = media_record

        return CandidateBuild(
            stock_clip_id=clip_id,
            segment_index=segment_index,
            source_clip=source_clip,
            asset=asset,
            clean_clip=copy.deepcopy(clean_clip),
            segment_report=report_segment,
            eligibility_status="accepted",
            source_media_id=media_id,
            media_record=media_record,
            candidate_record=candidate_record,
        )

    def _reject_candidate(
        self,
        *,
        run_id: str,
        project: ProjectBuild,
        clip_id: str,
        source_clip: ET.Element,
        source_name: str,
        segment_index: int,
        reason: str,
        detail: str,
        original_start: str | None,
        original_duration: str | None,
        original_duration_seconds: float | None,
        asset: ET.Element | None = None,
        source_media_id: str | None = None,
        media_record: SourceMediaRecord | None = None,
        proposed_start: str | None = None,
        proposed_duration: str | None = None,
        short_clip_recovery: str | None = None,
        candidate_tier: str | None = None,
        sidecar_path: str | None = None,
        srt_status: str | None = None,
        srt_window_status: str | None = None,
        srt_reasons: list[str] | None = None,
        visual_status: str | None = None,
        visual_reasons: list[str] | None = None,
        visual_metrics: dict[str, Any] | None = None,
    ) -> CandidateBuild:
        assert self.report is not None
        project.project_report.segments_skipped += 1
        project.project_report.skip_reasons[reason] = (
            project.project_report.skip_reasons.get(reason, 0) + 1
        )
        project.project_report.warnings.append(
            f"Segment {segment_index} ({source_name}): {detail}"
        )
        self.report.skipped_segments += 1
        self.report.segment_summary.skipped += 1
        self.report.segment_summary.skip_reasons[reason] = (
            self.report.segment_summary.skip_reasons.get(reason, 0) + 1
        )
        proposed_seconds: float | None = None
        if proposed_duration:
            try:
                proposed_seconds = float(parse_time(proposed_duration))
            except ValueError:
                proposed_seconds = None
        record = CandidateRecord(
            run_id=run_id,
            stock_clip_id=clip_id,
            source_project_id=project.source_project_id,
            source_media_id=source_media_id,
            session_id=None,
            source_segment_index=segment_index,
            source_ref=source_clip.get("ref"),
            source_name=source_name,
            eligibility_status="rejected",
            rejection_reason=reason,
            rejection_detail=detail,
            original_start=original_start,
            original_duration=original_duration,
            original_duration_seconds=original_duration_seconds,
            proposed_start=proposed_start,
            proposed_duration=proposed_duration,
            proposed_duration_seconds=proposed_seconds,
            short_clip_recovery=short_clip_recovery,
            candidate_tier=candidate_tier,
            sidecar_path=sidecar_path,
            srt_status=srt_status,
            srt_window_status=srt_window_status,
            srt_reasons=srt_reasons or [],
            visual_status=visual_status,
            visual_reasons=visual_reasons or [],
            visual_metrics=visual_metrics or {},
        )
        self._announce(f"    Rejected: {reason.replace('_', ' ')}.")
        return CandidateBuild(
            stock_clip_id=clip_id,
            segment_index=segment_index,
            source_clip=source_clip,
            asset=asset,
            clean_clip=None,
            segment_report=None,
            eligibility_status="rejected",
            rejection_reason=reason,
            rejection_detail=detail,
            source_media_id=source_media_id,
            media_record=media_record,
            candidate_record=record,
        )

    def _ensure_media_record(
        self,
        *,
        run_id: str,
        asset: ET.Element | None,
        resource_index: dict[str, ET.Element],
        sidecar_index: SidecarIndex,
        media_records: dict[str, SourceMediaRecord],
    ) -> tuple[str | None, SourceMediaRecord | None]:
        if asset is None:
            return None, None
        asset_ref = asset.get("id")
        media_id = stable_id(
            "MEDIA",
            run_id,
            asset_ref or asset.get("name") or "unknown",
        )
        existing = media_records.get(media_id)
        if existing is not None:
            return media_id, existing

        paths = asset_media_paths(asset)
        media_path = paths[0] if paths else None
        filename = media_path.name if media_path else asset.get("name")
        match = sidecar_match_for_asset(asset, sidecar_index)
        srt_info = self._srt_info_for_path(str(match.path) if match.path else None)
        duration = asset.get("duration")
        duration_seconds: float | None = None
        if duration:
            try:
                duration_seconds = float(parse_time(duration))
            except ValueError:
                pass
        fmt = asset.get("format")
        fmt_info = format_metadata(resource_index, fmt or "")
        record = SourceMediaRecord(
            id=media_id,
            run_id=run_id,
            asset_ref=asset_ref,
            asset_name=asset.get("name"),
            original_filename=filename,
            media_path=str(media_path) if media_path else None,
            normalized_stem=normalized_stem(filename),
            duration=duration,
            duration_seconds=duration_seconds,
            format_id=fmt,
            width=self._optional_int(fmt_info.get("width")),
            height=self._optional_int(fmt_info.get("height")),
            fps=self._optional_int(fmt_info.get("timecode_fps")),
            camera_lut=asset_conversion_lut(asset),
            srt_path=str(match.path) if match.path else None,
            srt_match_method=match.method,
            srt_match_confidence=match.confidence,
            srt_match_ambiguous=match.ambiguous,
            srt_match_candidate_count=match.archive_candidate_count,
            srt_sample_count=srt_info.sample_count if srt_info else None,
            srt_start=format_time(srt_info.start) if srt_info else None,
            srt_end=format_time(srt_info.end) if srt_info else None,
            srt_has_position=srt_info.has_position if srt_info else None,
            srt_has_altitude=srt_info.has_altitude if srt_info else None,
            srt_has_orientation=srt_info.has_orientation if srt_info else None,
            captured_at_local=None,
            captured_at_utc=None,
            capture_date=None,
            timezone=None,
            location={},
        )
        media_records[media_id] = record
        return media_id, record

    def _assign_sessions(
        self,
        *,
        run_id: str,
        projects: list[ProjectBuild],
        session_gap_hours: float,
    ) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []
        max_gap_seconds = session_gap_hours * 3600

        for project in projects:
            if project.anchor is None or project.anchor.segment_report is None:
                continue
            segment = project.anchor.segment_report
            location = segment.location
            capture = segment.capture_time
            time_of_day = segment.time_of_day
            location_key = "|".join(
                str(location.get(key) or "").lower()
                for key in ("country", "state", "city", "neighborhood", "poi")
            )
            if not location_key.strip("|"):
                latitude = location.get("center_lat")
                longitude = location.get("center_lon")
                if isinstance(latitude, (int, float)) and isinstance(
                    longitude, (int, float)
                ):
                    location_key = f"gps:{float(latitude):.3f}:{float(longitude):.3f}"
                else:
                    # Never merge unrelated unresolved projects simply because
                    # they happen to share a date.
                    location_key = f"unknown:{project.source_project_id}"
            date = str(capture.get("date") or "unknown")
            captured_at = parse_iso_local_datetime(
                str(capture.get("captured_at_local") or "")
            )

            match: dict[str, Any] | None = None
            for session in sessions:
                if session["location_key"] != location_key or session["capture_date"] != date:
                    continue
                session_time = session.get("captured_at")
                if captured_at is None or session_time is None:
                    match = session
                    break
                try:
                    gap_seconds = abs((captured_at - session_time).total_seconds())
                except TypeError:
                    # Be defensive if one timestamp carried a UTC offset and an
                    # older sidecar produced a naive local timestamp.
                    gap_seconds = abs(
                        (
                            captured_at.replace(tzinfo=None)
                            - session_time.replace(tzinfo=None)
                        ).total_seconds()
                    )
                if gap_seconds <= max_gap_seconds:
                    match = session
                    break

            if match is None:
                session_index = 1 + sum(
                    session["location_key"] == location_key
                    and session["capture_date"] == date
                    for session in sessions
                )
                session_id = stable_id(
                    "SESSION",
                    run_id,
                    location_key,
                    date,
                    session_index,
                )
                match = {
                    "id": session_id,
                    "run_id": run_id,
                    "session_key": stable_id(
                        "SESSIONKEY", location_key, date, session_index
                    ),
                    "location_key": location_key,
                    "capture_date": None if date == "unknown" else date,
                    "captured_at": captured_at,
                    "captured_at_local": capture.get("captured_at_local"),
                    "timezone": capture.get("timezone") or location.get("timezone"),
                    "location": location,
                    "capture": capture,
                    "time_of_day": time_of_day.get("label"),
                    "time_of_day_confidence": time_of_day.get("confidence"),
                    "event_base_name": event_base_name(location, capture),
                    "generated_event_name": None,
                    "generated_base_label": project_base_label(location, time_of_day),
                    "anchor_stock_clip_id": segment.stock_clip_id,
                    "projects": [],
                }
                sessions.append(match)
            match["projects"].append(project)
            project.session_id = str(match["id"])
            project.generated_project_label = project_base_label(location, time_of_day)

        disambiguate_event_names(sessions)
        for session in sessions:
            for project in session["projects"]:
                project.generated_event_name = str(session["generated_event_name"])
        return sessions

    def _apply_project_family_selection(
        self,
        *,
        run_id: str,
        projects: list[ProjectBuild],
    ) -> list[ProjectFamilyRecord]:
        """Keep only the best graded project family members for review emission."""
        assert self.report is not None
        report = self.report
        families = select_project_families(projects, run_id=run_id)
        selected_count = sum(1 for family in families if family.selected_source_project_id)
        if families:
            self._announce(
                f"Resolved {len(families)} source-project famil"
                f"{'y' if len(families) == 1 else 'ies'}; "
                f"{selected_count} graded winner(s) kept for review emission."
            )

        demoted_ids = set(apply_emission_gates(projects))
        for project in projects:
            if project.family_role not in {"superseded", "withheld"}:
                if project.family_role in {"selected", "standalone"}:
                    project.project_report.segments_written = len(project.accepted)
                continue
            reason = project.family_selection_reason or project.family_role
            demoted_here = [
                candidate
                for candidate in project.candidates
                if candidate.rejection_reason
                in {"superseded_project_family", "insufficient_grading"}
            ]
            project.project_report.segments_skipped += max(
                project.project_report.segments_written,
                len(demoted_here),
            )
            project.project_report.segments_written = 0
            skip_key = (
                "insufficient_grading"
                if project.family_role == "withheld"
                else "superseded_project_family"
            )
            project.project_report.skip_reasons[skip_key] = (
                project.project_report.skip_reasons.get(skip_key, 0) + len(demoted_here)
            )
            if project.family_role == "withheld":
                project.project_report.warnings.append(
                    "Withheld from review emission due to insufficient grading "
                    f"coverage ({reason})."
                )
            else:
                project.project_report.warnings.append(
                    "Superseded by preferred duplicate source project "
                    f"({reason})."
                )
            project.generated_compilation_name = None
            project.generated_project_label = None

        deduped = [
            candidate.stock_clip_id
            for project in projects
            for candidate in project.candidates
            if candidate.rejection_reason == "duplicate_source_range"
        ]
        if demoted_ids:
            kept_segments = [
                segment
                for segment in report.segments
                if segment.stock_clip_id not in demoted_ids
            ]
            removed = len(report.segments) - len(kept_segments)
            report.segments = kept_segments
            report.segment_summary.written = max(0, report.segment_summary.written - removed)
            report.segment_summary.skipped += removed
            for key in (
                "superseded_project_family",
                "insufficient_grading",
                "duplicate_source_range",
            ):
                count = sum(
                    1
                    for project in projects
                    for candidate in project.candidates
                    if candidate.rejection_reason == key
                    and candidate.stock_clip_id in demoted_ids
                )
                if count:
                    report.segment_summary.skip_reasons[key] = (
                        report.segment_summary.skip_reasons.get(key, 0) + count
                    )
            report.skipped_segments += removed
            report.stock_projects_written = max(0, report.stock_projects_written - removed)
            report.warnings.append(
                f"{removed} clip(s) were withheld from review emission "
                "(insufficient grading, superseded duplicate projects, "
                "or duplicate source ranges)."
            )
            if deduped:
                report.warnings.append(
                    f"{len(deduped)} duplicate source-range clip(s) were collapsed "
                    "inside selected projects."
                )

        return [
            ProjectFamilyRecord(
                id=family.id,
                run_id=family.run_id,
                session_id=family.session_id,
                selected_source_project_id=family.selected_source_project_id,
                member_count=family.member_count,
                similarity=family.similarity_payload(),
            )
            for family in families
        ]

    def _finalize_project_names(
        self,
        projects: list[ProjectBuild],
        sessions: list[dict[str, Any]],
    ) -> None:
        assign_project_labels(projects)
        session_by_id = {str(session["id"]): session for session in sessions}
        for project in projects:
            if not project.accepted or not project.generated_project_label:
                continue
            project.generated_compilation_name = safe_filename(
                f"{project.generated_project_label} — Stock Compilation"
            )
            session = session_by_id.get(project.session_id or "")
            if session is not None and not session.get("generated_base_label"):
                session["generated_base_label"] = project.generated_project_label
            project.project_report.generated_event = project.generated_event_name
            project.project_report.generated_project_label = project.generated_project_label
            project.project_report.generated_compilation_name = (
                project.generated_compilation_name
            )
            project.project_report.session_id = project.session_id

            for candidate in project.candidates:
                if candidate.candidate_record is None:
                    continue
                clip_name = None
                if candidate.eligibility_status == "accepted" and candidate.clip_sequence:
                    clip_name = safe_filename(
                        f"{project.generated_project_label} — Clip "
                        f"{candidate.clip_sequence:02d}"
                    )
                candidate.candidate_record = replace(
                    candidate.candidate_record,
                    session_id=project.session_id,
                    generated_event_name=project.generated_event_name,
                    generated_project_label=project.generated_project_label,
                    generated_compilation_name=project.generated_compilation_name,
                    generated_clip_project_name=clip_name,
                    clip_sequence=candidate.clip_sequence,
                    expected_export_basename=clip_name,
                )
                if candidate.segment_report is not None:
                    candidate.segment_report.generated_event = project.generated_event_name
                    candidate.segment_report.generated_project_label = (
                        project.generated_project_label
                    )
                    candidate.segment_report.generated_compilation_name = (
                        project.generated_compilation_name
                    )
                    candidate.segment_report.generated_clip_project_name = clip_name
                    candidate.segment_report.session_id = project.session_id
                    candidate.segment_report.expected_export_basename = clip_name

    def _build_output_xml(
        self,
        *,
        source_root: ET.Element,
        project_builds: list[ProjectBuild],
        sessions: list[dict[str, Any]],
        options: StockifyOptions,
        run_id: str,
    ) -> tuple[ET.Element, dict[str, Any], list[GeneratedOccurrenceRecord]]:
        assert self.report is not None
        report = self.report
        output_root = ET.Element("fcpxml", {"version": source_root.get("version", "1.12")})
        output_resources_element = clone_resources(source_root)
        output_root.append(output_resources_element)
        output_resource_index = build_resource_index(output_resources_element)
        output_library = ET.SubElement(output_root, "library")

        events: dict[str, ET.Element] = {}
        for session in sessions:
            event_name = str(session["generated_event_name"])
            event = ET.SubElement(
                output_library,
                "event",
                {
                    "name": event_name,
                    "uid": stable_uid("generated-event", run_id, str(session["id"])),
                },
            )
            events[str(session["id"])] = event

        write_individual = options.layout in {"project-per-clip", "both"}
        write_compilation = (
            options.layout in {"timeline-batch", "both"}
            or options.include_compilations
        )
        occurrences: list[GeneratedOccurrenceRecord] = []
        manifest_groups: list[dict[str, Any]] = []

        for project in project_builds:
            if not project.accepted or not project.session_id:
                continue
            output_event = events[project.session_id]
            if not all(
                [
                    project.sequence_format,
                    project.tc_format,
                    project.audio_layout,
                    project.audio_rate,
                    project.generated_event_name,
                    project.generated_project_label,
                    project.generated_compilation_name,
                ]
            ):
                raise StockifyError(
                    f"Project {project.source_project_name!r} lacks generated output metadata."
                )

            timeline_offset = Fraction(0)
            compilation_clips: list[ET.Element] = []
            manifest_clips: list[dict[str, Any]] = []
            individual_projects: list[ET.Element] = []
            fps = int(project.format_info.get("timecode_fps") or 30)

            for candidate in project.accepted:
                assert candidate.clean_clip is not None
                assert candidate.segment_report is not None
                assert candidate.candidate_record is not None
                assert candidate.clip_sequence is not None
                clip_project_name = candidate.candidate_record.generated_clip_project_name
                assert clip_project_name is not None
                duration_fraction = parse_time(candidate.clean_clip.get("duration"))
                timeline_offset_text = format_time(timeline_offset)
                project_timecode = format_project_timecode(timeline_offset, fps)

                candidate.segment_report.timeline_offset = timeline_offset_text
                candidate.segment_report.project_timecode = project_timecode
                candidate.segment_report.timeline_project = (
                    project.generated_compilation_name if write_compilation else None
                )
                candidate.segment_report.output_project = (
                    clip_project_name if write_individual else project.generated_compilation_name
                )
                candidate.candidate_record = replace(
                    candidate.candidate_record,
                    compilation_timeline_offset=timeline_offset_text,
                    project_timecode=project_timecode,
                )

                common_metadata = {
                    "com.vclip.stockify_run_id": run_id,
                    "com.vclip.stock_clip_id": candidate.stock_clip_id,
                    "com.vclip.source_event": project.source_event_name,
                    "com.vclip.source_project": project.source_project_name,
                    "com.vclip.source_project_id": project.source_project_id,
                    "com.vclip.source_segment_index": str(candidate.segment_index),
                    "com.vclip.session_id": project.session_id,
                    "com.vclip.generated_event": project.generated_event_name,
                    "com.vclip.generated_project_label": project.generated_project_label,
                    "com.vclip.generated_compilation_name": project.generated_compilation_name,
                    "com.vclip.generated_clip_project_name": clip_project_name,
                    "com.vclip.clip_sequence": str(candidate.clip_sequence),
                    "com.vclip.candidate_tier": candidate.segment_report.candidate_tier,
                }

                if write_compilation:
                    compilation_clip = sanitize_review_clip_effects(
                        copy.deepcopy(candidate.clean_clip),
                        output_resource_index,
                    )
                    add_vclip_metadata(
                        compilation_clip,
                        {**common_metadata, "com.vclip.representation": "compilation"},
                    )
                    compilation_clips.append(compilation_clip)
                    occurrences.append(
                        GeneratedOccurrenceRecord(
                            run_id=run_id,
                            stock_clip_id=candidate.stock_clip_id,
                            representation="compilation",
                            generated_event_name=project.generated_event_name,
                            generated_project_name=project.generated_compilation_name,
                            project_uid=stable_uid(
                                "compilation",
                                run_id,
                                project.source_project_id,
                            ),
                            source_start=compilation_clip.get("start", "0s"),
                            duration=compilation_clip.get("duration", "0s"),
                            timeline_offset=timeline_offset_text,
                            effect_signature=video_treatment_signature(compilation_clip),
                        )
                    )

                if write_individual:
                    individual_clip = sanitize_review_clip_effects(
                        copy.deepcopy(candidate.clean_clip),
                        output_resource_index,
                    )
                    add_vclip_metadata(
                        individual_clip,
                        {**common_metadata, "com.vclip.representation": "individual"},
                    )
                    individual_uid = stable_uid(
                        "individual",
                        run_id,
                        candidate.stock_clip_id,
                    )
                    individual_projects.append(
                        make_project(
                            project_name=clip_project_name,
                            project_uid=individual_uid,
                            sequence_format=str(project.sequence_format),
                            sequence_tc_format=str(project.tc_format),
                            sequence_audio_layout=str(project.audio_layout),
                            sequence_audio_rate=str(project.audio_rate),
                            clips=[individual_clip],
                        )
                    )
                    occurrences.append(
                        GeneratedOccurrenceRecord(
                            run_id=run_id,
                            stock_clip_id=candidate.stock_clip_id,
                            representation="individual",
                            generated_event_name=project.generated_event_name,
                            generated_project_name=clip_project_name,
                            project_uid=individual_uid,
                            source_start=individual_clip.get("start", "0s"),
                            duration=individual_clip.get("duration", "0s"),
                            timeline_offset="0s",
                            effect_signature=video_treatment_signature(individual_clip),
                        )
                    )

                manifest_clips.append(
                    {
                        "sequence": candidate.clip_sequence,
                        "stock_clip_id": candidate.stock_clip_id,
                        "source_event": project.source_event_name,
                        "source_project": project.source_project_name,
                        "source_segment_index": candidate.segment_index,
                        "source_name": candidate.segment_report.source_name,
                        "source_ref": candidate.segment_report.source_ref,
                        "generated_event": project.generated_event_name,
                        "generated_project_label": project.generated_project_label,
                        "individual_project": clip_project_name,
                        "compilation_project": project.generated_compilation_name,
                        "expected_export_basename": clip_project_name,
                        "timeline_offset": timeline_offset_text,
                        "project_timecode": project_timecode,
                        "source_start": candidate.clean_clip.get("start", "0s"),
                        "source_duration": candidate.clean_clip.get("duration", "0s"),
                        "duration_seconds": format_seconds(duration_fraction),
                        "candidate_tier": candidate.segment_report.candidate_tier,
                        "location": candidate.segment_report.location,
                        "capture_time": candidate.segment_report.capture_time,
                        "time_of_day": candidate.segment_report.time_of_day,
                    }
                )
                timeline_offset += duration_fraction

            if write_compilation:
                compilation_uid = stable_uid(
                    "compilation",
                    run_id,
                    project.source_project_id,
                )
                compilation_project = make_project(
                    project_name=str(project.generated_compilation_name),
                    project_uid=compilation_uid,
                    sequence_format=str(project.sequence_format),
                    sequence_tc_format=str(project.tc_format),
                    sequence_audio_layout=str(project.audio_layout),
                    sequence_audio_rate=str(project.audio_rate),
                    clips=compilation_clips,
                )
                output_event.append(compilation_project)
                project.project_report.compilation_written = True
                report.compilation_projects_written += 1

            for individual_project in individual_projects:
                output_event.append(individual_project)

            prefix = commandpost_filename_prefix(str(project.generated_project_label))
            manifest_groups.append(
                {
                    "source_project_id": project.source_project_id,
                    "source_event": project.source_event_name,
                    "source_project": project.source_project_name,
                    "session_id": project.session_id,
                    "generated_event": project.generated_event_name,
                    "package_title": project.generated_project_label,
                    "compilation_project": project.generated_compilation_name,
                    "filename_prefix": prefix,
                    "suggested_commandpost_filename": (
                        f"{prefix}_{{projectTimecode}}_{{original}}"
                    ),
                    "clip_count": len(manifest_clips),
                    "total_duration": format_time(timeline_offset),
                    "total_duration_seconds": format_seconds(timeline_offset),
                    "format": project.format_info,
                    "clips": manifest_clips,
                }
            )

        if not events:
            report.warnings.append("No accepted stock candidates produced output events.")

        report.export_manifest_groups = len(manifest_groups)
        report.export_manifest_clips = sum(
            len(group["clips"]) for group in manifest_groups
        )
        # After clip-effect sanitization, drop unused effect resource declarations.
        prune_unreferenced_effect_resources(output_root)
        manifest = {
            "manifest_version": 2,
            "created_at": utc_now(),
            "stockify_run_id": run_id,
            "library_name": options.library_name,
            "layout": options.layout,
            "input_file": str(options.input_path),
            "output_fcpxml": str(options.output_path),
            "report_file": str(options.report_path),
            "database_file": str(options.database_path),
            "commandpost_filename_template": "{prefix}_{projectTimecode}_{original}",
            "groups": manifest_groups,
        }
        return output_root, manifest, occurrences

    def _build_snapshot(
        self,
        *,
        run_id: str,
        events: list[SourceEventRecord],
        sessions: list[dict[str, Any]],
        projects: list[ProjectBuild],
        media_records: dict[str, SourceMediaRecord],
        occurrences: list[GeneratedOccurrenceRecord],
        families: list[ProjectFamilyRecord] | None = None,
    ) -> StockifySnapshot:
        session_records = [self._session_record(session) for session in sessions]
        project_records: list[SourceProjectRecord] = []
        candidate_records: list[CandidateRecord] = []
        for project in projects:
            signature_json = None
            if project.timeline_signature is not None:
                signature_json = json_dumps(list(project.timeline_signature))
            project_records.append(
                SourceProjectRecord(
                    id=project.source_project_id,
                    run_id=run_id,
                    source_event_id=project.source_event_id,
                    source_index=project.source_project_index,
                    source_name=project.source_project_name,
                    source_uid=project.source_project_uid,
                    classification=project.project_report.classification,
                    session_id=project.session_id,
                    anchor_segment_index=project.project_report.anchor_segment_index,
                    generated_event_name=project.generated_event_name,
                    generated_project_label=project.generated_project_label,
                    generated_compilation_name=project.generated_compilation_name,
                    accepted_clip_count=len(project.accepted),
                    skipped_clip_count=project.project_report.segments_skipped,
                    sequence_format=project.sequence_format,
                    tc_format=project.tc_format,
                    audio_layout=project.audio_layout,
                    audio_rate=project.audio_rate,
                    source_mod_date=project.source_project.get("modDate"),
                    project_family_id=project.project_family_id,
                    family_role=project.family_role,
                    family_selection_reason=project.family_selection_reason,
                    grading_coverage=project.grading_coverage,
                    timeline_signature_json=signature_json,
                )
            )
            for candidate in project.candidates:
                if candidate.candidate_record is None:
                    raise StockifyError(
                        f"Candidate {candidate.stock_clip_id} has no database record."
                    )
                if candidate.candidate_record.session_id is None and project.session_id:
                    candidate.candidate_record = replace(
                        candidate.candidate_record,
                        session_id=project.session_id,
                        generated_event_name=project.generated_event_name,
                        generated_project_label=project.generated_project_label,
                        generated_compilation_name=project.generated_compilation_name,
                    )
                candidate_records.append(candidate.candidate_record)
        return StockifySnapshot(
            events=events,
            sessions=session_records,
            projects=project_records,
            media=list(media_records.values()),
            candidates=candidate_records,
            occurrences=occurrences,
            families=list(families or []),
        )

    @staticmethod
    def _session_record(session: dict[str, Any]) -> ShootSessionRecord:
        location = dict(session["location"])
        capture = dict(session["capture"])
        return ShootSessionRecord(
            id=str(session["id"]),
            run_id=str(session["run_id"]),
            session_key=str(session["session_key"]),
            capture_date=session.get("capture_date"),
            captured_at_local=session.get("captured_at_local"),
            timezone=session.get("timezone"),
            center_lat=StockifyService._optional_float(location.get("center_lat")),
            center_lon=StockifyService._optional_float(location.get("center_lon")),
            gps_radius_meters=StockifyService._optional_float(
                location.get("radius_meters")
            ),
            country=StockifyService._optional_str(location.get("country")),
            state=StockifyService._optional_str(location.get("state")),
            city=StockifyService._optional_str(location.get("city")),
            neighborhood=StockifyService._optional_str(location.get("neighborhood")),
            poi=StockifyService._optional_str(location.get("poi")),
            public_label=StockifyService._optional_str(location.get("public_label")),
            location_confidence=StockifyService._optional_str(location.get("confidence")),
            time_of_day=StockifyService._optional_str(session.get("time_of_day")),
            time_of_day_confidence=StockifyService._optional_str(
                session.get("time_of_day_confidence")
            ),
            generated_event_name=str(session["generated_event_name"]),
            generated_base_label=str(session["generated_base_label"]),
            anchor_stock_clip_id=StockifyService._optional_str(
                session.get("anchor_stock_clip_id")
            ),
            weather_status="not_enriched",
            astronomy_status="not_enriched",
            location=location,
            capture=capture,
        )

    def _srt_info_for_path(self, sidecar_path: str | None) -> SrtInfo | None:
        if not sidecar_path:
            return None
        path = Path(sidecar_path)
        if path in self.srt_cache:
            return self.srt_cache[path]
        try:
            info = parse_srt_info(path)
        except (OSError, ValueError) as exc:
            if self.report is not None:
                self.report.warnings.append(f"Could not parse SRT {path}: {exc}")
            return None
        self.srt_cache[path] = info
        return info

    def _resolve_location(
        self,
        *,
        srt_info: SrtInfo | None,
        start: Fraction,
        duration: Fraction,
        event_name: str,
        project_name: str,
        sidecar_path: str | None,
    ) -> dict[str, object]:
        def on_error(exc: Exception, latitude: float, longitude: float) -> None:
            if self.report is not None:
                self.report.warnings.append(
                    f"Location enrichment failed for {latitude:.5f}, "
                    f"{longitude:.5f}: {exc}"
                )

        return resolve_clip_location(
            srt_info=srt_info,
            start=start,
            duration=duration,
            event_name=event_name,
            project_name=project_name,
            sidecar_path=sidecar_path,
            location_resolver=as_place_resolver(
                self.location_resolver,
                on_error=on_error,
            ),
        )

    def _record_color_evaluation(
        self,
        camera_lut: str | None,
        has_custom_lut: bool,
    ) -> None:
        assert self.report is not None
        policy = self.report.color_policy
        policy.evaluated_segments += 1
        if camera_lut:
            policy.evaluated_with_camera_lut += 1
        else:
            policy.evaluated_without_camera_lut += 1
        if has_custom_lut:
            policy.evaluated_with_custom_lut += 1
        else:
            policy.evaluated_without_custom_lut += 1

    def _record_written_color(
        self,
        camera_lut: str | None,
        has_custom_lut: bool,
        creative_effects: list[str],
    ) -> None:
        assert self.report is not None
        policy = self.report.color_policy
        if camera_lut:
            policy.written_with_camera_lut += 1
        else:
            policy.written_without_camera_lut += 1
        if has_custom_lut:
            policy.written_with_custom_lut += 1
        else:
            policy.written_without_custom_lut += 1
        if creative_effects:
            policy.written_with_creative_video_effects += 1
            for effect in creative_effects:
                policy.creative_video_effects[effect] = (
                    policy.creative_video_effects.get(effect, 0) + 1
                )
        else:
            policy.written_without_creative_video_effects += 1

    def _record_recovery(self, original_duration: Fraction, recovery: Any, options: StockifyOptions) -> None:
        assert self.report is not None
        summary = self.report.candidates
        if float(original_duration) < options.short_clip_threshold_seconds:
            summary.short_segments_seen += 1
            if recovery.status in {"expanded", "expanded_review"}:
                summary.short_segments_expanded += 1
            else:
                summary.short_segments_unexpanded += 1
        if recovery.srt_status == "matched":
            summary.segments_with_srt += 1
        elif recovery.srt_status == "missing":
            summary.segments_missing_srt += 1
        if recovery.srt_window_status == "clean":
            summary.srt_clean_windows += 1
        elif recovery.srt_window_status == "review":
            summary.srt_review_windows += 1
        elif recovery.srt_window_status == "reject":
            summary.srt_reject_windows += 1
        if recovery.visual_status == "clean":
            summary.visual_clean_windows += 1
        elif recovery.visual_status == "review":
            summary.visual_review_windows += 1
        elif recovery.visual_status == "reject":
            summary.visual_reject_windows += 1
        elif recovery.visual_status == "unavailable":
            summary.visual_unavailable_windows += 1
        if recovery.status == "visual_unavailable_expansion":
            summary.visual_blocked_expansions += 1

    def _announce(self, message: str) -> None:
        if self.progress:
            self.progress(message)

    @staticmethod
    def _optional_int(value: object) -> int | None:
        return int(value) if isinstance(value, (int, float)) else None

    @staticmethod
    def _optional_float(value: object) -> float | None:
        return float(value) if isinstance(value, (int, float)) else None

    @staticmethod
    def _optional_str(value: object) -> str | None:
        return str(value) if value not in (None, "") else None
