"""Match final Final Cut exports to reconciled candidates without packaging them."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..db.repository import CatalogRepository
from ..errors import VClipError
from ..packaging.matcher import ExportMatcher
from ..packaging.media import MediaProbe, find_video_files, probe_media
from ..util import export_stable_id, sha256_file, utc_now
from .catalog import WorkflowCatalog


@dataclass
class ExportIngestReport:
    stockify_run_id: str
    exports_directory: str
    video_files_found: int = 0
    exports_matched: int = 0
    exports_persisted: int = 0
    unmatched_files: list[str] = field(default_factory=list)
    ambiguous_files: dict[str, list[str]] = field(default_factory=dict)
    missing_candidate_ids: list[str] = field(default_factory=list)
    duration_mismatches: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class ExportIngestService:
    """Persist final physical exports as the boundary before enrichment."""

    def __init__(
        self,
        repository: CatalogRepository,
        workflow_catalog: WorkflowCatalog,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.repository = repository
        self.workflow_catalog = workflow_catalog
        self.progress = progress
        self.matcher = ExportMatcher()

    def run(
        self,
        *,
        exports_directory: Path,
        run_id: str | None,
        project_labels: set[str] | None = None,
        calculate_checksums: bool = True,
        inspect_media: bool = True,
        allow_unmatched: bool = False,
        allow_missing: bool = False,
        allow_duration_mismatch: bool = False,
        allow_unreconciled: bool = False,
        dry_run: bool = False,
        report_path: Path | None = None,
    ) -> ExportIngestReport:
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
            raise VClipError("No eligible candidates matched the requested export scope.")

        files = find_video_files(exports_directory)
        self._announce(f"Found {len(files)} exported video file(s).")
        match_result = self.matcher.match(files, candidates)
        report = ExportIngestReport(
            stockify_run_id=resolved_run_id,
            exports_directory=str(exports_directory),
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
                "candidates. Rename them to exact generated project names."
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
                f"{len(match_result.missing_candidate_ids)} eligible candidate(s) have no "
                "matching export. Export them or use --allow-missing for partial ingest."
            )
        if not match_result.matches:
            self._write_report(report_path, report)
            raise VClipError("No exported files matched database candidates.")

        candidate_by_id = {
            str(candidate["stock_clip_id"]): candidate for candidate in candidates
        }
        details: list[tuple[dict[str, Any], MediaProbe]] = []
        for index, match in enumerate(match_result.matches, start=1):
            candidate = candidate_by_id[match.stock_clip_id]
            self._announce(
                f"Inspecting export {index}/{len(match_result.matches)}: {match.path.name}"
            )
            probe = (
                probe_media(match.path)
                if inspect_media
                else MediaProbe(None, None, None, None, None)
            )
            checksum = sha256_file(match.path) if calculate_checksums else None
            expected_duration = candidate.get("final_duration_seconds")
            if expected_duration is None:
                expected_duration = candidate.get("proposed_duration_seconds")
            if probe.duration_seconds is not None and expected_duration is not None:
                tolerance = max(0.5, float(expected_duration) * 0.05)
                delta = abs(probe.duration_seconds - float(expected_duration))
                if delta > tolerance:
                    mismatch = {
                        "file": str(match.path),
                        "stock_clip_id": match.stock_clip_id,
                        "exported_duration_seconds": probe.duration_seconds,
                        "reviewed_duration_seconds": float(expected_duration),
                        "difference_seconds": delta,
                        "tolerance_seconds": tolerance,
                    }
                    report.duration_mismatches.append(mismatch)
                    report.warnings.append(
                        f"{match.path.name}: exported duration {probe.duration_seconds:.3f}s "
                        f"differs from reviewed duration {float(expected_duration):.3f}s."
                    )
            export_id = export_stable_id(resolved_run_id, match.stock_clip_id)
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
            }
            details.append((detail, probe))

        if report.duration_mismatches and not allow_duration_mismatch:
            self._write_report(report_path, report)
            raise VClipError(
                f"{len(report.duration_mismatches)} exported file(s) differ materially "
                "from reviewed durations. Inspect the report or use "
                "--allow-duration-mismatch after verifying the files."
            )

        if not dry_run:
            for detail, probe in details:
                stored = self.repository.upsert_export(detail)
                detail["id"] = stored["id"]
                detail["exported_path"] = stored["exported_path"]
                self.workflow_catalog.upsert_export_media(
                    export_id=str(stored["id"]),
                    width=probe.width,
                    height=probe.height,
                    codec_name=probe.codec_name,
                    frame_rate=probe.frame_rate,
                    probe=asdict(probe),
                )
                report.exports_persisted += 1
            if match_result.missing_candidate_ids:
                self.repository.mark_missing_exports(
                    resolved_run_id,
                    match_result.missing_candidate_ids,
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

    def _announce(self, message: str) -> None:
        if self.progress:
            self.progress(message)

    @staticmethod
    def _write_report(path: Path | None, report: ExportIngestReport) -> None:
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(report), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
