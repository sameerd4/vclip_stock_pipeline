"""Bulk orchestration for post-shard exact duplicate removal."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..db.repository import CatalogRepository
from ..errors import VClipError
from .catalog import WorkflowCatalog
from .review_dedupe import (
    NEAR_CONTAINMENT_THRESHOLD,
    NEAR_IOU_THRESHOLD,
    ReviewDedupeService,
    format_text_report,
    near_source_range_duplicate,
    range_containment,
    range_iou,
)


@dataclass
class ShardBatchResult:
    relative_path: str
    status: str  # processed | failed | unchanged
    input_xml: str
    manifest_path: str | None
    output_xml: str | None
    stockify_run_id: str | None = None
    projects_before: int = 0
    projects_after: int = 0
    projects_considered: int = 0
    exact_clusters_found: int = 0
    exact_projects_removed: int = 0
    near_clusters_found: int = 0
    near_projects_removed: int = 0
    clusters_found: int = 0
    projects_removed: int = 0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    report_path: str | None = None
    text_report_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NearDuplicatePair:
    relative_path: str
    left_project_name: str
    right_project_name: str
    left_stock_clip_id: str
    right_stock_clip_id: str
    source_media: str
    containment: float
    iou: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReviewDedupeBatchReport:
    input_root: str
    manifest_root: str
    output_root: str
    dry_run: bool
    near_policy: str = "none"
    shards_discovered: int = 0
    shards_processed: int = 0
    shards_failed: int = 0
    unchanged_shards: int = 0
    changed_shards: int = 0
    projects_before: int = 0
    exact_duplicate_clusters: int = 0
    exact_projects_removed: int = 0
    near_duplicate_clusters: int = 0
    near_projects_removed: int = 0
    projects_removed: int = 0
    projects_after: int = 0
    percentage_reduction: float = 0.0
    thresholds: dict[str, float] = field(default_factory=dict)
    shards: list[ShardBatchResult] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    near_duplicate_audit: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_root": self.input_root,
            "manifest_root": self.manifest_root,
            "output_root": self.output_root,
            "dry_run": self.dry_run,
            "near_policy": self.near_policy,
            "thresholds": self.thresholds,
            "shards_discovered": self.shards_discovered,
            "shards_processed": self.shards_processed,
            "shards_failed": self.shards_failed,
            "unchanged_shards": self.unchanged_shards,
            "changed_shards": self.changed_shards,
            "projects_before": self.projects_before,
            "exact_duplicate_clusters": self.exact_duplicate_clusters,
            "exact_projects_removed": self.exact_projects_removed,
            "near_duplicate_clusters": self.near_duplicate_clusters,
            "near_projects_removed": self.near_projects_removed,
            "projects_removed": self.projects_removed,
            "projects_after": self.projects_after,
            "percentage_reduction": self.percentage_reduction,
            "shards": [item.as_dict() for item in self.shards],
            "failures": list(self.failures),
            "warnings": list(self.warnings),
            "near_duplicate_audit": self.near_duplicate_audit,
        }


@dataclass(frozen=True)
class _ShardJob:
    relative_path: str
    input_xml: Path
    manifest_path: Path


class ReviewDedupeBatchService:
    """Run exact review-dedupe across a portable shard corpus."""

    def __init__(
        self,
        repository: CatalogRepository,
        catalog: WorkflowCatalog | None = None,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.repository = repository
        self.catalog = catalog or WorkflowCatalog(repository.database)
        self.progress = progress
        self.dedupe = ReviewDedupeService(
            repository, self.catalog, progress=progress
        )

    def run(
        self,
        *,
        input_root: Path,
        manifest_root: Path,
        output_root: Path,
        report_path: Path,
        text_report_path: Path,
        dry_run: bool = False,
        overwrite: bool = False,
        near_policy: str = "none",
    ) -> ReviewDedupeBatchReport:
        input_root = input_root.expanduser().resolve()
        manifest_root = manifest_root.expanduser().resolve()
        output_root = output_root.expanduser().resolve()
        report_path = report_path.expanduser().resolve()
        text_report_path = text_report_path.expanduser().resolve()
        policy = (near_policy or "none").strip().casefold()

        if not input_root.is_dir():
            raise VClipError(f"Input root not found: {input_root}")
        if not manifest_root.is_dir():
            raise VClipError(f"Manifest root not found: {manifest_root}")

        jobs, discovery_failures = self.discover_shard_jobs(input_root, manifest_root)
        master = ReviewDedupeBatchReport(
            input_root=str(input_root),
            manifest_root=str(manifest_root),
            output_root=str(output_root),
            dry_run=dry_run,
            near_policy=policy,
            thresholds={
                "near_containment": NEAR_CONTAINMENT_THRESHOLD,
                "near_iou": NEAR_IOU_THRESHOLD,
            },
            shards_discovered=len(jobs) + len(discovery_failures),
        )
        for failure in discovery_failures:
            master.shards_failed += 1
            master.failures.append(failure)
            master.shards.append(
                ShardBatchResult(
                    relative_path=failure["relative_path"],
                    status="failed",
                    input_xml=failure.get("input_xml", ""),
                    manifest_path=failure.get("manifest_path"),
                    output_xml=None,
                    error=failure["error"],
                )
            )

        if not jobs and not discovery_failures:
            master.warnings.append("No review shard FCPXML files discovered.")
            self._write_master_reports(master, report_path, text_report_path)
            return master

        details_root = report_path.parent / f"{report_path.stem}-shards"
        details_root.mkdir(parents=True, exist_ok=True)
        if not dry_run:
            output_root.mkdir(parents=True, exist_ok=True)

        successful_outputs: list[tuple[str, Path, Path | None]] = []
        for index, job in enumerate(jobs, start=1):
            self._announce(f"[{index}/{len(jobs)}] {job.relative_path}")
            shard_report_path = details_root / f"{_safe_stem(job.relative_path)}.json"
            shard_text_path = details_root / f"{_safe_stem(job.relative_path)}.txt"
            output_xml = output_root / job.relative_path
            try:
                result = self.dedupe.run(
                    input_xml=job.input_xml,
                    output_xml=output_xml,
                    report_path=None,
                    text_report_path=None,
                    manifest_path=job.manifest_path,
                    dry_run=dry_run,
                    overwrite=overwrite,
                    update_source_manifest=False,
                    near_policy=policy,
                )
                shard_report_path.write_text(
                    json.dumps(result.as_dict(), indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                shard_text_path.write_text(format_text_report(result), encoding="utf-8")

                changed = result.projects_removed > 0
                shard = ShardBatchResult(
                    relative_path=job.relative_path,
                    status="unchanged" if not changed else "processed",
                    input_xml=str(job.input_xml),
                    manifest_path=str(job.manifest_path),
                    output_xml=None if dry_run else str(output_xml),
                    stockify_run_id=result.stockify_run_id,
                    projects_before=result.projects_before,
                    projects_after=result.projects_after,
                    projects_considered=result.projects_considered,
                    exact_clusters_found=result.exact_clusters_found,
                    exact_projects_removed=result.exact_projects_removed,
                    near_clusters_found=result.near_clusters_found,
                    near_projects_removed=result.near_projects_removed,
                    clusters_found=result.clusters_found,
                    projects_removed=result.projects_removed,
                    warnings=list(result.warnings),
                    report_path=str(shard_report_path),
                    text_report_path=str(shard_text_path),
                )
                master.shards.append(shard)
                master.shards_processed += 1
                master.projects_before += result.projects_before
                master.projects_after += result.projects_after
                master.projects_removed += result.projects_removed
                master.exact_duplicate_clusters += result.exact_clusters_found
                master.exact_projects_removed += result.exact_projects_removed
                master.near_duplicate_clusters += result.near_clusters_found
                master.near_projects_removed += result.near_projects_removed
                if changed:
                    master.changed_shards += 1
                else:
                    master.unchanged_shards += 1
                master.warnings.extend(
                    f"{job.relative_path}: {warning}" for warning in result.warnings
                )
                if not dry_run:
                    successful_outputs.append(
                        (job.relative_path, output_xml, job.manifest_path)
                    )
                else:
                    successful_outputs.append(
                        (job.relative_path, job.input_xml, job.manifest_path)
                    )
            except Exception as exc:  # noqa: BLE001 - isolate shard failures
                master.shards_failed += 1
                message = str(exc)
                master.failures.append(
                    {"relative_path": job.relative_path, "error": message}
                )
                master.shards.append(
                    ShardBatchResult(
                        relative_path=job.relative_path,
                        status="failed",
                        input_xml=str(job.input_xml),
                        manifest_path=str(job.manifest_path),
                        output_xml=None,
                        error=message,
                    )
                )
                self._announce(f"FAILED {job.relative_path}: {message}")

        if master.projects_before > 0:
            master.percentage_reduction = round(
                100.0 * master.projects_removed / master.projects_before, 3
            )
        else:
            master.percentage_reduction = 0.0

        master.near_duplicate_audit = self.audit_near_duplicates(
            successful_outputs,
            use_clean_tree=not dry_run,
            near_policy=policy,
        )
        self._write_master_reports(master, report_path, text_report_path)
        return master

    def discover_shard_jobs(
        self,
        input_root: Path,
        manifest_root: Path,
    ) -> tuple[list[_ShardJob], list[dict[str, str]]]:
        """Pair portable XML (preferred) with authoritative manifests by relative path."""
        relative_keys: set[str] = set()
        for path in sorted(input_root.rglob("*.fcpxml")):
            relative_keys.add(path.relative_to(input_root).as_posix())
        for path in sorted(manifest_root.rglob("*.fcpxml")):
            relative_keys.add(path.relative_to(manifest_root).as_posix())

        jobs: list[_ShardJob] = []
        failures: list[dict[str, str]] = []
        for relative in sorted(relative_keys):
            portable = input_root / relative
            authoritative = manifest_root / relative
            if portable.is_file():
                input_xml = portable
            elif authoritative.is_file():
                input_xml = authoritative
            else:
                continue
            manifest_path = self._manifest_for_relative(manifest_root, relative)
            if manifest_path is None:
                sibling = input_xml.with_name(f"{input_xml.stem}-shard-manifest.json")
                if sibling.is_file():
                    manifest_path = sibling
                else:
                    failures.append(
                        {
                            "relative_path": relative,
                            "input_xml": str(input_xml),
                            "error": (
                                f"No authoritative shard manifest found for {relative} "
                                f"under {manifest_root}"
                            ),
                        }
                    )
                    continue
            jobs.append(
                _ShardJob(
                    relative_path=relative,
                    input_xml=input_xml.resolve(),
                    manifest_path=manifest_path.resolve(),
                )
            )
        return jobs, failures

    @staticmethod
    def _manifest_for_relative(manifest_root: Path, relative: str) -> Path | None:
        xml_path = manifest_root / relative
        candidate = xml_path.with_name(f"{xml_path.stem}-shard-manifest.json")
        if candidate.is_file():
            return candidate
        # Also allow manifests keyed only by basename in the same directory.
        basename = Path(relative).name
        alt = manifest_root / Path(relative).parent / f"{Path(basename).stem}-shard-manifest.json"
        if alt.is_file():
            return alt
        return None

    def audit_near_duplicates(
        self,
        shards: list[tuple[str, Path, Path | None]],
        *,
        use_clean_tree: bool,
        near_policy: str = "none",
    ) -> dict[str, Any]:
        """Report remaining ultra-near pairs after configured removal policy."""
        pairs: list[NearDuplicatePair] = []
        read_errors: list[dict[str, str]] = []
        shards_with_near = 0
        audited = 0
        for relative, xml_path, manifest_path in shards:
            if not xml_path.is_file():
                continue
            audited += 1
            try:
                root = ET.parse(xml_path).getroot()
                manifest = None
                if manifest_path and Path(manifest_path).is_file():
                    payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
                    payload["path"] = Path(manifest_path)
                    manifest = payload
                if use_clean_tree:
                    cleaned_manifest = xml_path.with_name(
                        f"{xml_path.stem}-shard-manifest.json"
                    )
                    if cleaned_manifest.is_file():
                        payload = json.loads(cleaned_manifest.read_text(encoding="utf-8"))
                        payload["path"] = cleaned_manifest
                        manifest = payload
                scoped_ids = ReviewDedupeService._scoped_clip_ids(root, manifest)
                run_id = ReviewDedupeService._resolve_run_id(root, manifest)
                if run_id is None:
                    raise VClipError("Missing stockify_run_id for near-duplicate audit")
                candidates = {
                    str(row["stock_clip_id"]): row
                    for row in self.repository.candidates_for_run(
                        run_id, accepted_only=True
                    )
                    if not scoped_ids or str(row["stock_clip_id"]) in scoped_ids
                }
                projects = self.dedupe._read_dedupe_projects(
                    root, candidates, scoped_ids
                )
            except Exception as exc:  # noqa: BLE001
                read_errors.append({"relative_path": relative, "error": str(exc)})
                continue

            shard_pairs = 0
            for left_index, left in enumerate(projects):
                for right in projects[left_index + 1 :]:
                    if not near_source_range_duplicate(left, right):
                        continue
                    shard_pairs += 1
                    pairs.append(
                        NearDuplicatePair(
                            relative_path=relative,
                            left_project_name=left.project_name,
                            right_project_name=right.project_name,
                            left_stock_clip_id=left.stock_clip_id,
                            right_stock_clip_id=right.stock_clip_id,
                            source_media=left.media_identity,
                            containment=round(range_containment(left, right), 6),
                            iou=round(range_iou(left, right), 6),
                        )
                    )
            if shard_pairs:
                shards_with_near += 1

        if near_policy == "aggressive":
            note = (
                "Ultra-near pairs remaining after exact + aggressive near removal. "
                "Should usually be empty aside from edge cases."
            )
        else:
            note = (
                "Ultra-near pairs remaining after exact source-range dedupe. "
                "Not removed automatically under near-policy=none."
            )
        return {
            "mode": "report_only",
            "near_policy": near_policy,
            "thresholds": {
                "containment": NEAR_CONTAINMENT_THRESHOLD,
                "iou": NEAR_IOU_THRESHOLD,
            },
            "shards_audited": audited,
            "shards_with_near_duplicates": shards_with_near,
            "near_duplicate_pairs": len(pairs),
            "pairs": [item.as_dict() for item in pairs[:500]],
            "pairs_truncated": max(0, len(pairs) - 500),
            "read_errors": read_errors,
            "note": note,
        }

    def _write_master_reports(
        self,
        report: ReviewDedupeBatchReport,
        report_path: Path,
        text_report_path: Path,
    ) -> None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        text_report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report.as_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        text_report_path.write_text(format_batch_text_report(report), encoding="utf-8")

    def _announce(self, message: str) -> None:
        if self.progress:
            self.progress(message)


def _safe_stem(relative_path: str) -> str:
    return relative_path.replace("/", "__").replace("\\", "__")


def format_batch_text_report(report: ReviewDedupeBatchReport) -> str:
    lines = [
        "Bulk review duplicate removal",
        "=============================",
        f"Input root:         {report.input_root}",
        f"Manifest root:      {report.manifest_root}",
        f"Output root:        {report.output_root}",
        f"Near policy:        {report.near_policy}",
        f"Shards discovered:  {report.shards_discovered:>7}",
        f"Shards processed:   {report.shards_processed:>7}",
        f"Shards failed:      {report.shards_failed:>7}",
        f"Unchanged shards:   {report.unchanged_shards:>7}",
        f"Changed shards:     {report.changed_shards:>7}",
        f"Projects before:    {report.projects_before:>7}",
        f"Exact clusters:     {report.exact_duplicate_clusters:>7}",
        f"Exact removed:      {report.exact_projects_removed:>7}",
        f"Near clusters:      {report.near_duplicate_clusters:>7}",
        f"Near removed:       {report.near_projects_removed:>7}",
        f"Total removed:      {report.projects_removed:>7}",
        f"Projects after:     {report.projects_after:>7}",
        f"Reduction:          {report.percentage_reduction:>6.1f}%",
        (
            "Thresholds:         "
            f"containment>={NEAR_CONTAINMENT_THRESHOLD:.0%}  "
            f"IoU>={NEAR_IOU_THRESHOLD:.0%}"
        ),
        f"Dry run:            {str(report.dry_run).lower()}",
        "",
    ]
    near = report.near_duplicate_audit or {}
    lines.extend(
        [
            "Post-dedupe near-duplicate audit (report-only)",
            "---------------------------------------------",
            f"Shards audited:     {near.get('shards_audited', 0):>7}",
            f"Shards with near:   {near.get('shards_with_near_duplicates', 0):>7}",
            f"Near pairs left:    {near.get('near_duplicate_pairs', 0):>7}",
            "",
        ]
    )
    if report.failures:
        lines.append("Failures")
        lines.append("--------")
        for item in report.failures:
            lines.append(f"- {item['relative_path']}: {item['error']}")
        lines.append("")
    if report.warnings:
        lines.append("Warnings")
        lines.append("--------")
        for warning in report.warnings[:50]:
            lines.append(f"- {warning}")
        if len(report.warnings) > 50:
            lines.append(f"- ... {len(report.warnings) - 50} more")
        lines.append("")
    lines.append("Per-shard summary")
    lines.append("-----------------")
    for shard in report.shards:
        if shard.status == "failed":
            lines.append(f"- FAIL {shard.relative_path}: {shard.error}")
        else:
            lines.append(
                f"- {shard.status.upper():9} {shard.relative_path}  "
                f"before={shard.projects_before} "
                f"exact={shard.exact_projects_removed} "
                f"near={shard.near_projects_removed} "
                f"after={shard.projects_after}"
            )
    return "\n".join(lines) + "\n"
