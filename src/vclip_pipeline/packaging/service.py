"""Turn reviewed Final Cut exports into metadata-rich stock packages."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..db.repository import CatalogRepository
from ..errors import VClipError
from ..stockify.core import slugify
from ..stockify.naming import TIME_LABELS
from ..util import (
    ensure_empty_directory,
    safe_filename,
    sha256_file,
    stable_id,
    unique_preserving_order,
    utc_now,
)
from .astronomy import AstronomyRecord, astronomy_to_db, build_astronomy
from .matcher import ExportMatcher
from .media import MediaProbe, find_video_files, probe_media
from .weather import (
    NoWeatherProvider,
    OpenMeteoHistoricalWeatherProvider,
    WeatherProvider,
    WeatherRecord,
)


@dataclass
class PackageReport:
    stockify_run_id: str
    exports_directory: str
    output_directory: str
    video_files_found: int = 0
    exports_matched: int = 0
    unmatched_files: list[str] = field(default_factory=list)
    ambiguous_files: dict[str, list[str]] = field(default_factory=dict)
    missing_candidate_ids: list[str] = field(default_factory=list)
    duration_mismatches: list[dict[str, Any]] = field(default_factory=list)
    packages_created: int = 0
    package_paths: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class PackageService:
    """Match physical exports, enrich metadata, and build package directories."""

    def __init__(
        self,
        repository: CatalogRepository,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.repository = repository
        self.progress = progress
        self.matcher = ExportMatcher()

    def run(
        self,
        *,
        exports_directory: Path,
        output_directory: Path,
        run_id: str | None,
        project_labels: set[str] | None,
        mode: str,
        weather_provider: WeatherProvider | None,
        calculate_checksums: bool,
        inspect_media: bool,
        allow_unmatched: bool,
        allow_missing: bool,
        allow_duration_mismatch: bool,
        allow_unreconciled: bool,
        require_weather: bool,
        overwrite: bool,
        dry_run: bool,
        report_path: Path | None,
    ) -> PackageReport:
        if mode not in {"copy", "move", "hardlink", "symlink"}:
            raise VClipError(f"Unsupported package transfer mode: {mode}")
        if not exports_directory.is_dir():
            raise VClipError(f"Exports directory does not exist: {exports_directory}")

        resolved_run = self._resolve_run(run_id, allow_unreconciled)
        resolved_run_id = str(resolved_run["id"])
        candidates = self.repository.candidates_for_run(
            resolved_run_id,
            accepted_only=True,
            approved_only=not allow_unreconciled,
        )
        if allow_unreconciled:
            candidates = [
                candidate
                for candidate in candidates
                if candidate["review_status"] in {"pending", "approved"}
            ]
        if project_labels:
            candidates = [
                candidate
                for candidate in candidates
                if candidate.get("generated_project_label") in project_labels
            ]
        if not candidates:
            raise VClipError("No eligible candidates matched the requested package scope.")

        files = find_video_files(exports_directory)
        self._announce(f"Found {len(files)} exported video file(s).")
        match_result = self.matcher.match(files, candidates)
        report = PackageReport(
            stockify_run_id=resolved_run_id,
            exports_directory=str(exports_directory),
            output_directory=str(output_directory),
            video_files_found=len(files),
            exports_matched=len(match_result.matches),
            unmatched_files=[str(path) for path in match_result.unmatched_files],
            ambiguous_files=match_result.ambiguous_files,
            missing_candidate_ids=match_result.missing_candidate_ids,
        )

        if match_result.ambiguous_files:
            self._write_report(report_path, report)
            raise VClipError(
                f"{len(match_result.ambiguous_files)} exported file(s) matched multiple "
                "candidates. Rename them to their exact generated project names."
            )
        if match_result.unmatched_files and not allow_unmatched:
            self._write_report(report_path, report)
            raise VClipError(
                f"{len(match_result.unmatched_files)} video file(s) could not be matched. "
                "Use --allow-unmatched to ignore unrelated files."
            )
        if match_result.missing_candidate_ids and not allow_missing:
            self._write_report(report_path, report)
            raise VClipError(
                f"{len(match_result.missing_candidate_ids)} approved candidate(s) have no "
                "matching export. Export them or use --allow-missing for a partial package."
            )
        if not match_result.matches:
            self._write_report(report_path, report)
            raise VClipError("No exported files matched the database candidates.")

        candidate_by_id = {
            str(candidate["stock_clip_id"]): candidate for candidate in candidates
        }
        export_details: dict[str, dict[str, Any]] = {}
        for index, match in enumerate(match_result.matches, start=1):
            candidate = candidate_by_id[match.stock_clip_id]
            self._announce(
                f"Inspecting export {index}/{len(match_result.matches)}: {match.path.name}"
            )
            probe = probe_media(match.path) if inspect_media else MediaProbe(None, None, None, None, None)
            checksum = sha256_file(match.path) if calculate_checksums else None
            expected_duration = candidate.get("final_duration_seconds")
            if expected_duration is None:
                expected_duration = candidate.get("proposed_duration_seconds")
            if probe.duration_seconds is not None and expected_duration is not None:
                tolerance = max(0.5, float(expected_duration) * 0.05)
                if abs(probe.duration_seconds - float(expected_duration)) > tolerance:
                    mismatch = {
                        "file": str(match.path),
                        "stock_clip_id": match.stock_clip_id,
                        "exported_duration_seconds": probe.duration_seconds,
                        "reviewed_duration_seconds": float(expected_duration),
                        "tolerance_seconds": tolerance,
                    }
                    report.duration_mismatches.append(mismatch)
                    report.warnings.append(
                        f"{match.path.name}: exported duration {probe.duration_seconds:.3f}s "
                        f"differs from reviewed duration {float(expected_duration):.3f}s."
                    )
            export_id = stable_id(
                "EXPORT",
                resolved_run_id,
                match.stock_clip_id,
                str(match.path.resolve()),
            )
            detail = {
                "id": export_id,
                "stockify_run_id": resolved_run_id,
                "stock_clip_id": match.stock_clip_id,
                "exported_filename": match.path.name,
                "exported_path": str(match.path.resolve()),
                "match_method": match.method,
                "match_confidence": match.confidence,
                "file_size_bytes": match.path.stat().st_size,
                "duration_seconds": probe.duration_seconds,
                "sha256": checksum,
                "reconciled_at": utc_now(),
                "probe": asdict(probe),
                "path": match.path,
            }
            export_details[match.stock_clip_id] = detail

        if report.duration_mismatches and not allow_duration_mismatch:
            self._write_report(report_path, report)
            raise VClipError(
                f"{len(report.duration_mismatches)} exported file(s) differ materially "
                "from their reviewed durations. Export from the same Final Cut "
                "representation you reconciled, or use --allow-duration-mismatch "
                "after inspecting the report."
            )

        if not dry_run:
            for detail in export_details.values():
                self.repository.upsert_export(
                    {
                        key: value
                        for key, value in detail.items()
                        if key not in {"probe", "path"}
                    }
                )

        if match_result.missing_candidate_ids and not dry_run:
            self.repository.mark_missing_exports(
                resolved_run_id,
                match_result.missing_candidate_ids,
            )

        sessions = {
            str(session["id"]): session
            for session in self.repository.sessions_for_run(resolved_run_id)
        }
        projects = {
            str(project["id"]): project
            for project in self.repository.projects_for_run(resolved_run_id)
        }
        # Historical weather is part of the normal package flow; callers pass
        # NoWeatherProvider only for an explicit --weather none opt-out.
        provider = weather_provider or OpenMeteoHistoricalWeatherProvider()

        grouped: dict[str, list[dict[str, Any]]] = {}
        for match in match_result.matches:
            candidate = candidate_by_id[match.stock_clip_id]
            grouped.setdefault(str(candidate["source_project_id"]), []).append(candidate)

        projects_with_missing_exports = {
            str(candidate_by_id[clip_id]["source_project_id"])
            for clip_id in match_result.missing_candidate_ids
            if clip_id in candidate_by_id
        }

        folder_names = self._package_folder_names(grouped, sessions)
        if not dry_run:
            output_directory.mkdir(parents=True, exist_ok=True)
            existing_paths = [
                output_directory / folder_name
                for folder_name in folder_names.values()
                if (output_directory / folder_name).exists()
                or (output_directory / folder_name).is_symlink()
            ]
            if existing_paths and not overwrite:
                self._write_report(report_path, report)
                raise VClipError(
                    "Package output already exists: "
                    + ", ".join(str(path) for path in existing_paths)
                    + ". Use --overwrite to replace it."
                )

        for source_project_id, project_candidates in grouped.items():
            project_candidates.sort(key=lambda item: int(item.get("clip_sequence") or 0))
            project = projects[source_project_id]
            session_id = str(project.get("session_id") or "")
            session = sessions.get(session_id, {})
            weather = self._get_weather(session, provider, persist=not dry_run)
            if require_weather and weather.status != "enriched":
                raise VClipError(
                    f"Weather enrichment is required but unavailable for "
                    f"{project.get('generated_project_label')}."
                )
            astronomy = self._get_astronomy(
                session,
                weather,
                persist=not dry_run,
            )
            package_path = output_directory / folder_names[source_project_id]
            package_result = self._build_one_package(
                run_id=resolved_run_id,
                project=project,
                session=session,
                candidates=project_candidates,
                exports=export_details,
                package_path=package_path,
                weather=weather,
                astronomy=astronomy,
                mode=mode,
                overwrite=overwrite,
                dry_run=dry_run,
                partial=source_project_id in projects_with_missing_exports,
            )
            report.packages_created += 1
            report.package_paths.append(str(package_path))
            if not dry_run:
                self.repository.upsert_package(
                    package=package_result["package_db"],
                    clips=package_result["package_clips_db"],
                )

        self._write_report(report_path, report)
        return report

    def _resolve_run(self, run_id: str | None, allow_unreconciled: bool) -> dict[str, Any]:
        if run_id:
            run = self.repository.get_stockify_run(run_id)
            if run["status"] != "complete":
                raise VClipError(f"Stockify run {run_id} is not complete.")
            return run
        if allow_unreconciled:
            return self.repository.latest_stockify_run()
        return self.repository.latest_reconciled_stockify_run()

    def _get_weather(
        self,
        session: dict[str, Any],
        provider: WeatherProvider,
        *,
        persist: bool,
    ) -> WeatherRecord:
        if not session:
            return NoWeatherProvider().fetch({"id": "UNKNOWN"})
        cached = self.repository.weather_for_session(str(session["id"]), provider.name)
        if cached is not None:
            return WeatherRecord(
                id=str(cached["id"]),
                session_id=str(cached["session_id"]),
                provider=str(cached["provider"]),
                status=str(cached["status"]),
                requested_at=cached.get("requested_at"),
                observed_at=cached.get("observed_at"),
                timezone=cached.get("timezone"),
                condition_label=cached.get("condition_label"),
                temperature_c=cached.get("temperature_c"),
                precipitation_mm=cached.get("precipitation_mm"),
                rain_mm=cached.get("rain_mm"),
                cloud_cover_percent=cached.get("cloud_cover_percent"),
                visibility_meters=cached.get("visibility_meters"),
                wind_speed_kmh=cached.get("wind_speed_kmh"),
                weather_code=cached.get("weather_code"),
                grid_latitude=cached.get("grid_latitude"),
                grid_longitude=cached.get("grid_longitude"),
                source_latitude=cached.get("source_latitude"),
                source_longitude=cached.get("source_longitude"),
                fetched_at=str(cached["fetched_at"]),
                raw=cached.get("raw", {}),
            )
        self._announce(
            f"Fetching historical weather for {session.get('generated_event_name')}."
        )
        weather = provider.fetch(session)
        if provider.name != "none" and persist:
            self.repository.upsert_weather(asdict(weather))
        return weather

    def _get_astronomy(
        self,
        session: dict[str, Any],
        weather: WeatherRecord,
        *,
        persist: bool,
    ) -> AstronomyRecord:
        if not session:
            return build_astronomy({"id": "UNKNOWN"}, weather)
        # Always recompute so weather-adjusted concept signals stay current.
        # Factual sunrise/sunset are cheap/local; persist overwrites the cache.
        self._announce(
            f"Computing astronomical context for {session.get('generated_event_name')}."
        )
        astronomy = build_astronomy(session, weather)
        if persist and session.get("id"):
            self.repository.upsert_astronomy(astronomy_to_db(astronomy))
        return astronomy

    @staticmethod
    def _package_folder_names(
        grouped: dict[str, list[dict[str, Any]]],
        sessions: dict[str, dict[str, Any]],
    ) -> dict[str, str]:
        provisional: dict[str, str] = {}
        counts: dict[str, int] = {}
        for project_id, candidates in grouped.items():
            title = str(candidates[0].get("generated_project_label") or "Stock Footage")
            slug = slugify(title)
            provisional[project_id] = slug
            counts[slug] = counts.get(slug, 0) + 1
        for project_id, candidates in grouped.items():
            slug = provisional[project_id]
            if counts[slug] > 1:
                session = sessions.get(str(candidates[0].get("session_id") or ""), {})
                date = session.get("capture_date") or "unknown-date"
                provisional[project_id] = f"{slug}--{date}"
        collisions: dict[str, list[str]] = {}
        for project_id, name in provisional.items():
            collisions.setdefault(name, []).append(project_id)
        for name, project_ids in collisions.items():
            if len(project_ids) == 1:
                continue
            for index, project_id in enumerate(sorted(project_ids), start=1):
                provisional[project_id] = f"{name}--{index:02d}"
        return provisional

    def _build_one_package(
        self,
        *,
        run_id: str,
        project: dict[str, Any],
        session: dict[str, Any],
        candidates: list[dict[str, Any]],
        exports: dict[str, dict[str, Any]],
        package_path: Path,
        weather: WeatherRecord,
        astronomy: AstronomyRecord,
        mode: str,
        overwrite: bool,
        dry_run: bool,
        partial: bool,
    ) -> dict[str, Any]:
        title = str(project.get("generated_project_label") or "Stock Footage")
        package_id = stable_id("PACKAGE", run_id, project["id"])
        clips_path = package_path / "clips"
        clip_metadata_path = package_path / "metadata" / "clips"
        if not dry_run:
            ensure_empty_directory(package_path, overwrite=overwrite)
            clips_path.mkdir(parents=True, exist_ok=True)
            clip_metadata_path.mkdir(parents=True, exist_ok=True)

        public_clips: list[dict[str, Any]] = []
        internal_clips: list[dict[str, Any]] = []
        manifest_clips: list[dict[str, Any]] = []
        package_clips_db: list[dict[str, Any]] = []

        for candidate in candidates:
            clip_id = str(candidate["stock_clip_id"])
            export = exports.get(clip_id)
            if export is None:
                continue
            source_path: Path = export["path"]
            sequence = int(candidate.get("clip_sequence") or 0)
            output_name = safe_filename(
                str(candidate.get("expected_export_basename") or source_path.stem)
            ) + source_path.suffix.lower()
            destination = clips_path / output_name
            if not dry_run:
                self._transfer(source_path, destination, mode)

            duration = export.get("duration_seconds")
            if duration is None:
                duration = candidate.get("final_duration_seconds") or candidate.get(
                    "proposed_duration_seconds"
                )
            public_clip = {
                "stock_clip_id": clip_id,
                "sequence": sequence,
                "filename": output_name,
                "duration_seconds": duration,
                "width": export["probe"].get("width"),
                "height": export["probe"].get("height"),
                "codec": export["probe"].get("codec_name"),
            }
            public_clips.append(public_clip)
            internal_clip = {
                **public_clip,
                "source_event": candidate.get("source_event_name"),
                "source_project": candidate.get("source_project_name"),
                "source_segment_index": candidate.get("source_segment_index"),
                "source_filename": candidate.get("source_filename"),
                "source_media_path": candidate.get("source_media_path"),
                "source_srt_path": candidate.get("source_srt_path"),
                "proposed_start": candidate.get("proposed_start"),
                "proposed_duration": candidate.get("proposed_duration"),
                "final_start": candidate.get("final_start"),
                "final_duration": candidate.get("final_duration"),
                "manual_change": candidate.get("manual_change"),
                "location": candidate.get("location"),
                "capture_time": candidate.get("capture_time"),
                "candidate_tier": candidate.get("candidate_tier"),
                "srt_status": candidate.get("srt_status"),
                "srt_window_status": candidate.get("srt_window_status"),
                "visual_status": candidate.get("visual_status"),
                "creative_effects": candidate.get("creative_effects"),
                "camera_lut": candidate.get("camera_lut"),
            }
            internal_clips.append(internal_clip)
            manifest_clip = {
                **public_clip,
                "relative_path": str(Path("clips") / output_name),
                "size_bytes": export.get("file_size_bytes"),
                "sha256": export.get("sha256"),
                "match_method": export.get("match_method"),
                "match_confidence": export.get("match_confidence"),
            }
            manifest_clips.append(manifest_clip)
            package_clips_db.append(
                {
                    "stockify_run_id": run_id,
                    "stock_clip_id": clip_id,
                    "export_id": export["id"],
                    "sort_order": sequence,
                    "packaged_filename": output_name,
                    "packaged_path": str(destination),
                }
            )
            if not dry_run:
                (clip_metadata_path / f"{Path(output_name).stem}.json").write_text(
                    json.dumps(internal_clip, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

        location = {
            "country": session.get("country"),
            "state": session.get("state"),
            "city": session.get("city"),
            "neighborhood": session.get("neighborhood"),
            "poi": session.get("poi"),
            "public_label": session.get("public_label"),
        }
        description_location = session.get("public_label") or session.get("city") or "the shoot location"
        raw_time_label = session.get("time_of_day") or "unknown"
        time_label = TIME_LABELS.get(raw_time_label, str(raw_time_label).replace("_", " ").title())
        search_tags = unique_preserving_order(
            [
                str(value)
                for value in (
                    session.get("city"),
                    session.get("neighborhood"),
                    session.get("poi"),
                    time_label,
                    "drone",
                    "aerial",
                    *weather.search_tags(),
                )
                if value
            ]
        )
        public_metadata = {
            "package_id": package_id,
            "title": title,
            "slug": slugify(title),
            "description": f"Human-reviewed drone footage captured around {description_location}.",
            "location": location,
            "capture": {
                "date": session.get("capture_date"),
                "time_of_day": time_label,
                "timezone": session.get("timezone"),
            },
            "weather": weather.public_dict(),
            "astronomy": astronomy.public_dict(),
            "search_tags": search_tags,
            "clip_count": len(public_clips),
            "clips": public_clips,
        }
        internal_metadata = {
            "stockify_run_id": run_id,
            "source_project_id": project["id"],
            "source_event_name": project.get("source_event_name"),
            "source_project_name": project.get("source_name"),
            "session": session,
            "weather": {
                **weather.public_dict(),
                "source_latitude": weather.source_latitude,
                "source_longitude": weather.source_longitude,
            },
            "weather_raw": weather.raw,
            "astronomy": {
                **astronomy.public_dict(),
                "source_latitude": astronomy.source_latitude,
                "source_longitude": astronomy.source_longitude,
                "visual_analysis": astronomy.visual_analysis,
            },
            "astronomy_raw": astronomy.raw,
            "clips": internal_clips,
        }
        manifest = {
            "manifest_version": 1,
            "created_at": utc_now(),
            "package_id": package_id,
            "title": title,
            "status": "partial" if partial else "ready",
            "clips": manifest_clips,
        }
        if not dry_run:
            (package_path / "metadata.json").write_text(
                json.dumps(public_metadata, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            (package_path / "manifest.json").write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            (package_path / "vclip-internal.json").write_text(
                json.dumps(internal_metadata, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        return {
            "package_db": {
                "id": package_id,
                "stockify_run_id": run_id,
                "source_project_id": project["id"],
                "session_id": project.get("session_id"),
                "title": title,
                "slug": slugify(title),
                "output_path": str(package_path),
                "clip_count": len(public_clips),
                "status": "partial" if partial else "ready",
                "metadata": public_metadata,
            },
            "package_clips_db": package_clips_db,
        }

    @staticmethod
    def _transfer(source: Path, destination: Path, mode: str) -> None:
        if destination.exists() or destination.is_symlink():
            destination.unlink()
        if mode == "copy":
            shutil.copy2(source, destination)
        elif mode == "move":
            shutil.move(str(source), str(destination))
        elif mode == "hardlink":
            os.link(source, destination)
        elif mode == "symlink":
            destination.symlink_to(source.resolve())
        else:  # pragma: no cover - guarded by argument validation.
            raise AssertionError(mode)

    def _announce(self, message: str) -> None:
        if self.progress:
            self.progress(message)

    @staticmethod
    def _write_report(report_path: Path | None, report: PackageReport) -> None:
        if report_path is None:
            return
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(asdict(report), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
