"""Split a Stockify review FCPXML into small, authoritative review shards.

The sharder is intentionally a cheap post-processing step. It never scans media,
parses SRT files, changes candidate IDs, or creates a new Stockify run.
"""

from __future__ import annotations

import copy
import json
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Iterable

from ..db.repository import CatalogRepository
from ..errors import VClipError
from ..stockify.core import format_time, local_name, slugify, stable_uid
from ..stockify.fcpxml import (
    build_resource_index,
    first_direct_child,
    iter_source_events,
    parse_source,
    read_vclip_metadata,
    validate_fcpxml,
)
from ..util import safe_filename, sha256_file, stable_id, utc_now
from .models import ReviewShard, ShardProject


@dataclass
class ReviewShardReport:
    source_xml: str
    source_sha256: str
    stockify_run_id: str
    output_directory: str
    grouping: str
    representation: str
    max_projects: int
    max_megabytes: float | None
    projects_found: int = 0
    projects_selected: int = 0
    projects_skipped: int = 0
    source_projects_found: int = 0
    shards_written: int = 0
    shard_paths: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _ProjectUnit:
    source_project_id: str
    market_id: str
    market_label: str
    projects: tuple[ShardProject, ...]

    @property
    def project_count(self) -> int:
        return len(self.projects)


@dataclass(frozen=True)
class _RenderedShard:
    xml_bytes: bytes
    resource_ids: tuple[str, ...]
    stock_clip_ids: tuple[str, ...]
    source_project_ids: tuple[str, ...]
    scope_project_count: int


class MarketCatalog:
    """Small, versioned mapping from resolved cities to customer markets."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.version = int(payload.get("version", 1))
        self._markets: list[dict[str, Any]] = list(payload.get("markets", []))
        self._city_index: dict[str, tuple[str, str]] = {}
        for market in self._markets:
            market_id = str(market["id"])
            label = str(market.get("label") or market_id)
            for city in market.get("cities", []):
                self._city_index[str(city).casefold()] = (market_id, label)

    @classmethod
    def from_path(cls, path: Path) -> "MarketCatalog":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise VClipError(f"Could not read market catalog {path}: {exc}") from exc
        return cls(payload)

    def resolve(self, city: str | None, state: str | None) -> tuple[str, str]:
        if city:
            match = self._city_index.get(city.casefold())
            if match:
                return match
            state_slug = slugify(state or "unknown-state")
            city_slug = slugify(city)
            return f"{state_slug}--{city_slug}", f"{city}, {state}" if state else city
        if state:
            return f"unknown--{slugify(state)}", f"Unknown — {state}"
        return "unknown", "Unknown Location"


class ReviewShardService:
    """Create independently importable, partial-review FCPXML documents."""

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
        review_xml: Path,
        output_directory: Path,
        markets_path: Path,
        group_by: str = "market",
        representation: str = "individual",
        max_projects: int = 125,
        max_megabytes: float | None = 8.0,
        include_scope_markers: bool = True,
        include_compilations: bool = False,
        overwrite: bool = False,
        dry_run: bool = False,
        report_path: Path | None = None,
    ) -> ReviewShardReport:
        if group_by not in {"market", "event", "none"}:
            raise VClipError(f"Unsupported shard grouping: {group_by}")
        if representation not in {"individual", "compilation", "both"}:
            raise VClipError(f"Unsupported shard representation: {representation}")
        if max_projects <= 0:
            raise VClipError("--max-projects must be greater than zero.")
        if max_megabytes is not None and max_megabytes <= 0:
            raise VClipError("--max-megabytes must be greater than zero.")
        if not review_xml.is_file():
            raise VClipError(f"Review FCPXML does not exist: {review_xml}")

        source_sha = sha256_file(review_xml)
        root = parse_source(review_xml).getroot()
        resources = first_direct_child(root, "resources")
        library = first_direct_child(root, "library")
        if resources is None or library is None:
            raise VClipError("Review FCPXML must contain one resources section and library.")
        resource_index = build_resource_index(resources)
        if representation == "compilation":
            include_scope_markers = False
        projects, embedded_run_ids = self._read_projects(root)
        if len(embedded_run_ids) != 1:
            raise VClipError(
                "Review FCPXML must contain candidates from exactly one Stockify run; "
                f"found {len(embedded_run_ids)}."
            )
        run_id = next(iter(embedded_run_ids))
        self.repository.get_stockify_run(run_id)
        candidate_rows = self.repository.candidates_for_run(run_id, accepted_only=True)
        candidate_by_id = {
            str(row["stock_clip_id"]): row for row in candidate_rows
        }
        markets = MarketCatalog.from_path(markets_path)
        selected = self._select_projects(
            projects,
            representation=representation,
            include_compilations=include_compilations,
            candidate_by_id=candidate_by_id,
            markets=markets,
            group_by=group_by,
        )
        report = ReviewShardReport(
            source_xml=str(review_xml),
            source_sha256=source_sha,
            stockify_run_id=run_id,
            output_directory=str(output_directory),
            grouping=group_by,
            representation=representation,
            max_projects=max_projects,
            max_megabytes=max_megabytes,
            projects_found=len(projects),
            projects_selected=len(selected),
            projects_skipped=len(projects) - len(selected),
        )
        if not selected:
            raise VClipError("No generated review projects matched the requested representation.")

        units = self._build_units(selected)
        report.source_projects_found = len(units)
        grouped_units: dict[tuple[str, str], list[_ProjectUnit]] = defaultdict(list)
        for unit in units:
            grouped_units[(unit.market_id, unit.market_label)].append(unit)

        output_directory = output_directory.expanduser().resolve()
        if not dry_run:
            output_directory.mkdir(parents=True, exist_ok=True)
        max_bytes = (
            int(max_megabytes * 1024 * 1024)
            if max_megabytes is not None
            else None
        )
        shards: list[ReviewShard] = []
        for (market_id, market_label), market_units in sorted(grouped_units.items()):
            chunks = self._chunk_units(market_units, max_projects, report)
            fitted_chunks: list[list[_ProjectUnit]] = []
            for chunk in chunks:
                fitted_chunks.extend(
                    self._fit_by_size(
                        source_root=root,
                        source_resources=resources,
                        resource_index=resource_index,
                        run_id=run_id,
                        market_id=market_id,
                        market_label=market_label,
                        units=chunk,
                        max_bytes=max_bytes,
                        include_scope_markers=include_scope_markers,
                    )
                )
            for part, chunk in enumerate(fitted_chunks, start=1):
                shard_id = stable_id(
                    "SHARD",
                    run_id,
                    market_id,
                    part,
                    *[unit.source_project_id for unit in chunk],
                )
                rendered = self._render_shard(
                    source_root=root,
                    source_resources=resources,
                    resource_index=resource_index,
                    run_id=run_id,
                    shard_id=shard_id,
                    market_id=market_id,
                    market_label=market_label,
                    part=part,
                    units=chunk,
                    include_scope_markers=include_scope_markers,
                )
                filename = f"{slugify(review_xml.stem)}--{slugify(market_id)}--{part:02d}.fcpxml"
                path = output_directory / filename
                manifest_path = path.with_name(f"{path.stem}-shard-manifest.json")
                if not dry_run:
                    if (path.exists() or manifest_path.exists()) and not overwrite:
                        raise VClipError(
                            f"Review shard output already exists: {path}. Use --overwrite."
                        )
                    path.write_bytes(rendered.xml_bytes)
                    manifest = self._shard_manifest(
                        shard_id=shard_id,
                        run_id=run_id,
                        source_xml=review_xml,
                        source_sha=source_sha,
                        market_id=market_id,
                        market_label=market_label,
                        part=part,
                        path=path,
                        rendered=rendered,
                        projects=[p for unit in chunk for p in unit.projects],
                        grouping=group_by,
                        representation=representation,
                        include_scope_markers=include_scope_markers,
                    )
                    manifest_path.write_text(
                        json.dumps(manifest, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                shards.append(
                    ReviewShard(
                        shard_id=shard_id,
                        market_id=market_id,
                        market_label=market_label,
                        part=part,
                        path=path,
                        manifest_path=manifest_path,
                        project_count=sum(unit.project_count for unit in chunk),
                        scope_project_count=rendered.scope_project_count,
                        stock_clip_ids=rendered.stock_clip_ids,
                        source_project_ids=rendered.source_project_ids,
                        resource_ids=rendered.resource_ids,
                        size_bytes=len(rendered.xml_bytes),
                    )
                )
                self._announce(
                    f"{market_label} part {part:02d}: "
                    f"{sum(unit.project_count for unit in chunk)} clip project(s), "
                    f"{len(rendered.xml_bytes) / 1024 / 1024:.2f} MB"
                )

        index_payload = {
            "manifest_version": 1,
            "created_at": utc_now(),
            "source_xml": str(review_xml),
            "source_sha256": source_sha,
            "stockify_run_id": run_id,
            "grouping": group_by,
            "representation": representation,
            "max_projects": max_projects,
            "max_megabytes": max_megabytes,
            "include_scope_markers": include_scope_markers,
            "shards": [
                {
                    **asdict(shard),
                    "path": str(shard.path),
                    "manifest_path": str(shard.manifest_path),
                }
                for shard in shards
            ],
            "reconcile": {
                "authority": "individual" if representation != "compilation" else "compilation",
                "scope": "observed-projects",
                "instruction": (
                    "Export each reviewed shard from Final Cut and reconcile it with "
                    "--scope observed-projects. Keep scope-marker projects until after "
                    "the reviewed XML is exported."
                ),
            },
        }
        index_path = output_directory / f"{slugify(review_xml.stem)}--shards.json"
        if not dry_run:
            if index_path.exists() and not overwrite:
                raise VClipError(f"Shard index already exists: {index_path}. Use --overwrite.")
            index_path.write_text(
                json.dumps(index_payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        report.shards_written = len(shards)
        report.shard_paths = [str(shard.path) for shard in shards]
        self._write_report(report_path, report)
        return report

    def _read_projects(self, root: ET.Element) -> tuple[list[ShardProject], set[str]]:
        projects: list[ShardProject] = []
        run_ids: set[str] = set()
        for event in iter_source_events(root):
            event_name = event.get("name", "<unnamed event>")
            for project in [
                child for child in list(event) if local_name(child.tag) == "project"
            ]:
                metadata_rows = self._project_metadata(project)
                project_run_ids = {
                    row.get("com.vclip.stockify_run_id", "")
                    for row in metadata_rows
                    if row.get("com.vclip.stockify_run_id")
                }
                clip_ids = tuple(
                    dict.fromkeys(
                        row["com.vclip.stock_clip_id"]
                        for row in metadata_rows
                        if row.get("com.vclip.stock_clip_id")
                    )
                )
                source_project_ids = {
                    row.get("com.vclip.source_project_id", "")
                    for row in metadata_rows
                    if row.get("com.vclip.source_project_id")
                }
                representations = {
                    row.get("com.vclip.representation", "")
                    for row in metadata_rows
                    if row.get("com.vclip.representation")
                }
                if not project_run_ids or not clip_ids:
                    continue
                run_ids.update(project_run_ids)
                representation = (
                    next(iter(representations))
                    if len(representations) == 1
                    else "compilation" if "Stock Compilation" in project.get("name", "") else "individual"
                )
                source_project_id = (
                    next(iter(source_project_ids))
                    if len(source_project_ids) == 1
                    else f"mixed:{stable_id('PROJECT', *clip_ids)}"
                )
                projects.append(
                    ShardProject(
                        event_name=event_name,
                        event_uid=event.get("uid"),
                        project_name=project.get("name", "<unnamed project>"),
                        project_uid=project.get("uid"),
                        representation=representation,
                        stockify_run_id=next(iter(project_run_ids)),
                        source_project_id=source_project_id,
                        stock_clip_ids=clip_ids,
                        market_id="unknown",
                        market_label="Unknown Location",
                        element=project,
                    )
                )
        return projects, run_ids

    @staticmethod
    def _project_metadata(project: ET.Element) -> list[dict[str, str]]:
        sequence = first_direct_child(project, "sequence")
        spine = first_direct_child(sequence, "spine") if sequence is not None else None
        if spine is None:
            return []
        result: list[dict[str, str]] = []
        for node in spine.iter():
            if node is spine or local_name(node.tag) not in {"asset-clip", "video"}:
                continue
            metadata = read_vclip_metadata(node)
            if metadata:
                result.append(metadata)
        return result

    @staticmethod
    def _select_projects(
        projects: list[ShardProject],
        *,
        representation: str,
        include_compilations: bool,
        candidate_by_id: dict[str, dict[str, Any]],
        markets: MarketCatalog,
        group_by: str,
    ) -> list[ShardProject]:
        selected: list[ShardProject] = []
        for project in projects:
            if representation != "both" and project.representation != representation:
                continue
            if project.representation == "compilation" and not include_compilations and representation != "compilation":
                continue
            market_id, market_label = ReviewShardService._project_market(
                project, candidate_by_id, markets, group_by
            )
            selected.append(
                ShardProject(
                    **{
                        **project.__dict__,
                        "market_id": market_id,
                        "market_label": market_label,
                    }
                )
            )
        return selected

    @staticmethod
    def _project_market(
        project: ShardProject,
        candidate_by_id: dict[str, dict[str, Any]],
        markets: MarketCatalog,
        group_by: str,
    ) -> tuple[str, str]:
        if group_by == "none":
            return "all", "All Locations"
        if group_by == "event":
            return slugify(project.event_name), project.event_name
        votes: list[tuple[str, str]] = []
        for clip_id in project.stock_clip_ids:
            candidate = candidate_by_id.get(clip_id)
            if candidate is None:
                continue
            votes.append(
                markets.resolve(
                    candidate.get("session_city"),
                    candidate.get("session_state"),
                )
            )
        if not votes:
            return "unknown", "Unknown Location"
        counts = Counter(votes)
        (market_id, label), count = counts.most_common(1)[0]
        if len(counts) > 1 and count / len(votes) < 2 / 3:
            return "mixed", "Mixed Locations"
        return market_id, label

    @staticmethod
    def _build_units(projects: list[ShardProject]) -> list[_ProjectUnit]:
        by_source: dict[str, list[ShardProject]] = defaultdict(list)
        for project in projects:
            by_source[project.source_project_id].append(project)
        units: list[_ProjectUnit] = []
        for source_project_id, source_projects in by_source.items():
            votes = Counter((p.market_id, p.market_label) for p in source_projects)
            (market_id, market_label), count = votes.most_common(1)[0]
            if len(votes) > 1 and count / len(source_projects) < 2 / 3:
                market_id, market_label = "mixed", "Mixed Locations"
            units.append(
                _ProjectUnit(
                    source_project_id=source_project_id,
                    market_id=market_id,
                    market_label=market_label,
                    projects=tuple(source_projects),
                )
            )
        return units

    @staticmethod
    def _chunk_units(
        units: list[_ProjectUnit],
        max_projects: int,
        report: ReviewShardReport,
    ) -> list[list[_ProjectUnit]]:
        chunks: list[list[_ProjectUnit]] = []
        current: list[_ProjectUnit] = []
        count = 0
        for unit in units:
            if unit.project_count > max_projects:
                report.warnings.append(
                    f"Source project {unit.source_project_id} contains {unit.project_count} "
                    f"individual projects, exceeding the {max_projects} project target; "
                    "it remains atomic so scope-marker reconciliation stays safe."
                )
            if current and count + unit.project_count > max_projects:
                chunks.append(current)
                current = []
                count = 0
            current.append(unit)
            count += unit.project_count
        if current:
            chunks.append(current)
        return chunks

    def _fit_by_size(
        self,
        *,
        source_root: ET.Element,
        source_resources: ET.Element,
        resource_index: dict[str, ET.Element],
        run_id: str,
        market_id: str,
        market_label: str,
        units: list[_ProjectUnit],
        max_bytes: int | None,
        include_scope_markers: bool,
    ) -> list[list[_ProjectUnit]]:
        if max_bytes is None or len(units) <= 1:
            return [units]
        rendered = self._render_shard(
            source_root=source_root,
            source_resources=source_resources,
            resource_index=resource_index,
            run_id=run_id,
            shard_id="SIZE_PROBE",
            market_id=market_id,
            market_label=market_label,
            part=0,
            units=units,
            include_scope_markers=include_scope_markers,
        )
        if len(rendered.xml_bytes) <= max_bytes:
            return [units]
        midpoint = max(1, len(units) // 2)
        return self._fit_by_size(
            source_root=source_root,
            source_resources=source_resources,
            resource_index=resource_index,
            run_id=run_id,
            market_id=market_id,
            market_label=market_label,
            units=units[:midpoint],
            max_bytes=max_bytes,
            include_scope_markers=include_scope_markers,
        ) + self._fit_by_size(
            source_root=source_root,
            source_resources=source_resources,
            resource_index=resource_index,
            run_id=run_id,
            market_id=market_id,
            market_label=market_label,
            units=units[midpoint:],
            max_bytes=max_bytes,
            include_scope_markers=include_scope_markers,
        )

    def _render_shard(
        self,
        *,
        source_root: ET.Element,
        source_resources: ET.Element,
        resource_index: dict[str, ET.Element],
        run_id: str,
        shard_id: str,
        market_id: str,
        market_label: str,
        part: int,
        units: list[_ProjectUnit],
        include_scope_markers: bool,
    ) -> _RenderedShard:
        selected_projects = [project for unit in units for project in unit.projects]
        project_elements: list[ET.Element] = []
        by_event: dict[tuple[str, str | None], list[ET.Element]] = defaultdict(list)
        scope_count = 0
        for unit in units:
            for project in unit.projects:
                copied = copy.deepcopy(project.element)
                project_elements.append(copied)
                by_event[(project.event_name, project.event_uid)].append(copied)
            if include_scope_markers and unit.projects:
                marker = self._make_scope_marker(unit, resource_index)
                project_elements.append(marker)
                first = unit.projects[0]
                by_event[(first.event_name, first.event_uid)].append(marker)
                scope_count += 1

        required = self._resource_closure(project_elements, source_resources)
        root = ET.Element("fcpxml", {"version": source_root.get("version", "1.12")})
        resources = ET.SubElement(root, "resources")
        for child in list(source_resources):
            resource_id = child.get("id")
            if resource_id and resource_id in required:
                resources.append(copy.deepcopy(child))
        library = ET.SubElement(root, "library")
        suffix = f"{market_label} {part:02d}" if part else market_label
        for (event_name, event_uid), event_projects in by_event.items():
            event = ET.SubElement(
                library,
                "event",
                {
                    "name": safe_filename(f"{event_name} — {suffix}"),
                    "uid": stable_uid(
                        "review-shard-event",
                        run_id,
                        shard_id,
                        event_uid or event_name,
                    ),
                },
            )
            for project in event_projects:
                event.append(project)

        validation = validate_fcpxml(root)
        if not validation.passed:
            raise VClipError(
                "Generated review shard failed FCPXML validation: "
                + "; ".join(validation.errors[:10])
            )
        ET.indent(root)
        xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        clip_ids = tuple(
            dict.fromkeys(
                clip_id
                for project in selected_projects
                for clip_id in project.stock_clip_ids
            )
        )
        source_ids = tuple(unit.source_project_id for unit in units)
        return _RenderedShard(
            xml_bytes=xml_bytes,
            resource_ids=tuple(
                child.get("id", "") for child in list(resources) if child.get("id")
            ),
            stock_clip_ids=clip_ids,
            source_project_ids=source_ids,
            scope_project_count=scope_count,
        )

    @staticmethod
    def _make_scope_marker(
        unit: _ProjectUnit,
        resource_index: dict[str, ET.Element],
    ) -> ET.Element:
        first_project = unit.projects[0].element
        source_sequence = first_direct_child(first_project, "sequence")
        if source_sequence is None or not source_sequence.get("format"):
            raise VClipError(
                f"Cannot create scope marker for source project {unit.source_project_id}: "
                "missing sequence format."
            )
        format_id = str(source_sequence.get("format"))
        frame_duration = Fraction(1, 30)
        format_resource = resource_index.get(format_id)
        if format_resource is not None and format_resource.get("frameDuration"):
            from ..stockify.core import parse_time

            parsed = parse_time(format_resource.get("frameDuration"))
            if parsed > 0:
                frame_duration = parsed
        compilation_name = None
        metadata_rows = ReviewShardService._project_metadata(first_project)
        for row in metadata_rows:
            compilation_name = row.get("com.vclip.generated_compilation_name")
            if compilation_name:
                break
        if not compilation_name:
            compilation_name = f"{unit.source_project_id} — Stock Compilation"
        project = ET.Element(
            "project",
            {
                "name": compilation_name,
                "uid": stable_uid("review-scope", unit.source_project_id),
            },
        )
        sequence = ET.SubElement(
            project,
            "sequence",
            {
                "format": format_id,
                "duration": format_time(frame_duration),
                "tcStart": "0s",
                "tcFormat": source_sequence.get("tcFormat", "NDF"),
                "audioLayout": source_sequence.get("audioLayout", "stereo"),
                "audioRate": source_sequence.get("audioRate", "48k"),
            },
        )
        spine = ET.SubElement(sequence, "spine")
        ET.SubElement(
            spine,
            "gap",
            {
                "name": "VClip Review Scope — keep until reviewed XML export",
                "offset": "0s",
                "start": "0s",
                "duration": format_time(frame_duration),
            },
        )
        return project

    @staticmethod
    def _resource_closure(
        projects: Iterable[ET.Element],
        source_resources: ET.Element,
    ) -> set[str]:
        resource_index = build_resource_index(source_resources)
        known = set(resource_index)

        def refs_in(element: ET.Element) -> set[str]:
            refs: set[str] = set()
            for node in element.iter():
                for value in node.attrib.values():
                    if value in known:
                        refs.add(value)
            return refs

        required: set[str] = set()
        for project in projects:
            required.update(refs_in(project))
        pending = list(required)
        while pending:
            resource_id = pending.pop()
            resource = resource_index.get(resource_id)
            if resource is None:
                continue
            for dependency in refs_in(resource):
                if dependency not in required:
                    required.add(dependency)
                    pending.append(dependency)
        return required

    @staticmethod
    def _shard_manifest(
        *,
        shard_id: str,
        run_id: str,
        source_xml: Path,
        source_sha: str,
        market_id: str,
        market_label: str,
        part: int,
        path: Path,
        rendered: _RenderedShard,
        projects: list[ShardProject],
        grouping: str,
        representation: str,
        include_scope_markers: bool,
    ) -> dict[str, Any]:
        return {
            "manifest_version": 1,
            "created_at": utc_now(),
            "shard_id": shard_id,
            "stockify_run_id": run_id,
            "source_review_xml": str(source_xml),
            "source_review_sha256": source_sha,
            "output_fcpxml": str(path),
            "grouping": grouping,
            "representation": representation,
            "market": {"id": market_id, "label": market_label},
            "part": part,
            "project_count": len(projects),
            "scope_project_count": rendered.scope_project_count,
            "source_project_ids": list(rendered.source_project_ids),
            "stock_clip_ids": list(rendered.stock_clip_ids),
            "projects": [
                {
                    "event_name": project.event_name,
                    "project_name": project.project_name,
                    "project_uid": project.project_uid,
                    "representation": project.representation,
                    "source_project_id": project.source_project_id,
                    "stock_clip_ids": list(project.stock_clip_ids),
                }
                for project in projects
            ],
            "resource_ids": list(rendered.resource_ids),
            "include_scope_markers": include_scope_markers,
            "reconcile": {
                "authority": "individual" if representation != "compilation" else "compilation",
                "scope": "observed-projects",
                "command": (
                    f"vclip reconcile <reviewed-shard.fcpxml> --db <vclip.sqlite3> "
                    f"--run-id {run_id} --scope observed-projects"
                ),
            },
        }

    def _announce(self, message: str) -> None:
        if self.progress:
            self.progress(message)

    @staticmethod
    def _write_report(path: Path | None, report: ReviewShardReport) -> None:
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(report), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
