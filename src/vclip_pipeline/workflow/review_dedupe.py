"""Post-shard exact source-range duplicate removal for review FCPXML."""

from __future__ import annotations

import copy
import json
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..db.repository import CatalogRepository
from ..errors import VClipError
from ..stockify.core import parse_time
from ..stockify.fcpxml import (
    first_direct_child,
    local_name,
    read_vclip_metadata,
    validate_fcpxml,
)
from .catalog import WorkflowCatalog

EXACT_RANGE_TOLERANCE_SECONDS = 0.05
NEAR_CONTAINMENT_THRESHOLD = 0.95
NEAR_IOU_THRESHOLD = 0.92
REASON = "exact_source_range_duplicate"
NEAR_REASON = "near_duplicate_source_range"
NEAR_POLICIES = frozenset({"none", "aggressive"})


@dataclass(frozen=True)
class DedupeProject:
    order: int
    project_name: str
    project_uid: str | None
    event_name: str
    stock_clip_id: str
    stockify_run_id: str
    source_project_id: str | None
    representation: str
    media_identity: str
    source_start_seconds: float
    source_duration_seconds: float
    source_start: str
    source_duration: str
    short_clip_recovery: str | None
    element: ET.Element = field(repr=False, compare=False)


@dataclass
class DedupeRemoval:
    removed_project_name: str
    kept_project_name: str
    removed_stock_clip_id: str
    kept_stock_clip_id: str
    source_media: str
    source_start: str
    source_duration: str
    reason: str = REASON
    removed_short_clip_recovery: str | None = None
    kept_short_clip_recovery: str | None = None
    cluster_size: int = 0
    containment: float | None = None
    iou: float | None = None


@dataclass
class ReviewDedupeReport:
    input_fcpxml: str
    output_fcpxml: str | None
    stockify_run_id: str | None
    manifest_path: str | None
    scoped_stock_clip_ids: int
    projects_considered: int
    clusters_found: int
    projects_removed: int
    projects_kept: int
    dry_run: bool
    projects_before: int = 0
    projects_after: int = 0
    near_policy: str = "none"
    exact_clusters_found: int = 0
    exact_projects_removed: int = 0
    near_clusters_found: int = 0
    near_projects_removed: int = 0
    removals: list[DedupeRemoval] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_fcpxml": self.input_fcpxml,
            "output_fcpxml": self.output_fcpxml,
            "stockify_run_id": self.stockify_run_id,
            "manifest_path": self.manifest_path,
            "scoped_stock_clip_ids": self.scoped_stock_clip_ids,
            "near_policy": self.near_policy,
            "projects_before": self.projects_before,
            "projects_considered": self.projects_considered,
            "exact_clusters_found": self.exact_clusters_found,
            "exact_projects_removed": self.exact_projects_removed,
            "near_clusters_found": self.near_clusters_found,
            "near_projects_removed": self.near_projects_removed,
            "clusters_found": self.clusters_found,
            "projects_removed": self.projects_removed,
            "projects_kept": self.projects_kept,
            "projects_after": self.projects_after,
            "thresholds": {
                "exact_tolerance_seconds": EXACT_RANGE_TOLERANCE_SECONDS,
                "near_containment": NEAR_CONTAINMENT_THRESHOLD,
                "near_iou": NEAR_IOU_THRESHOLD,
            },
            "dry_run": self.dry_run,
            "removals": [asdict(item) for item in self.removals],
            "warnings": list(self.warnings),
        }


class ReviewDedupeService:
    """Remove exact source-range duplicate projects from a review shard FCPXML."""

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

    def run(
        self,
        *,
        input_xml: Path,
        output_xml: Path,
        report_path: Path | None = None,
        text_report_path: Path | None = None,
        manifest_path: Path | None = None,
        dry_run: bool = False,
        overwrite: bool = False,
        update_source_manifest: bool = True,
        near_policy: str = "none",
    ) -> ReviewDedupeReport:
        policy = (near_policy or "none").strip().casefold()
        if policy not in NEAR_POLICIES:
            raise VClipError(
                f"Unsupported near-policy {near_policy!r}; "
                f"expected one of {sorted(NEAR_POLICIES)}"
            )

        input_xml = input_xml.expanduser().resolve()
        output_xml = output_xml.expanduser().resolve()
        if not input_xml.is_file():
            raise VClipError(f"Review FCPXML not found: {input_xml}")
        if output_xml.exists() and not overwrite and not dry_run:
            raise VClipError(
                f"Output already exists: {output_xml} (pass --overwrite to replace)"
            )

        self._announce(f"Reading review shard: {input_xml.name}")
        tree = ET.parse(input_xml)
        root = tree.getroot()
        projects_before = len(root.findall("./library/event/project"))
        manifest = self._load_manifest(input_xml, manifest_path)
        scoped_ids = self._scoped_clip_ids(root, manifest)
        run_id = self._resolve_run_id(root, manifest)
        if run_id is None:
            raise VClipError(
                "Could not determine stockify_run_id from shard metadata or manifest."
            )

        candidates = {
            str(row["stock_clip_id"]): row
            for row in self.repository.candidates_for_run(run_id, accepted_only=True)
            if not scoped_ids or str(row["stock_clip_id"]) in scoped_ids
        }
        projects = self._read_dedupe_projects(root, candidates, scoped_ids)

        exact_clusters = self._cluster_by_predicate(
            projects, exact_source_range_duplicate
        )
        exact_removals = self._choose_exact_removals(exact_clusters)
        exact_representatives = {
            kept.stock_clip_id
            for cluster in exact_clusters
            for kept in [min(cluster, key=_representative_sort_key)]
        }
        exact_removed_ids = {item.removed_stock_clip_id for item in exact_removals}
        survivors = [
            project
            for project in projects
            if project.stock_clip_id not in exact_removed_ids
        ]

        near_clusters: list[list[DedupeProject]] = []
        near_removals: list[DedupeRemoval] = []
        if policy == "aggressive":
            near_clusters = self._cluster_by_predicate(
                survivors, aggressive_near_match
            )
            near_removals = self._choose_near_removals(
                near_clusters,
                exact_representatives=exact_representatives,
            )

        removals = [*exact_removals, *near_removals]
        total_removed = len(removals)

        report = ReviewDedupeReport(
            input_fcpxml=str(input_xml),
            output_fcpxml=None if dry_run else str(output_xml),
            stockify_run_id=run_id,
            manifest_path=str(manifest["path"]) if manifest else None,
            scoped_stock_clip_ids=len(scoped_ids) if scoped_ids else len(candidates),
            projects_considered=len(projects),
            clusters_found=len(exact_clusters) + len(near_clusters),
            projects_removed=total_removed,
            projects_kept=len(projects) - total_removed,
            dry_run=dry_run,
            projects_before=projects_before,
            projects_after=projects_before - total_removed,
            near_policy=policy,
            exact_clusters_found=len(exact_clusters),
            exact_projects_removed=len(exact_removals),
            near_clusters_found=len(near_clusters),
            near_projects_removed=len(near_removals),
            removals=removals,
        )
        if scoped_ids:
            missing = scoped_ids - set(candidates)
            if missing:
                report.warnings.append(
                    f"{len(missing)} manifest stock_clip_id(s) were not found as "
                    "accepted candidates in the database."
                )

        if dry_run:
            self._announce(
                f"Dry run: would remove {len(exact_removals)} exact and "
                f"{len(near_removals)} near duplicate project(s)."
            )
        else:
            removed_names = {item.removed_project_name for item in removals}
            self._write_output(root, output_xml, removed_names)
            self.catalog.record_review_dedupe_removals(
                stockify_run_id=run_id,
                removals=removals,
                source_fcpxml=str(input_xml),
                output_fcpxml=str(output_xml),
            )
            if manifest:
                self._update_manifest(
                    manifest,
                    removals,
                    output_xml,
                    update_source_manifest=update_source_manifest,
                )
            self._announce(
                f"Removed {total_removed} duplicate project(s) "
                f"(exact={len(exact_removals)}, near={len(near_removals)}); "
                f"wrote {output_xml.name}."
            )

        if report_path is not None:
            report_path = report_path.expanduser().resolve()
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(report.as_dict(), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        if text_report_path is not None:
            text_report_path = text_report_path.expanduser().resolve()
            text_report_path.parent.mkdir(parents=True, exist_ok=True)
            text_report_path.write_text(format_text_report(report), encoding="utf-8")
        return report

    def _load_manifest(
        self,
        input_xml: Path,
        manifest_path: Path | None,
    ) -> dict[str, Any] | None:
        candidates = []
        if manifest_path is not None:
            candidates.append(manifest_path.expanduser().resolve())
        candidates.append(
            input_xml.with_name(f"{input_xml.stem}-shard-manifest.json")
        )
        for path in candidates:
            if path.is_file():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise VClipError(f"Could not read shard manifest {path}: {exc}") from exc
                payload = dict(payload)
                payload["path"] = path
                return payload
        return None

    @staticmethod
    def _scoped_clip_ids(
        root: ET.Element,
        manifest: dict[str, Any] | None,
    ) -> set[str]:
        if manifest and manifest.get("stock_clip_ids"):
            return {str(value) for value in manifest["stock_clip_ids"]}
        ids: set[str] = set()
        for project in root.findall("./library/event/project"):
            for clip_id in ReviewDedupeService._project_clip_ids(project):
                ids.add(clip_id)
        return ids

    @staticmethod
    def _resolve_run_id(
        root: ET.Element,
        manifest: dict[str, Any] | None,
    ) -> str | None:
        if manifest and manifest.get("stockify_run_id"):
            return str(manifest["stockify_run_id"])
        for project in root.findall("./library/event/project"):
            for metadata in ReviewDedupeService._project_clip_metadata(project):
                run_id = metadata.get("com.vclip.stockify_run_id")
                if run_id:
                    return run_id
        return None

    def _read_dedupe_projects(
        self,
        root: ET.Element,
        candidates: dict[str, dict[str, Any]],
        scoped_ids: set[str],
    ) -> list[DedupeProject]:
        projects: list[DedupeProject] = []
        order = 0
        for event in root.findall("./library/event"):
            event_name = event.get("name", "")
            for project in list(event.findall("project")):
                name = project.get("name", "")
                if "Stock Compilation" in name:
                    continue
                clip_ids = self._project_clip_ids(project)
                if not clip_ids:
                    continue
                # Individual review projects are one clip; use the first ID.
                clip_id = clip_ids[0]
                if scoped_ids and clip_id not in scoped_ids:
                    continue
                candidate = candidates.get(clip_id)
                if candidate is None:
                    continue
                metadata_rows = self._project_clip_metadata(project)
                representation = "individual"
                if metadata_rows:
                    representation = metadata_rows[0].get(
                        "com.vclip.representation", "individual"
                    )
                if representation == "compilation":
                    continue
                media_identity = media_identity_for_row(candidate)
                start_text, duration_text, start_s, duration_s = source_range_for_row(
                    candidate, project
                )
                if media_identity is None or start_s is None or duration_s is None:
                    continue
                projects.append(
                    DedupeProject(
                        order=order,
                        project_name=name or "<unnamed project>",
                        project_uid=project.get("uid"),
                        event_name=event_name,
                        stock_clip_id=clip_id,
                        stockify_run_id=str(candidate["run_id"]),
                        source_project_id=candidate.get("source_project_id"),
                        representation=representation,
                        media_identity=media_identity,
                        source_start_seconds=start_s,
                        source_duration_seconds=duration_s,
                        source_start=start_text or f"{start_s}s",
                        source_duration=duration_text or f"{duration_s}s",
                        short_clip_recovery=candidate.get("short_clip_recovery"),
                        element=project,
                    )
                )
                order += 1
        return projects

    @staticmethod
    def _project_clip_ids(project: ET.Element) -> list[str]:
        return list(
            dict.fromkeys(
                row["com.vclip.stock_clip_id"]
                for row in ReviewDedupeService._project_clip_metadata(project)
                if row.get("com.vclip.stock_clip_id")
            )
        )

    @staticmethod
    def _project_clip_metadata(project: ET.Element) -> list[dict[str, str]]:
        sequence = first_direct_child(project, "sequence")
        spine = first_direct_child(sequence, "spine") if sequence is not None else None
        if spine is None:
            return []
        rows: list[dict[str, str]] = []
        for node in spine.iter():
            if node is spine or local_name(node.tag) not in {"asset-clip", "video"}:
                continue
            metadata = read_vclip_metadata(node)
            if metadata:
                rows.append(metadata)
        return rows

    def _cluster_duplicates(
        self, projects: list[DedupeProject]
    ) -> list[list[DedupeProject]]:
        return self._cluster_by_predicate(projects, exact_source_range_duplicate)

    @staticmethod
    def _cluster_by_predicate(
        projects: list[DedupeProject],
        predicate,
    ) -> list[list[DedupeProject]]:
        if not projects:
            return []
        parent = list(range(len(projects)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            root_left = find(left)
            root_right = find(right)
            if root_left != root_right:
                parent[root_right] = root_left

        for left_index, left in enumerate(projects):
            for right_index in range(left_index + 1, len(projects)):
                right = projects[right_index]
                if predicate(left, right):
                    union(left_index, right_index)

        grouped: dict[int, list[DedupeProject]] = {}
        for index, project in enumerate(projects):
            grouped.setdefault(find(index), []).append(project)
        return [group for group in grouped.values() if len(group) >= 2]

    @staticmethod
    def _choose_exact_removals(
        clusters: list[list[DedupeProject]],
    ) -> list[DedupeRemoval]:
        removals: list[DedupeRemoval] = []
        for cluster in clusters:
            kept = min(cluster, key=_representative_sort_key)
            for project in sorted(cluster, key=lambda item: item.order):
                if project.stock_clip_id == kept.stock_clip_id:
                    continue
                removals.append(
                    DedupeRemoval(
                        removed_project_name=project.project_name,
                        kept_project_name=kept.project_name,
                        removed_stock_clip_id=project.stock_clip_id,
                        kept_stock_clip_id=kept.stock_clip_id,
                        source_media=project.media_identity,
                        source_start=project.source_start,
                        source_duration=project.source_duration,
                        reason=REASON,
                        removed_short_clip_recovery=project.short_clip_recovery,
                        kept_short_clip_recovery=kept.short_clip_recovery,
                        cluster_size=len(cluster),
                    )
                )
        removals.sort(key=lambda item: (item.kept_project_name, item.removed_project_name))
        return removals

    @staticmethod
    def _choose_near_removals(
        clusters: list[list[DedupeProject]],
        *,
        exact_representatives: set[str],
    ) -> list[DedupeRemoval]:
        removals: list[DedupeRemoval] = []
        for cluster in clusters:
            kept = min(
                cluster,
                key=lambda project: _near_representative_sort_key(
                    project,
                    cluster,
                    exact_representatives,
                ),
            )
            for project in sorted(cluster, key=lambda item: item.order):
                if project.stock_clip_id == kept.stock_clip_id:
                    continue
                removals.append(
                    DedupeRemoval(
                        removed_project_name=project.project_name,
                        kept_project_name=kept.project_name,
                        removed_stock_clip_id=project.stock_clip_id,
                        kept_stock_clip_id=kept.stock_clip_id,
                        source_media=project.media_identity,
                        source_start=project.source_start,
                        source_duration=project.source_duration,
                        reason=NEAR_REASON,
                        removed_short_clip_recovery=project.short_clip_recovery,
                        kept_short_clip_recovery=kept.short_clip_recovery,
                        cluster_size=len(cluster),
                        containment=round(range_containment(project, kept), 6),
                        iou=round(range_iou(project, kept), 6),
                    )
                )
        removals.sort(key=lambda item: (item.kept_project_name, item.removed_project_name))
        return removals

    def _write_output(
        self,
        root: ET.Element,
        output_xml: Path,
        removed_names: set[str],
    ) -> None:
        # Work on a deep copy so dry-run callers can reuse the source tree later.
        out_root = copy.deepcopy(root)
        for event in out_root.findall("./library/event"):
            for project in list(event.findall("project")):
                if project.get("name", "") in removed_names:
                    event.remove(project)
        validation = validate_fcpxml(out_root)
        if not validation.passed:
            raise VClipError(
                "Deduped review XML failed FCPXML validation: "
                + "; ".join(validation.errors[:10])
            )
        ET.indent(out_root)
        output_xml.parent.mkdir(parents=True, exist_ok=True)
        output_xml.write_bytes(
            ET.tostring(out_root, encoding="utf-8", xml_declaration=True)
        )

    @staticmethod
    def _update_manifest(
        manifest: dict[str, Any],
        removals: list[DedupeRemoval],
        output_xml: Path,
        *,
        update_source_manifest: bool = True,
    ) -> None:
        path = Path(manifest["path"])
        removed_ids = {item.removed_stock_clip_id for item in removals}
        removed_names = {item.removed_project_name for item in removals}
        payload = {
            key: value for key, value in manifest.items() if key != "path"
        }
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
        reasons = sorted({item.reason for item in removals}) or [REASON]
        payload["dedupe"] = {
            "removed_stock_clip_ids": sorted(removed_ids),
            "removed_project_names": sorted(removed_names),
            "output_fcpxml": str(output_xml),
            "reasons": reasons,
            "reason": reasons[0] if len(reasons) == 1 else "mixed",
        }
        # Always write a cleaned manifest next to the output XML.
        target = output_xml.with_name(f"{output_xml.stem}-shard-manifest.json")
        target.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        # Optionally annotate the source manifest (disabled for batch/manifest-root).
        if (
            update_source_manifest
            and path.resolve() != target.resolve()
        ):
            original = {
                key: value for key, value in manifest.items() if key != "path"
            }
            original["dedupe"] = payload["dedupe"]
            path.write_text(
                json.dumps(original, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

    def _announce(self, message: str) -> None:
        if self.progress:
            self.progress(message)


def media_identity_for_row(row: dict[str, Any]) -> str | None:
    media_id = row.get("source_media_id")
    if media_id:
        path = row.get("source_media_path")
        if path:
            return f"path:{path}"
        stem = row.get("source_normalized_stem")
        if stem:
            return f"stem:{stem}"
        return f"media:{media_id}"
    ref = row.get("source_ref")
    if ref:
        return f"ref:{ref}"
    return None


def source_range_for_row(
    row: dict[str, Any],
    project: ET.Element,
) -> tuple[str | None, str | None, float | None, float | None]:
    start_text = row.get("proposed_start") or row.get("original_start")
    duration_text = row.get("proposed_duration") or row.get("original_duration")
    if not start_text or not duration_text:
        sequence = first_direct_child(project, "sequence")
        spine = first_direct_child(sequence, "spine") if sequence is not None else None
        if spine is not None:
            for node in spine.iter():
                if node is spine or local_name(node.tag) not in {"asset-clip", "video"}:
                    continue
                start_text = node.get("start") or start_text or "0s"
                duration_text = node.get("duration") or duration_text
                break
    try:
        start_s = float(parse_time(start_text or "0s"))
        duration_s = float(parse_time(duration_text)) if duration_text else None
    except ValueError:
        return start_text, duration_text, None, None
    return start_text, duration_text, start_s, duration_s


def exact_source_range_duplicate(left: DedupeProject, right: DedupeProject) -> bool:
    if left.media_identity != right.media_identity:
        return False
    if abs(left.source_start_seconds - right.source_start_seconds) > EXACT_RANGE_TOLERANCE_SECONDS:
        return False
    if (
        abs(left.source_duration_seconds - right.source_duration_seconds)
        > EXACT_RANGE_TOLERANCE_SECONDS
    ):
        return False
    return True


def range_overlap_seconds(left: DedupeProject, right: DedupeProject) -> float:
    left_end = left.source_start_seconds + left.source_duration_seconds
    right_end = right.source_start_seconds + right.source_duration_seconds
    return max(0.0, min(left_end, right_end) - max(left.source_start_seconds, right.source_start_seconds))


def range_iou(left: DedupeProject, right: DedupeProject) -> float:
    if left.media_identity != right.media_identity:
        return 0.0
    overlap = range_overlap_seconds(left, right)
    if overlap <= 0:
        return 0.0
    union = (
        max(
            left.source_start_seconds + left.source_duration_seconds,
            right.source_start_seconds + right.source_duration_seconds,
        )
        - min(left.source_start_seconds, right.source_start_seconds)
    )
    if union <= 0:
        return 0.0
    return overlap / union


def range_containment(left: DedupeProject, right: DedupeProject) -> float:
    """Fraction of the shorter range covered by overlap (same-media only)."""
    if left.media_identity != right.media_identity:
        return 0.0
    overlap = range_overlap_seconds(left, right)
    shorter = min(left.source_duration_seconds, right.source_duration_seconds)
    if shorter <= 0:
        return 0.0
    return overlap / shorter


def aggressive_near_match(left: DedupeProject, right: DedupeProject) -> bool:
    """Aggressive near-duplicate predicate used for optional automatic removal."""
    if left.media_identity != right.media_identity:
        return False
    return (
        range_containment(left, right) >= NEAR_CONTAINMENT_THRESHOLD
        and range_iou(left, right) >= NEAR_IOU_THRESHOLD
    )


def near_source_range_duplicate(left: DedupeProject, right: DedupeProject) -> bool:
    """Ultra-near match that excludes already-exact duplicates."""
    if exact_source_range_duplicate(left, right):
        return False
    return aggressive_near_match(left, right)


def _representative_sort_key(project: DedupeProject) -> tuple[int, int, str]:
    # Prefer non-expanded_review / original-style candidates, then shard order.
    recovery = (project.short_clip_recovery or "").casefold()
    expanded_penalty = 1 if recovery == "expanded_review" else 0
    return (expanded_penalty, project.order, project.project_name)


def _near_representative_sort_key(
    project: DedupeProject,
    cluster: list[DedupeProject],
    exact_representatives: set[str],
) -> tuple[int, int, int, float, int, str]:
    """Deterministic keeper selection for aggressive near-duplicate clusters."""
    recovery = (project.short_clip_recovery or "").casefold()
    expanded_penalty = 1 if recovery == "expanded_review" else 0
    exact_rep_penalty = 0 if project.stock_clip_id in exact_representatives else 1
    others = [
        other
        for other in cluster
        if other.stock_clip_id != project.stock_clip_id
    ]
    near_to_all = all(aggressive_near_match(project, other) for other in others)
    # Prefer longer only when it still satisfies aggressive near-match to the cluster.
    duration_key = (
        -project.source_duration_seconds if near_to_all else 0.0
    )
    near_to_all_penalty = 0 if near_to_all else 1
    return (
        expanded_penalty,
        exact_rep_penalty,
        near_to_all_penalty,
        duration_key,
        project.order,
        project.project_name,
    )


def format_text_report(report: ReviewDedupeReport) -> str:
    lines = [
        "Review duplicate removal",
        "========================",
        f"Input:              {report.input_fcpxml}",
        f"Output:             {report.output_fcpxml or '(dry-run)'}",
        f"Run ID:             {report.stockify_run_id or '-'}",
        f"Near policy:        {report.near_policy}",
        f"Projects considered:{report.projects_considered:>7}",
        f"Exact clusters:     {report.exact_clusters_found:>7}",
        f"Exact removed:      {report.exact_projects_removed:>7}",
        f"Near clusters:      {report.near_clusters_found:>7}",
        f"Near removed:       {report.near_projects_removed:>7}",
        f"Total removed:      {report.projects_removed:>7}",
        f"Kept:               {report.projects_kept:>7}",
        f"Dry run:            {str(report.dry_run).lower()}",
        "",
    ]
    if not report.removals:
        lines.append("No source-range duplicates found.")
        return "\n".join(lines) + "\n"
    lines.append("Removals")
    lines.append("--------")
    for item in report.removals:
        lines.append(
            f"- remove {item.removed_project_name} ({item.removed_stock_clip_id})"
        )
        lines.append(
            f"  keep   {item.kept_project_name} ({item.kept_stock_clip_id})"
        )
        lines.append(
            f"  media  {item.source_media}  start={item.source_start}  "
            f"duration={item.source_duration}"
        )
        extra = ""
        if item.containment is not None and item.iou is not None:
            extra = f"  containment={item.containment:.4f} iou={item.iou:.4f}"
        lines.append(f"  reason {item.reason}{extra}")
    if report.warnings:
        lines.append("")
        lines.append("Warnings")
        lines.append("--------")
        for warning in report.warnings:
            lines.append(f"- {warning}")
    return "\n".join(lines) + "\n"
