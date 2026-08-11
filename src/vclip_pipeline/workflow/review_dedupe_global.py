"""Global cross-shard duplicate removal over an already-clean shard corpus."""

from __future__ import annotations

import json
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..db.repository import CatalogRepository
from ..errors import VClipError
from ..stockify.core import parse_time
from ..stockify.fcpxml import validate_fcpxml
from ..util import stable_id, utc_now
from .catalog import WorkflowCatalog
from .review_dedupe import (
    EXACT_RANGE_TOLERANCE_SECONDS,
    NEAR_CONTAINMENT_THRESHOLD,
    NEAR_IOU_THRESHOLD,
    NEAR_POLICIES,
    NEAR_REASON,
    REASON,
    ReviewDedupeService,
    aggressive_near_match,
    exact_source_range_duplicate,
    media_identity_for_row,
    range_containment,
    range_iou,
)

_DJI_DATE_RE = re.compile(r"(20\d{2})(\d{2})(\d{2})", re.IGNORECASE)
_UNKNOWN_LABELS = frozenset(
    {
        "",
        "unknown",
        "unknown location",
        "unknown place",
    }
)


@dataclass
class GlobalCandidate:
    """One surviving stock candidate appearance in the clean shard corpus."""

    corpus_order: int
    stock_clip_id: str
    stockify_run_id: str
    relative_shard: str
    project_name: str
    event_name: str
    media_identity: str
    source_start: str
    source_duration: str
    source_start_seconds: float
    source_duration_seconds: float
    short_clip_recovery: str | None
    review_status: str
    export_status: str
    location_label: str
    location_known: bool
    capture_date: str | None
    capture_consistent: bool
    source_filename: str | None
    session_public_label: str | None
    session_city: str | None
    structured_location_label: str | None
    row: dict[str, Any] = field(repr=False, compare=False)


@dataclass
class GlobalEdge:
    left_stock_clip_id: str
    right_stock_clip_id: str
    relation: str  # exact | near
    containment: float
    iou: float
    source_media: str


@dataclass
class GlobalRemoval:
    stockify_run_id: str
    removed_stock_clip_id: str
    canonical_stock_clip_id: str
    cluster_id: str
    cluster_type: str
    source_media: str
    canonical_source_start: str
    canonical_source_duration: str
    reason: str
    containment: float | None
    iou: float | None
    source_shard: str
    removed_project_name: str
    kept_project_name: str
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass
class GlobalClusterRecord:
    cluster_id: str
    cluster_type: str
    canonical_stock_clip_id: str
    removed_stock_clip_ids: list[str]
    source_media: str
    canonical_source_start: str
    canonical_source_duration: str
    member_stock_clip_ids: list[str]
    source_shards: list[str]
    source_project_names: list[str]
    event_names: list[str]
    location_claims: list[dict[str, Any]]
    capture_time_claims: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    keeper_selection_reasons: list[str]
    metadata_conflict_class: str
    projects_removed: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReviewGlobalDedupeReport:
    input_root: str
    output_root: str
    dry_run: bool
    near_policy: str
    candidates_before: int = 0
    exact_pair_relationships: int = 0
    near_pair_relationships: int = 0
    connected_clusters: int = 0
    projects_implicated: int = 0
    exact_only_clusters: int = 0
    near_only_clusters: int = 0
    mixed_clusters: int = 0
    projects_removed: int = 0
    candidates_after: int = 0
    reduction_percent: float = 0.0
    metadata_conflict_clusters: int = 0
    known_plus_unknown_clusters: int = 0
    shards_changed: int = 0
    shards_unchanged: int = 0
    shards_failed: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    clusters: list[GlobalClusterRecord] = field(default_factory=list)
    post_write_audit: dict[str, Any] = field(default_factory=dict)
    thresholds: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_root": self.input_root,
            "output_root": self.output_root,
            "dry_run": self.dry_run,
            "near_policy": self.near_policy,
            "thresholds": self.thresholds,
            "candidates_before": self.candidates_before,
            "exact_pair_relationships": self.exact_pair_relationships,
            "near_pair_relationships": self.near_pair_relationships,
            "connected_clusters": self.connected_clusters,
            "projects_implicated": self.projects_implicated,
            "exact_only_clusters": self.exact_only_clusters,
            "near_only_clusters": self.near_only_clusters,
            "mixed_clusters": self.mixed_clusters,
            "projects_removed": self.projects_removed,
            "candidates_after": self.candidates_after,
            "reduction_percent": self.reduction_percent,
            "metadata_conflict_clusters": self.metadata_conflict_clusters,
            "known_plus_unknown_clusters": self.known_plus_unknown_clusters,
            "shards_changed": self.shards_changed,
            "shards_unchanged": self.shards_unchanged,
            "shards_failed": self.shards_failed,
            "failures": list(self.failures),
            "warnings": list(self.warnings),
            "clusters": [item.as_dict() for item in self.clusters],
            "post_write_audit": self.post_write_audit,
        }


class ReviewGlobalDedupeService:
    """Collapse duplicate source-range assets across clean shard boundaries."""

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
        conflict_report_path: Path,
        near_policy: str = "none",
        dry_run: bool = False,
        overwrite: bool = False,
    ) -> ReviewGlobalDedupeReport:
        policy = (near_policy or "none").strip().casefold()
        if policy not in NEAR_POLICIES:
            raise VClipError(
                f"Unsupported near-policy {near_policy!r}; "
                f"expected one of {sorted(NEAR_POLICIES)}"
            )
        input_root = input_root.expanduser().resolve()
        output_root = output_root.expanduser().resolve()
        report_path = report_path.expanduser().resolve()
        text_report_path = text_report_path.expanduser().resolve()
        conflict_report_path = conflict_report_path.expanduser().resolve()
        if not input_root.is_dir():
            raise VClipError(f"Input root not found: {input_root}")
        if output_root.exists() and any(output_root.iterdir()) and not overwrite and not dry_run:
            raise VClipError(
                f"Output root is not empty: {output_root} (pass --overwrite)"
            )

        self._announce(f"Scanning clean shard corpus: {input_root}")
        shard_entries = self._discover_shards(input_root)
        candidates, warnings = self._load_global_candidates(shard_entries)
        edges = self._build_edges(candidates, near_policy=policy)
        clusters = self._connected_components(candidates, edges)

        cluster_records: list[GlobalClusterRecord] = []
        removals: list[GlobalRemoval] = []
        removed_by_shard: dict[str, set[str]] = {}
        removed_project_names_by_shard: dict[str, set[str]] = {}

        for cluster in clusters:
            record, cluster_removals = self._resolve_cluster(cluster, edges)
            cluster_records.append(record)
            for removal in cluster_removals:
                removals.append(removal)
                removed_by_shard.setdefault(removal.source_shard, set()).add(
                    removal.removed_stock_clip_id
                )
                removed_project_names_by_shard.setdefault(
                    removal.source_shard, set()
                ).add(removal.removed_project_name)

        report = ReviewGlobalDedupeReport(
            input_root=str(input_root),
            output_root=str(output_root),
            dry_run=dry_run,
            near_policy=policy,
            thresholds={
                "exact_tolerance_seconds": EXACT_RANGE_TOLERANCE_SECONDS,
                "near_containment": NEAR_CONTAINMENT_THRESHOLD,
                "near_iou": NEAR_IOU_THRESHOLD,
            },
            candidates_before=len(candidates),
            exact_pair_relationships=sum(1 for edge in edges if edge.relation == "exact"),
            near_pair_relationships=sum(1 for edge in edges if edge.relation == "near"),
            connected_clusters=len(cluster_records),
            projects_implicated=len({member.stock_clip_id for cluster in clusters for member in cluster}),
            exact_only_clusters=sum(1 for item in cluster_records if item.cluster_type == "exact"),
            near_only_clusters=sum(1 for item in cluster_records if item.cluster_type == "near"),
            mixed_clusters=sum(1 for item in cluster_records if item.cluster_type == "mixed"),
            projects_removed=len(removals),
            candidates_after=len(candidates) - len(removals),
            metadata_conflict_clusters=sum(
                1
                for item in cluster_records
                if item.metadata_conflict_class == "conflicting_known_labels"
            ),
            known_plus_unknown_clusters=sum(
                1
                for item in cluster_records
                if item.metadata_conflict_class == "known_plus_unknown"
            ),
            clusters=cluster_records,
            warnings=warnings,
        )
        if report.candidates_before > 0:
            report.reduction_percent = round(
                100.0 * report.projects_removed / report.candidates_before, 3
            )

        # Hypothetical survivor set for dry-run audit / post-write audit basis.
        removed_ids = {item.removed_stock_clip_id for item in removals}
        survivor_candidates = [
            item for item in candidates if item.stock_clip_id not in removed_ids
        ]

        changed_shards = set(removed_by_shard)
        report.shards_changed = len(changed_shards)
        report.shards_unchanged = max(0, len(shard_entries) - len(changed_shards))

        if dry_run:
            self._announce(
                f"Dry run: would remove {len(removals)} global duplicate(s) "
                f"across {len(cluster_records)} cluster(s)."
            )
            report.post_write_audit = self._audit_candidates(
                survivor_candidates, near_policy=policy
            )
        else:
            if overwrite and output_root.exists():
                # Replace managed outputs safely by rewriting files; do not wipe unrelated paths.
                pass
            output_root.mkdir(parents=True, exist_ok=True)
            changed, unchanged, failures = self._write_corpus(
                input_root=input_root,
                output_root=output_root,
                shard_entries=shard_entries,
                removed_by_shard=removed_by_shard,
                removed_project_names_by_shard=removed_project_names_by_shard,
                overwrite=overwrite,
            )
            report.shards_changed = changed
            report.shards_unchanged = unchanged
            report.shards_failed = len(failures)
            report.failures.extend(failures)
            self.catalog.record_review_global_dedupe_removals(removals=removals)
            # Post-write audit on actual output survivors (same rules).
            output_entries = self._discover_shards(output_root)
            output_candidates, audit_warnings = self._load_global_candidates(output_entries)
            report.warnings.extend(audit_warnings)
            report.post_write_audit = self._audit_candidates(
                output_candidates, near_policy=policy
            )

        self._write_reports(
            report,
            report_path=report_path,
            text_report_path=text_report_path,
            conflict_report_path=conflict_report_path,
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
                raise VClipError(f"Could not read shard manifest {manifest_path}: {exc}") from exc
            entries.append(
                {
                    "relative_xml": relative,
                    "xml_path": xml_path.resolve(),
                    "manifest_path": manifest_path.resolve(),
                    "manifest": manifest,
                }
            )
        return entries

    def _load_global_candidates(
        self,
        shard_entries: list[dict[str, Any]],
    ) -> tuple[list[GlobalCandidate], list[str]]:
        warnings: list[str] = []
        appearances: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        clip_ids: set[str] = set()
        for entry in shard_entries:
            manifest = entry["manifest"]
            seen_in_shard: set[str] = set()
            for project in manifest.get("projects") or []:
                if project.get("representation") == "compilation":
                    continue
                if "Stock Compilation" in str(project.get("project_name") or ""):
                    continue
                for clip_id in project.get("stock_clip_ids") or []:
                    clip_id = str(clip_id)
                    if clip_id in seen_in_shard:
                        continue
                    seen_in_shard.add(clip_id)
                    clip_ids.add(clip_id)
                    appearances.append((clip_id, entry, project))
            # Fallback for manifests that list clip IDs only at the top level
            # (e.g. after within-shard dedupe helpers that sync stock_clip_ids).
            for clip_id in manifest.get("stock_clip_ids") or []:
                clip_id = str(clip_id)
                if clip_id in seen_in_shard:
                    continue
                seen_in_shard.add(clip_id)
                clip_ids.add(clip_id)
                appearances.append(
                    (
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

        rows = self.repository.candidates_by_ids(clip_ids)
        candidates: list[GlobalCandidate] = []
        seen: set[str] = set()
        order = 0
        for clip_id, entry, project in appearances:
            if clip_id in seen:
                continue
            row = rows.get(clip_id)
            if row is None:
                warnings.append(
                    f"{entry['relative_xml']}: missing accepted candidate {clip_id}"
                )
                continue
            media = media_identity_for_row(row)
            start_text = row.get("proposed_start") or row.get("original_start") or "0s"
            duration_text = row.get("proposed_duration") or row.get("original_duration")
            if media is None or not duration_text:
                warnings.append(
                    f"{entry['relative_xml']}: incomplete source range for {clip_id}"
                )
                continue
            try:
                start_s = float(parse_time(start_text))
                duration_s = float(parse_time(duration_text))
            except ValueError:
                warnings.append(
                    f"{entry['relative_xml']}: unparsable source range for {clip_id}"
                )
                continue
            location_label, location_known, structured = _structured_location(row)
            capture_date = _capture_date(row)
            candidates.append(
                GlobalCandidate(
                    corpus_order=order,
                    stock_clip_id=clip_id,
                    stockify_run_id=str(row["run_id"]),
                    relative_shard=entry["relative_xml"],
                    project_name=str(
                        project.get("project_name")
                        or row.get("generated_clip_project_name")
                        or clip_id
                    ),
                    event_name=str(project.get("event_name") or row.get("generated_event_name") or ""),
                    media_identity=media,
                    source_start=str(start_text),
                    source_duration=str(duration_text),
                    source_start_seconds=start_s,
                    source_duration_seconds=duration_s,
                    short_clip_recovery=row.get("short_clip_recovery"),
                    review_status=str(row.get("review_status") or "pending"),
                    export_status=str(row.get("export_status") or "pending"),
                    location_label=location_label,
                    location_known=location_known,
                    capture_date=capture_date,
                    capture_consistent=_capture_consistent(row, capture_date),
                    source_filename=row.get("source_filename"),
                    session_public_label=row.get("session_public_label"),
                    session_city=row.get("session_city"),
                    structured_location_label=structured,
                    row=row,
                )
            )
            seen.add(clip_id)
            order += 1
        return candidates, warnings

    def _build_edges(
        self,
        candidates: list[GlobalCandidate],
        *,
        near_policy: str,
    ) -> list[GlobalEdge]:
        edges: list[GlobalEdge] = []
        for left_index, left in enumerate(candidates):
            for right in candidates[left_index + 1 :]:
                # Global pass only forms relationships across shard boundaries.
                if left.relative_shard == right.relative_shard:
                    continue
                if exact_source_range_duplicate(left, right):  # type: ignore[arg-type]
                    edges.append(
                        GlobalEdge(
                            left_stock_clip_id=left.stock_clip_id,
                            right_stock_clip_id=right.stock_clip_id,
                            relation="exact",
                            containment=round(range_containment(left, right), 6),  # type: ignore[arg-type]
                            iou=round(range_iou(left, right), 6),  # type: ignore[arg-type]
                            source_media=left.media_identity,
                        )
                    )
                    continue
                if near_policy == "aggressive" and aggressive_near_match(left, right):  # type: ignore[arg-type]
                    edges.append(
                        GlobalEdge(
                            left_stock_clip_id=left.stock_clip_id,
                            right_stock_clip_id=right.stock_clip_id,
                            relation="near",
                            containment=round(range_containment(left, right), 6),  # type: ignore[arg-type]
                            iou=round(range_iou(left, right), 6),  # type: ignore[arg-type]
                            source_media=left.media_identity,
                        )
                    )
        return edges

    def _connected_components(
        self,
        candidates: list[GlobalCandidate],
        edges: list[GlobalEdge],
    ) -> list[list[GlobalCandidate]]:
        by_id = {item.stock_clip_id: item for item in candidates}
        parent = {item.stock_clip_id: item.stock_clip_id for item in candidates}

        def find(node: str) -> str:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(left: str, right: str) -> None:
            root_left = find(left)
            root_right = find(right)
            if root_left != root_right:
                parent[root_right] = root_left

        for edge in edges:
            union(edge.left_stock_clip_id, edge.right_stock_clip_id)

        groups: dict[str, list[GlobalCandidate]] = {}
        for clip_id in parent:
            groups.setdefault(find(clip_id), []).append(by_id[clip_id])
        return [group for group in groups.values() if len(group) >= 2]

    def _resolve_cluster(
        self,
        cluster: list[GlobalCandidate],
        edges: list[GlobalEdge],
    ) -> tuple[GlobalClusterRecord, list[GlobalRemoval]]:
        member_ids = {item.stock_clip_id for item in cluster}
        cluster_edges = [
            edge
            for edge in edges
            if edge.left_stock_clip_id in member_ids
            and edge.right_stock_clip_id in member_ids
        ]
        relations = {edge.relation for edge in cluster_edges}
        if relations == {"exact"}:
            cluster_type = "exact"
        elif relations == {"near"}:
            cluster_type = "near"
        else:
            cluster_type = "mixed"

        keeper = min(
            cluster,
            key=lambda item: _global_keeper_sort_key(
                item, cluster, cluster_type=cluster_type
            ),
        )
        keeper_reasons = _explain_keeper_choice(keeper, cluster, cluster_type=cluster_type)
        conflict_class = _classify_metadata_conflict(cluster)

        cluster_id = stable_id(
            "GCLUSTER",
            keeper.media_identity,
            f"{keeper.source_start_seconds:.3f}",
            f"{keeper.source_duration_seconds:.3f}",
            *sorted(member_ids),
        )
        removals: list[GlobalRemoval] = []
        for member in sorted(cluster, key=lambda item: item.corpus_order):
            if member.stock_clip_id == keeper.stock_clip_id:
                continue
            edge = _best_edge_to_keeper(member.stock_clip_id, keeper.stock_clip_id, cluster_edges)
            reason = REASON if edge and edge.relation == "exact" else NEAR_REASON
            if cluster_type == "mixed" and edge is None:
                reason = NEAR_REASON if any(
                    item.relation == "near" for item in cluster_edges
                ) else REASON
            removals.append(
                GlobalRemoval(
                    stockify_run_id=member.stockify_run_id,
                    removed_stock_clip_id=member.stock_clip_id,
                    canonical_stock_clip_id=keeper.stock_clip_id,
                    cluster_id=cluster_id,
                    cluster_type=cluster_type,
                    source_media=keeper.media_identity,
                    canonical_source_start=keeper.source_start,
                    canonical_source_duration=keeper.source_duration,
                    reason=reason,
                    containment=edge.containment if edge else None,
                    iou=edge.iou if edge else None,
                    source_shard=member.relative_shard,
                    removed_project_name=member.project_name,
                    kept_project_name=keeper.project_name,
                    provenance={
                        "cluster_id": cluster_id,
                        "cluster_type": cluster_type,
                        "canonical_stock_clip_id": keeper.stock_clip_id,
                        "removed_stock_clip_id": member.stock_clip_id,
                        "source_shards": sorted({item.relative_shard for item in cluster}),
                        "source_project_names": sorted({item.project_name for item in cluster}),
                        "event_names": sorted({item.event_name for item in cluster if item.event_name}),
                        "location_claims": _location_claims(cluster),
                        "capture_time_claims": _capture_claims(cluster),
                        "keeper_selection_reasons": keeper_reasons,
                        "metadata_conflict_class": conflict_class,
                        "edge": asdict(edge) if edge else None,
                    },
                )
            )

        record = GlobalClusterRecord(
            cluster_id=cluster_id,
            cluster_type=cluster_type,
            canonical_stock_clip_id=keeper.stock_clip_id,
            removed_stock_clip_ids=[item.removed_stock_clip_id for item in removals],
            source_media=keeper.media_identity,
            canonical_source_start=keeper.source_start,
            canonical_source_duration=keeper.source_duration,
            member_stock_clip_ids=sorted(member_ids),
            source_shards=sorted({item.relative_shard for item in cluster}),
            source_project_names=sorted({item.project_name for item in cluster}),
            event_names=sorted({item.event_name for item in cluster if item.event_name}),
            location_claims=_location_claims(cluster),
            capture_time_claims=_capture_claims(cluster),
            edges=[asdict(edge) for edge in cluster_edges],
            keeper_selection_reasons=keeper_reasons,
            metadata_conflict_class=conflict_class,
            projects_removed=len(removals),
        )
        return record, removals

    def _write_corpus(
        self,
        *,
        input_root: Path,
        output_root: Path,
        shard_entries: list[dict[str, Any]],
        removed_by_shard: dict[str, set[str]],
        removed_project_names_by_shard: dict[str, set[str]],
        overwrite: bool,
    ) -> tuple[int, int, list[dict[str, str]]]:
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
                removed_names = removed_project_names_by_shard.get(relative, set())
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
    ) -> None:
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
        prior = payload.get("dedupe") if isinstance(payload.get("dedupe"), dict) else {}
        payload["global_dedupe"] = {
            "removed_stock_clip_ids": sorted(removed_ids),
            "removed_project_names": sorted(removed_names),
            "output_fcpxml": str(output_xml),
            "prior_within_shard_dedupe": prior,
        }
        target = output_xml.with_name(f"{output_xml.stem}-shard-manifest.json")
        target.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _audit_candidates(
        self,
        candidates: list[GlobalCandidate],
        *,
        near_policy: str,
    ) -> dict[str, Any]:
        edges = self._build_edges(candidates, near_policy=near_policy)
        exact = sum(1 for edge in edges if edge.relation == "exact")
        near = sum(1 for edge in edges if edge.relation == "near")
        return {
            "mode": "global_post_audit",
            "near_policy": near_policy,
            "candidates_audited": len(candidates),
            "remaining_exact_global_pairs": exact,
            "remaining_aggressive_near_pairs": near,
            "thresholds": {
                "exact_tolerance_seconds": EXACT_RANGE_TOLERANCE_SECONDS,
                "near_containment": NEAR_CONTAINMENT_THRESHOLD,
                "near_iou": NEAR_IOU_THRESHOLD,
            },
        }

    def _write_reports(
        self,
        report: ReviewGlobalDedupeReport,
        *,
        report_path: Path,
        text_report_path: Path,
        conflict_report_path: Path,
    ) -> None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        text_report_path.parent.mkdir(parents=True, exist_ok=True)
        conflict_report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report.as_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        text_report_path.write_text(format_global_text_report(report), encoding="utf-8")
        conflicts = [
            item.as_dict()
            for item in report.clusters
            if item.metadata_conflict_class
            in {"conflicting_known_labels", "known_plus_unknown"}
        ]
        conflict_report_path.write_text(
            json.dumps(
                {
                    "generated_at": utc_now(),
                    "conflict_cluster_count": len(conflicts),
                    "clusters": conflicts,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

    def _announce(self, message: str) -> None:
        if self.progress:
            self.progress(message)


def _structured_location(row: dict[str, Any]) -> tuple[str, bool, str | None]:
    location = row.get("location") if isinstance(row.get("location"), dict) else {}
    session_label = str(row.get("session_public_label") or "").strip()
    session_city = str(row.get("session_city") or "").strip()
    loc_label = str(location.get("public_label") or "").strip()
    loc_city = str(location.get("city") or "").strip()
    structured = next(
        (
            value
            for value in (loc_label, loc_city, session_label, session_city)
            if value and value.casefold() not in _UNKNOWN_LABELS
        ),
        None,
    )
    if structured:
        return structured, True, structured
    fallback = session_label or loc_label or "Unknown Location"
    known = fallback.casefold() not in _UNKNOWN_LABELS
    return fallback, known, structured


def _capture_date(row: dict[str, Any]) -> str | None:
    capture = row.get("capture_time") if isinstance(row.get("capture_time"), dict) else {}
    for key in ("captured_at_local", "capture_date", "date"):
        value = capture.get(key) if capture else None
        if value:
            text = str(value)
            return text[:10]
    session_date = row.get("session_capture_date") or row.get("session_captured_at_local")
    if session_date:
        return str(session_date)[:10]
    return None


def _capture_consistent(row: dict[str, Any], capture_date: str | None) -> bool:
    if not capture_date:
        return False
    filename = str(row.get("source_filename") or row.get("source_normalized_stem") or "")
    match = _DJI_DATE_RE.search(filename.replace("-", "").replace("_", ""))
    if not match:
        # No contradictory filename signal; treat session/capture presence as consistent.
        return True
    file_date = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return file_date == capture_date


def _status_rank(candidate: GlobalCandidate) -> int:
    if candidate.review_status == "approved":
        return 0
    if candidate.export_status == "matched":
        return 1
    if candidate.review_status == "pending":
        return 2
    return 3


def _location_rank(candidate: GlobalCandidate) -> int:
    # Stronger structured known location wins; unknown loses.
    if candidate.location_known and candidate.structured_location_label:
        return 0
    if candidate.location_known:
        return 1
    return 2


def _global_keeper_sort_key(
    candidate: GlobalCandidate,
    cluster: list[GlobalCandidate],
    *,
    cluster_type: str,
) -> tuple:
    near_to_all = True
    if cluster_type in {"near", "mixed"}:
        others = [item for item in cluster if item.stock_clip_id != candidate.stock_clip_id]
        near_to_all = all(
            aggressive_near_match(candidate, other)  # type: ignore[arg-type]
            or exact_source_range_duplicate(candidate, other)  # type: ignore[arg-type]
            for other in others
        )
    expanded_penalty = (
        1 if (candidate.short_clip_recovery or "").casefold() == "expanded_review" else 0
    )
    duration_key = (
        -candidate.source_duration_seconds
        if cluster_type in {"near", "mixed"} and near_to_all
        else 0.0
    )
    return (
        _status_rank(candidate),
        0 if candidate.capture_consistent else 1,
        _location_rank(candidate),
        expanded_penalty,
        0 if near_to_all else 1,
        duration_key,
        candidate.corpus_order,
        candidate.relative_shard,
        candidate.project_name,
        candidate.stock_clip_id,
    )


def _explain_keeper_choice(
    keeper: GlobalCandidate,
    cluster: list[GlobalCandidate],
    *,
    cluster_type: str,
) -> list[str]:
    reasons = []
    if _status_rank(keeper) == 0:
        reasons.append("approved_review_status")
    elif keeper.export_status == "matched":
        reasons.append("export_matched")
    if keeper.capture_consistent:
        reasons.append("capture_date_consistent_with_source_filename")
    if keeper.location_known:
        reasons.append("known_structured_location")
    else:
        reasons.append("fallback_location_unknown_or_weak")
    if (keeper.short_clip_recovery or "").casefold() != "expanded_review":
        reasons.append("non_expanded_review")
    if cluster_type in {"near", "mixed"}:
        reasons.append("near_cluster_duration_or_order_tiebreak")
    reasons.append("deterministic_corpus_order")
    return reasons


def _classify_metadata_conflict(cluster: list[GlobalCandidate]) -> str:
    structured = {
        (item.structured_location_label or "").casefold()
        for item in cluster
        if item.structured_location_label
    }
    # Strong structured agreement wins over contradictory event/project titles.
    if len(structured) == 1:
        if any(not item.location_known for item in cluster):
            return "known_plus_unknown"
        return "consistent"
    if len(structured) >= 2:
        return "conflicting_known_labels"

    known_labels = sorted(
        {
            item.location_label.casefold()
            for item in cluster
            if item.location_known and item.location_label
        }
    )
    unknown = any(not item.location_known for item in cluster)
    if len(known_labels) >= 2:
        return "conflicting_known_labels"
    if known_labels and unknown:
        return "known_plus_unknown"
    # Without structured agreement, competing event titles are competing claims.
    event_names = {
        item.event_name.strip().casefold()
        for item in cluster
        if item.event_name and item.event_name.strip()
    }
    if len(event_names) >= 2:
        return "conflicting_known_labels"
    capture_dates = {
        item.capture_date
        for item in cluster
        if item.capture_date
    }
    if len(capture_dates) >= 2:
        return "conflicting_known_labels"
    return "consistent"


def _location_claims(cluster: list[GlobalCandidate]) -> list[dict[str, Any]]:
    return [
        {
            "stock_clip_id": item.stock_clip_id,
            "project_name": item.project_name,
            "event_name": item.event_name,
            "structured_location_label": item.structured_location_label,
            "location_label": item.location_label,
            "location_known": item.location_known,
            "session_city": item.session_city,
            "session_public_label": item.session_public_label,
        }
        for item in sorted(cluster, key=lambda row: row.corpus_order)
    ]


def _capture_claims(cluster: list[GlobalCandidate]) -> list[dict[str, Any]]:
    return [
        {
            "stock_clip_id": item.stock_clip_id,
            "capture_date": item.capture_date,
            "capture_consistent": item.capture_consistent,
            "source_filename": item.source_filename,
        }
        for item in sorted(cluster, key=lambda row: row.corpus_order)
    ]


def _best_edge_to_keeper(
    removed_id: str,
    keeper_id: str,
    edges: list[GlobalEdge],
) -> GlobalEdge | None:
    direct = [
        edge
        for edge in edges
        if {edge.left_stock_clip_id, edge.right_stock_clip_id} == {removed_id, keeper_id}
    ]
    if direct:
        # Prefer exact evidence when both somehow exist.
        exact = [edge for edge in direct if edge.relation == "exact"]
        return exact[0] if exact else direct[0]
    return None


def format_global_text_report(report: ReviewGlobalDedupeReport) -> str:
    audit = report.post_write_audit or {}
    lines = [
        "Global review duplicate removal",
        "===============================",
        f"Input root:              {report.input_root}",
        f"Output root:             {report.output_root}",
        f"Near policy:             {report.near_policy}",
        f"Candidates before:       {report.candidates_before:>7}",
        f"Exact pair relations:    {report.exact_pair_relationships:>7}",
        f"Near pair relations:     {report.near_pair_relationships:>7}",
        f"Connected clusters:      {report.connected_clusters:>7}",
        f"Projects implicated:     {report.projects_implicated:>7}",
        f"Exact-only clusters:     {report.exact_only_clusters:>7}",
        f"Near-only clusters:      {report.near_only_clusters:>7}",
        f"Mixed clusters:          {report.mixed_clusters:>7}",
        f"Projects removed:        {report.projects_removed:>7}",
        f"Candidates after:        {report.candidates_after:>7}",
        f"Reduction:               {report.reduction_percent:>6.1f}%",
        f"Metadata conflicts:      {report.metadata_conflict_clusters:>7}",
        f"Known+unknown clusters:  {report.known_plus_unknown_clusters:>7}",
        f"Shards changed:          {report.shards_changed:>7}",
        f"Shards unchanged:        {report.shards_unchanged:>7}",
        f"Shards failed:           {report.shards_failed:>7}",
        f"Dry run:                 {str(report.dry_run).lower()}",
        "",
        "Post audit",
        "----------",
        f"Remaining exact pairs:   {audit.get('remaining_exact_global_pairs', 0):>7}",
        f"Remaining near pairs:    {audit.get('remaining_aggressive_near_pairs', 0):>7}",
        "",
    ]
    if report.failures:
        lines.append("Failures")
        lines.append("--------")
        for item in report.failures:
            lines.append(f"- {item['relative_path']}: {item['error']}")
        lines.append("")
    return "\n".join(lines) + "\n"
