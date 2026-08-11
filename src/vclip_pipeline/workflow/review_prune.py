"""Canonical review pruning for unusably short stock candidates."""

from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..db.repository import CatalogRepository
from ..errors import VClipError
from ..util import utc_now
from .catalog import WorkflowCatalog
from .review_dedupe import ReviewDedupeService

REASON = "short_stock_candidate"
DEFAULT_MIN_DURATION = 3.0


@dataclass
class PruneCandidate:
    """One individual candidate appearance in the canonical shard corpus."""

    stockify_run_id: str
    stock_clip_id: str
    relative_shard: str
    project_name: str
    event_name: str
    effective_duration_seconds: float
    duration_source: str
    candidate_tier: str | None
    short_clip_recovery: str | None
    input_xml: str
    row: dict[str, Any] = field(repr=False, compare=False)


@dataclass
class PruneRemoval:
    stockify_run_id: str
    removed_stock_clip_id: str
    removed_project_name: str
    reason: str
    effective_duration_seconds: float
    min_duration_seconds: float
    candidate_tier: str | None
    short_clip_recovery: str | None
    source_shard: str
    input_xml: str
    output_xml: str | None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReviewPruneReport:
    input_root: str
    output_root: str
    dry_run: bool
    min_duration_seconds: float
    candidates_before: int = 0
    candidates_removed: int = 0
    candidates_after: int = 0
    shards_discovered: int = 0
    shards_changed: int = 0
    shards_unchanged: int = 0
    shards_failed: int = 0
    removal_duration_distribution: dict[str, int] = field(default_factory=dict)
    candidate_tier_breakdown: dict[str, int] = field(default_factory=dict)
    recovery_reason_breakdown: dict[str, int] = field(default_factory=dict)
    removals: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    post_write_verification: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_root": self.input_root,
            "output_root": self.output_root,
            "dry_run": self.dry_run,
            "min_duration_seconds": self.min_duration_seconds,
            "reason": REASON,
            "candidates_before": self.candidates_before,
            "candidates_removed": self.candidates_removed,
            "candidates_after": self.candidates_after,
            "shards_discovered": self.shards_discovered,
            "shards_changed": self.shards_changed,
            "shards_unchanged": self.shards_unchanged,
            "shards_failed": self.shards_failed,
            "removal_duration_distribution": dict(self.removal_duration_distribution),
            "candidate_tier_breakdown": dict(self.candidate_tier_breakdown),
            "recovery_reason_breakdown": dict(self.recovery_reason_breakdown),
            "removals": list(self.removals),
            "failures": list(self.failures),
            "warnings": list(self.warnings),
            "post_write_verification": dict(self.post_write_verification),
        }


class ReviewPruneService:
    """Remove unusably short individual candidates from a canonical shard tree."""

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
        self._xml_writer = ReviewDedupeService(repository, self.catalog)

    def run(
        self,
        *,
        input_root: Path,
        output_root: Path,
        report_path: Path,
        text_report_path: Path,
        min_duration: float = DEFAULT_MIN_DURATION,
        dry_run: bool = False,
        overwrite: bool = False,
    ) -> ReviewPruneReport:
        if min_duration <= 0:
            raise VClipError("--min-duration must be greater than zero")
        input_root = input_root.expanduser().resolve()
        output_root = output_root.expanduser().resolve()
        report_path = report_path.expanduser().resolve()
        text_report_path = text_report_path.expanduser().resolve()
        if not input_root.is_dir():
            raise VClipError(f"Input root not found: {input_root}")
        if (
            output_root.exists()
            and any(output_root.iterdir())
            and not overwrite
            and not dry_run
        ):
            raise VClipError(
                f"Output root is not empty: {output_root} (pass --overwrite)"
            )

        self._announce(f"Scanning canonical shard corpus: {input_root}")
        shard_entries = self._discover_shards(input_root)
        candidates, warnings = self._load_candidates(shard_entries)
        removals = [
            self._to_removal(item, min_duration=min_duration, output_root=output_root)
            for item in candidates
            if item.effective_duration_seconds < min_duration
        ]
        removed_keys = {
            (item.stockify_run_id, item.removed_stock_clip_id) for item in removals
        }
        removed_by_shard: dict[str, set[str]] = {}
        removed_names_by_shard: dict[str, set[str]] = {}
        for item in removals:
            removed_by_shard.setdefault(item.source_shard, set()).add(
                item.removed_stock_clip_id
            )
            removed_names_by_shard.setdefault(item.source_shard, set()).add(
                item.removed_project_name
            )

        report = ReviewPruneReport(
            input_root=str(input_root),
            output_root=str(output_root),
            dry_run=dry_run,
            min_duration_seconds=min_duration,
            candidates_before=len(candidates),
            candidates_removed=len(removals),
            candidates_after=len(candidates) - len(removals),
            shards_discovered=len(shard_entries),
            shards_changed=len(removed_by_shard),
            shards_unchanged=max(0, len(shard_entries) - len(removed_by_shard)),
            removal_duration_distribution=_duration_distribution(
                [item.effective_duration_seconds for item in removals]
            ),
            candidate_tier_breakdown=dict(
                Counter(
                    (item.candidate_tier or "unknown")
                    for item in removals
                )
            ),
            recovery_reason_breakdown=dict(
                Counter(
                    (item.short_clip_recovery or "unknown")
                    for item in removals
                )
            ),
            removals=[_removal_dict(item) for item in removals],
            warnings=warnings,
        )

        survivors = [
            item
            for item in candidates
            if (item.stockify_run_id, item.stock_clip_id) not in removed_keys
        ]

        if dry_run:
            self._announce(
                f"Dry run: would remove {len(removals)} short candidate(s) "
                f"(<{min_duration:.3f}s)."
            )
            report.post_write_verification = self._verify_candidates(
                survivors, min_duration=min_duration
            )
        else:
            output_root.mkdir(parents=True, exist_ok=True)
            changed, unchanged, failures = self._write_corpus(
                input_root=input_root,
                output_root=output_root,
                shard_entries=shard_entries,
                removed_by_shard=removed_by_shard,
                removed_names_by_shard=removed_names_by_shard,
                min_duration=min_duration,
                overwrite=overwrite,
            )
            report.shards_changed = changed
            report.shards_unchanged = unchanged
            report.shards_failed = len(failures)
            report.failures.extend(failures)
            # Only persist removals for shards that wrote successfully.
            failed_shards = {item["relative_path"] for item in failures}
            persisted = [
                item for item in removals if item.source_shard not in failed_shards
            ]
            for item in persisted:
                item.output_xml = str(output_root / item.source_shard)
            self.catalog.record_review_short_prune_removals(removals=persisted)
            report.removals = [_removal_dict(item) for item in persisted]
            if failures:
                # Recount against successful writes only.
                report.candidates_removed = len(persisted)
                report.candidates_after = report.candidates_before - len(persisted)
            output_entries = self._discover_shards(output_root)
            output_candidates, audit_warnings = self._load_candidates(output_entries)
            report.warnings.extend(audit_warnings)
            report.post_write_verification = self._verify_candidates(
                output_candidates, min_duration=min_duration
            )

        self._write_reports(
            report, report_path=report_path, text_report_path=text_report_path
        )
        return report

    def _discover_shards(self, root: Path) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for xml_path in sorted(root.rglob("*.fcpxml")):
            relative = xml_path.relative_to(root).as_posix()
            manifest_path = xml_path.with_name(f"{xml_path.stem}-shard-manifest.json")
            if not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise VClipError(
                    f"Could not read shard manifest {manifest_path}: {exc}"
                ) from exc
            entries.append(
                {
                    "relative_xml": relative,
                    "xml_path": xml_path.resolve(),
                    "manifest_path": manifest_path.resolve(),
                    "manifest": manifest,
                }
            )
        return entries

    def _load_candidates(
        self,
        shard_entries: list[dict[str, Any]],
    ) -> tuple[list[PruneCandidate], list[str]]:
        warnings: list[str] = []
        appearances: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
        pairs: set[tuple[str, str]] = set()
        for entry in shard_entries:
            manifest = entry["manifest"]
            run_id = str(manifest.get("stockify_run_id") or "")
            seen_in_shard: set[tuple[str, str]] = set()
            for project in manifest.get("projects") or []:
                if project.get("representation") == "compilation":
                    continue
                if "Stock Compilation" in str(project.get("project_name") or ""):
                    continue
                for clip_id in project.get("stock_clip_ids") or []:
                    clip_id = str(clip_id)
                    if not run_id:
                        warnings.append(
                            f"{entry['relative_xml']}: missing stockify_run_id "
                            f"for {clip_id}"
                        )
                        continue
                    key = (run_id, clip_id)
                    if key in seen_in_shard:
                        continue
                    seen_in_shard.add(key)
                    pairs.add(key)
                    appearances.append((run_id, clip_id, entry, project))
            for clip_id in manifest.get("stock_clip_ids") or []:
                clip_id = str(clip_id)
                if not run_id:
                    continue
                key = (run_id, clip_id)
                if key in seen_in_shard:
                    continue
                seen_in_shard.add(key)
                pairs.add(key)
                appearances.append(
                    (
                        run_id,
                        clip_id,
                        entry,
                        {
                            "project_name": None,
                            "event_name": None,
                            "stock_clip_ids": [clip_id],
                            "representation": "individual",
                        },
                    )
                )

        rows = self.repository.candidates_by_run_and_ids(pairs)
        candidates: list[PruneCandidate] = []
        seen: set[tuple[str, str]] = set()
        for run_id, clip_id, entry, project in appearances:
            key = (run_id, clip_id)
            if key in seen:
                continue
            row = rows.get(key)
            if row is None:
                warnings.append(
                    f"{entry['relative_xml']}: missing accepted candidate "
                    f"({run_id}, {clip_id})"
                )
                continue
            duration, source = effective_duration_seconds(row)
            if duration is None:
                warnings.append(
                    f"{entry['relative_xml']}: no usable duration for "
                    f"({run_id}, {clip_id})"
                )
                continue
            candidates.append(
                PruneCandidate(
                    stockify_run_id=run_id,
                    stock_clip_id=clip_id,
                    relative_shard=entry["relative_xml"],
                    project_name=str(
                        project.get("project_name")
                        or row.get("generated_clip_project_name")
                        or clip_id
                    ),
                    event_name=str(
                        project.get("event_name")
                        or row.get("generated_event_name")
                        or ""
                    ),
                    effective_duration_seconds=duration,
                    duration_source=source,
                    candidate_tier=row.get("candidate_tier"),
                    short_clip_recovery=row.get("short_clip_recovery"),
                    input_xml=str(entry["xml_path"]),
                    row=row,
                )
            )
            seen.add(key)
        return candidates, warnings

    @staticmethod
    def _to_removal(
        candidate: PruneCandidate,
        *,
        min_duration: float,
        output_root: Path,
    ) -> PruneRemoval:
        return PruneRemoval(
            stockify_run_id=candidate.stockify_run_id,
            removed_stock_clip_id=candidate.stock_clip_id,
            removed_project_name=candidate.project_name,
            reason=REASON,
            effective_duration_seconds=candidate.effective_duration_seconds,
            min_duration_seconds=min_duration,
            candidate_tier=candidate.candidate_tier,
            short_clip_recovery=candidate.short_clip_recovery,
            source_shard=candidate.relative_shard,
            input_xml=candidate.input_xml,
            output_xml=str(output_root / candidate.relative_shard),
            provenance={
                "duration_source": candidate.duration_source,
                "event_name": candidate.event_name,
                "candidate_tier": candidate.candidate_tier,
                "short_clip_recovery": candidate.short_clip_recovery,
            },
        )

    def _write_corpus(
        self,
        *,
        input_root: Path,
        output_root: Path,
        shard_entries: list[dict[str, Any]],
        removed_by_shard: dict[str, set[str]],
        removed_names_by_shard: dict[str, set[str]],
        min_duration: float,
        overwrite: bool,
    ) -> tuple[int, int, list[dict[str, str]]]:
        del input_root  # path identity comes from shard entries
        changed = 0
        unchanged = 0
        failures: list[dict[str, str]] = []
        for entry in shard_entries:
            relative = entry["relative_xml"]
            output_xml = output_root / relative
            try:
                if output_xml.exists() and not overwrite:
                    raise VClipError(f"Output exists: {output_xml}")
                output_xml.parent.mkdir(parents=True, exist_ok=True)
                removed_ids = removed_by_shard.get(relative, set())
                removed_names = removed_names_by_shard.get(relative, set())
                if not removed_ids:
                    shutil.copy2(entry["xml_path"], output_xml)
                    shutil.copy2(
                        entry["manifest_path"],
                        output_xml.with_name(f"{output_xml.stem}-shard-manifest.json"),
                    )
                    unchanged += 1
                    continue
                tree = ET.parse(entry["xml_path"])
                root = tree.getroot()
                self._xml_writer._write_output(root, output_xml, removed_names)
                self._rewrite_output_manifest(
                    manifest=entry["manifest"],
                    output_xml=output_xml,
                    removed_ids=removed_ids,
                    removed_names=removed_names,
                    min_duration=min_duration,
                )
                changed += 1
            except Exception as exc:  # noqa: BLE001
                failures.append({"relative_path": relative, "error": str(exc)})
                self._announce(f"FAILED {relative}: {exc}")
        return changed, unchanged, failures

    @staticmethod
    def _rewrite_output_manifest(
        *,
        manifest: dict[str, Any],
        output_xml: Path,
        removed_ids: set[str],
        removed_names: set[str],
        min_duration: float | None,
    ) -> None:
        payload = {key: value for key, value in manifest.items() if key != "path"}
        payload["stock_clip_ids"] = [
            clip_id
            for clip_id in payload.get("stock_clip_ids", [])
            if clip_id not in removed_ids
        ]
        payload["projects"] = [
            project
            for project in payload.get("projects", [])
            if project.get("project_name") not in removed_names
            and not removed_ids.intersection(project.get("stock_clip_ids") or [])
        ]
        payload["project_count"] = len(payload.get("projects") or [])
        payload["short_prune"] = {
            "reason": REASON,
            "removed_stock_clip_ids": sorted(removed_ids),
            "removed_project_names": sorted(removed_names),
            "output_fcpxml": str(output_xml),
            "min_duration_seconds": min_duration,
        }
        target = output_xml.with_name(f"{output_xml.stem}-shard-manifest.json")
        target.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _verify_candidates(
        self,
        candidates: list[PruneCandidate],
        *,
        min_duration: float,
    ) -> dict[str, Any]:
        remaining = [
            item
            for item in candidates
            if item.effective_duration_seconds < min_duration
        ]
        return {
            "mode": "short_prune_post_audit",
            "min_duration_seconds": min_duration,
            "candidates_audited": len(candidates),
            "remaining_short_candidates": len(remaining),
            "remaining_stock_clip_ids": sorted(
                {
                    f"{item.stockify_run_id}:{item.stock_clip_id}"
                    for item in remaining
                }
            ),
        }

    def _write_reports(
        self,
        report: ReviewPruneReport,
        *,
        report_path: Path,
        text_report_path: Path,
    ) -> None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        text_report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report.as_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        text_report_path.write_text(format_prune_text_report(report), encoding="utf-8")

    def _announce(self, message: str) -> None:
        if self.progress:
            self.progress(message)


def effective_duration_seconds(row: dict[str, Any]) -> tuple[float | None, str]:
    """Resolve effective duration with final → proposed → original precedence."""
    for key, label in (
        ("final_duration_seconds", "final_duration_seconds"),
        ("proposed_duration_seconds", "proposed_duration_seconds"),
        ("original_duration_seconds", "original_duration_seconds"),
    ):
        value = row.get(key)
        if value is None:
            continue
        try:
            return float(value), label
        except (TypeError, ValueError):
            continue
    return None, "missing"


def _duration_distribution(durations: list[float]) -> dict[str, int]:
    buckets = {
        "<1.0s": 0,
        "1.0-2.0s": 0,
        "2.0-3.0s": 0,
        ">=3.0s": 0,
    }
    for value in durations:
        if value < 1.0:
            buckets["<1.0s"] += 1
        elif value < 2.0:
            buckets["1.0-2.0s"] += 1
        elif value < 3.0:
            buckets["2.0-3.0s"] += 1
        else:
            buckets[">=3.0s"] += 1
    return {key: count for key, count in buckets.items() if count}


def _removal_dict(item: PruneRemoval) -> dict[str, Any]:
    payload = asdict(item)
    payload["created_at"] = utc_now()
    return payload


def format_prune_text_report(report: ReviewPruneReport) -> str:
    audit = report.post_write_verification or {}
    lines = [
        "Canonical short-candidate prune",
        "===============================",
        f"Input root:              {report.input_root}",
        f"Output root:             {report.output_root}",
        f"Min duration:            {report.min_duration_seconds:.3f}s",
        f"Candidates before:       {report.candidates_before:>7}",
        f"Candidates removed:      {report.candidates_removed:>7}",
        f"Candidates after:        {report.candidates_after:>7}",
        f"Shards discovered:       {report.shards_discovered:>7}",
        f"Shards changed:          {report.shards_changed:>7}",
        f"Shards unchanged:        {report.shards_unchanged:>7}",
        f"Shards failed:           {report.shards_failed:>7}",
        f"Dry run:                 {str(report.dry_run).lower()}",
        "",
        "Removal duration distribution",
        "-----------------------------",
    ]
    if report.removal_duration_distribution:
        for bucket, count in report.removal_duration_distribution.items():
            lines.append(f"  {bucket:<12} {count:>5}")
    else:
        lines.append("  (none)")
    lines.extend(
        [
            "",
            "Candidate tier breakdown",
            "------------------------",
        ]
    )
    if report.candidate_tier_breakdown:
        for tier, count in sorted(report.candidate_tier_breakdown.items()):
            lines.append(f"  {tier:<32} {count:>5}")
    else:
        lines.append("  (none)")
    lines.extend(
        [
            "",
            "Recovery reason breakdown",
            "-------------------------",
        ]
    )
    if report.recovery_reason_breakdown:
        for reason, count in sorted(report.recovery_reason_breakdown.items()):
            lines.append(f"  {reason:<32} {count:>5}")
    else:
        lines.append("  (none)")
    lines.extend(
        [
            "",
            "Post-write verification",
            "-----------------------",
            f"Remaining short candidates: "
            f"{audit.get('remaining_short_candidates', 0):>5}",
            "",
        ]
    )
    if report.failures:
        lines.append("Failures")
        lines.append("--------")
        for failure in report.failures:
            lines.append(f"  {failure.get('relative_path')}: {failure.get('error')}")
        lines.append("")
    if report.warnings:
        lines.append("Warnings")
        lines.append("--------")
        for warning in report.warnings[:40]:
            lines.append(f"  {warning}")
        if len(report.warnings) > 40:
            lines.append(f"  … {len(report.warnings) - 40} more")
        lines.append("")
    return "\n".join(lines) + "\n"
