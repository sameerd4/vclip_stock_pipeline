"""Recover Unknown Location labels in a final review shard corpus from SRT GPS."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import uuid4

from ..db.repository import CatalogRepository
from ..errors import VClipError
from ..geo import LocationResolver, build_location_resolver, default_places_path, resolve_place
from ..stockify.core import local_name
from ..stockify.fcpxml import (
    first_direct_child,
    parse_source,
    read_vclip_metadata,
    validate_fcpxml,
)
from ..stockify.flight_location import (
    GEO_CLUSTER_SEPARATION_METERS,
    cluster_source_points,
)
from ..stockify.location_recovery import _relabel_project
from ..stockify.metadata import extract_gps_summary, is_usable_gps
from ..stockify.naming import event_base_name, project_base_label
from ..stockify.jpg_exif_same_shoot import (
    EVIDENCE_SOURCE as JPG_EXIF_EVIDENCE_SOURCE,
    index_jpg_photos,
    infer_jpg_exif_same_shoot,
    parse_dji_file_identity,
)
from ..stockify.sidecars import normalized_stem, parse_srt_info
from ..util import json_dumps, safe_filename, utc_now
from .catalog import WorkflowCatalog
from .editorial_group_forensics import (
    EDITORIAL_CONSENSUS_EVIDENCE,
    analyze_editorial_groups,
    build_source_geo_evidence,
    fill_missing_place_labels,
)
from .unresolved_evidence_dossier import build_unresolved_evidence_dossiers

CONFIDENCE = "confirmed_gps"
RECOVERY_REASON = "srt_gps_review_recovery"
OVERRIDE_REASON = "manual_gps_override"
JPG_EXIF_REASON = "jpg_exif_same_shoot"
STATUS_AUTOMATIC = "automatic_reverse_geocode"
STATUS_OVERRIDE = "manual_gps_override"
STATUS_UNRESOLVED = "unresolved_gps_cluster"
STATUS_FORENSIC_JPG = "forensic_jpg_exif_same_shoot"
_UNKNOWN_LABELS = frozenset(
    {"", "unknown", "unknown location", "unknown place", "none"}
)


@dataclass
class LocationRecoveryRow:
    stockify_run_id: str
    stock_clip_id: str
    original_event_name: str
    new_event_name: str
    original_project_name: str
    new_project_name: str
    source_media: str | None
    srt_paths: list[str]
    representative_lat: float | None
    representative_lon: float | None
    resolution_confidence: str
    recovery_reason: str
    source_shard: str
    input_xml: str
    output_xml: str | None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReviewLocationRecoverReport:
    input_root: str
    output_root: str
    dry_run: bool
    media_roots: list[str] = field(default_factory=list)
    unknown_events_before: int = 0
    unknown_clips_before: int = 0
    events_with_recoverable_gps: int = 0
    clips_with_recoverable_gps: int = 0
    homogeneous_events: int = 0
    mixed_location_events: int = 0
    recovered_geographic_clusters: int = 0
    candidates_moved_or_relabelled: int = 0
    unknown_events_after: int = 0
    unknown_clips_after: int = 0
    shards_changed: int = 0
    shards_unchanged: int = 0
    shards_failed: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    recoveries: list[dict[str, Any]] = field(default_factory=list)
    clusters: list[dict[str, Any]] = field(default_factory=list)
    location_overrides_path: str | None = None
    overrides_applied: int = 0
    overrides_unused: int = 0
    post_write_audit: dict[str, Any] = field(default_factory=dict)
    forensic_jpg_exif: bool = False
    jpg_exif_forensic: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_root": self.input_root,
            "output_root": self.output_root,
            "dry_run": self.dry_run,
            "media_roots": list(self.media_roots),
            "location_overrides_path": self.location_overrides_path,
            "forensic_jpg_exif": self.forensic_jpg_exif,
            "unknown_events_before": self.unknown_events_before,
            "unknown_clips_before": self.unknown_clips_before,
            "events_with_recoverable_gps": self.events_with_recoverable_gps,
            "clips_with_recoverable_gps": self.clips_with_recoverable_gps,
            "homogeneous_events": self.homogeneous_events,
            "mixed_location_events": self.mixed_location_events,
            "recovered_geographic_clusters": self.recovered_geographic_clusters,
            "candidates_moved_or_relabelled": self.candidates_moved_or_relabelled,
            "overrides_applied": self.overrides_applied,
            "overrides_unused": self.overrides_unused,
            "unknown_events_after": self.unknown_events_after,
            "unknown_clips_after": self.unknown_clips_after,
            "shards_changed": self.shards_changed,
            "shards_unchanged": self.shards_unchanged,
            "shards_failed": self.shards_failed,
            "failures": list(self.failures),
            "warnings": list(self.warnings),
            "recoveries": list(self.recoveries),
            "clusters": list(self.clusters),
            "post_write_audit": dict(self.post_write_audit),
            "jpg_exif_forensic": dict(self.jpg_exif_forensic),
        }


class ReviewLocationRecoverService:
    """Recover Unknown Location labels in final shard XML from SRT GPS."""

    def __init__(
        self,
        repository: CatalogRepository,
        location_resolver: LocationResolver,
        catalog: WorkflowCatalog | None = None,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.repository = repository
        self.location_resolver = location_resolver
        self.catalog = catalog or WorkflowCatalog(repository.database)
        self.progress = progress

    def run(
        self,
        *,
        input_root: Path,
        output_root: Path | None,
        media_roots: Iterable[Path],
        report_path: Path,
        text_report_path: Path,
        dry_run: bool = False,
        overwrite: bool = False,
        location_overrides: Path | None = None,
        forensic_jpg_exif: bool = False,
    ) -> ReviewLocationRecoverReport:
        input_root = input_root.expanduser().resolve()
        output_root_resolved = (
            output_root.expanduser().resolve() if output_root is not None else input_root
        )
        report_path = report_path.expanduser().resolve()
        text_report_path = text_report_path.expanduser().resolve()
        media_roots = [Path(root).expanduser().resolve() for root in media_roots]
        overrides_path = (
            location_overrides.expanduser().resolve() if location_overrides else None
        )
        if not input_root.is_dir():
            raise VClipError(f"Input root not found: {input_root}")
        if not media_roots:
            raise VClipError("At least one --media-root is required")
        if forensic_jpg_exif:
            return self._run_forensic_jpg_exif(
                input_root=input_root,
                media_roots=media_roots,
                report_path=report_path,
                text_report_path=text_report_path,
            )
        if output_root is None:
            raise VClipError("--output-root is required unless --forensic-jpg-exif")
        output_root = output_root_resolved
        if (
            output_root.exists()
            and any(output_root.iterdir())
            and not overwrite
            and not dry_run
        ):
            raise VClipError(
                f"Output root is not empty: {output_root} (pass --overwrite)"
            )

        override_index = (
            load_location_overrides(overrides_path) if overrides_path else None
        )

        self._announce(f"Scanning final shard corpus: {input_root}")
        shard_entries = self._discover_shards(input_root)
        appearances = self._collect_appearances(shard_entries)
        pairs = {(item["stockify_run_id"], item["stock_clip_id"]) for item in appearances}
        rows = self.repository.candidates_by_run_and_ids(pairs)

        unknown_appearances: list[dict[str, Any]] = []
        known_keys: set[tuple[str, str]] = set()
        for appearance in appearances:
            key = (appearance["stockify_run_id"], appearance["stock_clip_id"])
            row = rows.get(key)
            if row is None:
                continue
            appearance["row"] = row
            appearance["source_basename"] = _source_basename(row)
            if _is_unknown_candidate(row, appearance.get("event_name")):
                unknown_appearances.append(appearance)
            else:
                known_keys.add(key)

        unknown_events_before = {
            (item["stockify_run_id"], item["event_name"])
            for item in unknown_appearances
            if item.get("event_name")
        }
        report = ReviewLocationRecoverReport(
            input_root=str(input_root),
            output_root=str(output_root),
            dry_run=dry_run,
            media_roots=[str(root) for root in media_roots],
            location_overrides_path=str(overrides_path) if overrides_path else None,
            unknown_events_before=len(unknown_events_before),
            unknown_clips_before=len(
                {(item["stockify_run_id"], item["stock_clip_id"]) for item in unknown_appearances}
            ),
        )

        needed_stems = {
            normalized_stem(item["source_basename"])
            for item in unknown_appearances
            if item.get("source_basename")
        }
        needed_stems.discard("")
        self._announce(f"Indexing SRT sidecars for {len(needed_stems)} source stem(s)")
        srt_index = _index_srts_by_basename(media_roots, needed_stems)
        source_observations = self._source_observations(unknown_appearances, srt_index)

        recoveries, event_stats, clusters, plan_warnings = self._plan_recoveries(
            unknown_appearances,
            source_observations=source_observations,
            output_root=output_root,
            override_index=override_index,
        )
        report.events_with_recoverable_gps = event_stats["events_with_recoverable_gps"]
        report.clips_with_recoverable_gps = event_stats["clips_with_recoverable_gps"]
        report.homogeneous_events = event_stats["homogeneous_events"]
        report.mixed_location_events = event_stats["mixed_location_events"]
        report.recovered_geographic_clusters = event_stats["recovered_geographic_clusters"]
        report.overrides_applied = event_stats["overrides_applied"]
        report.overrides_unused = event_stats["overrides_unused"]
        report.candidates_moved_or_relabelled = len(recoveries)
        report.recoveries = [asdict(item) for item in recoveries]
        report.clusters = clusters
        report.warnings.extend(plan_warnings)

        by_shard: dict[str, list[LocationRecoveryRow]] = defaultdict(list)
        for item in recoveries:
            by_shard[item.source_shard].append(item)

        if dry_run:
            self._announce(
                f"Dry run: would recover {len(recoveries)} candidate(s) "
                f"across {report.recovered_geographic_clusters} cluster(s)."
            )
            report.shards_changed = len(by_shard)
            report.shards_unchanged = max(0, len(shard_entries) - len(by_shard))
            report.post_write_audit = self._hypothetical_audit(
                appearances=appearances,
                recoveries=recoveries,
                known_keys=known_keys,
            )
            applied = recoveries
        else:
            output_root.mkdir(parents=True, exist_ok=True)
            changed, unchanged, failures = self._write_corpus(
                shard_entries=shard_entries,
                output_root=output_root,
                recoveries_by_shard=by_shard,
                overwrite=overwrite,
            )
            report.shards_changed = changed
            report.shards_unchanged = unchanged
            report.shards_failed = len(failures)
            report.failures.extend(failures)
            failed_shards = {item["relative_path"] for item in failures}
            persisted = [
                item for item in recoveries if item.source_shard not in failed_shards
            ]
            for item in persisted:
                item.output_xml = str(output_root / item.source_shard)
            self._persist_candidate_updates(persisted)
            self.catalog.record_review_location_recoveries(recoveries=persisted)
            report.recoveries = [asdict(item) for item in persisted]
            report.candidates_moved_or_relabelled = len(persisted)
            report.post_write_audit = self._post_write_audit(
                input_root=input_root,
                output_root=output_root,
                recoveries=persisted,
                known_keys=known_keys,
                appearances=appearances,
            )
            applied = persisted

        recovered_keys = {
            (item.stockify_run_id, item.stock_clip_id) for item in applied
        }
        remaining_unknown = [
            item
            for item in unknown_appearances
            if (item["stockify_run_id"], item["stock_clip_id"]) not in recovered_keys
        ]
        report.unknown_clips_after = len(
            {
                (item["stockify_run_id"], item["stock_clip_id"])
                for item in remaining_unknown
            }
        )
        report.unknown_events_after = len(
            {
                (item["stockify_run_id"], item["event_name"])
                for item in remaining_unknown
                if item.get("event_name")
            }
        )

        self._write_reports(
            report, report_path=report_path, text_report_path=text_report_path
        )
        return report

    def _run_forensic_jpg_exif(
        self,
        *,
        input_root: Path,
        media_roots: list[Path],
        report_path: Path,
        text_report_path: Path,
    ) -> ReviewLocationRecoverReport:
        """Read-only JPG EXIF same-shoot forensic pass for SRT-less Unknown sources."""
        self._announce(
            f"Forensic JPG EXIF same-shoot scan (read-only): {input_root}"
        )
        shard_entries = self._discover_shards(input_root)
        appearances = self._collect_appearances(shard_entries)
        pairs = {(item["stockify_run_id"], item["stock_clip_id"]) for item in appearances}
        rows = self.repository.candidates_by_run_and_ids(pairs)

        unknown_appearances: list[dict[str, Any]] = []
        for appearance in appearances:
            key = (appearance["stockify_run_id"], appearance["stock_clip_id"])
            row = rows.get(key)
            if row is None:
                continue
            appearance["row"] = row
            appearance["source_basename"] = _source_basename(row)
            if _is_unknown_candidate(row, appearance.get("event_name")):
                unknown_appearances.append(appearance)

        unknown_events_before = {
            (item["stockify_run_id"], item["event_name"])
            for item in unknown_appearances
            if item.get("event_name")
        }
        report = ReviewLocationRecoverReport(
            input_root=str(input_root),
            output_root=str(input_root),
            dry_run=True,
            media_roots=[str(root) for root in media_roots],
            forensic_jpg_exif=True,
            unknown_events_before=len(unknown_events_before),
            unknown_clips_before=len(
                {
                    (item["stockify_run_id"], item["stock_clip_id"])
                    for item in unknown_appearances
                }
            ),
            unknown_events_after=len(unknown_events_before),
            unknown_clips_after=len(
                {
                    (item["stockify_run_id"], item["stock_clip_id"])
                    for item in unknown_appearances
                }
            ),
        )

        needed_stems = {
            normalized_stem(item["source_basename"])
            for item in unknown_appearances
            if item.get("source_basename")
        }
        needed_stems.discard("")
        self._announce(f"Indexing SRT sidecars for {len(needed_stems)} source stem(s)")
        srt_index = _index_srts_by_basename(media_roots, needed_stems)
        srt_observations = self._source_observations(unknown_appearances, srt_index)

        # Only sources still lacking usable SRT GPS are JPG-forensic candidates.
        no_srt_sources: dict[str, dict[str, Any]] = {}
        for appearance in unknown_appearances:
            stem = normalized_stem(appearance.get("source_basename"))
            if not stem or stem in srt_observations or stem in no_srt_sources:
                continue
            identity = parse_dji_file_identity(appearance.get("source_basename"))
            no_srt_sources[stem] = {
                "source_basename": appearance.get("source_basename"),
                "stem": stem,
                "media_path": (appearance.get("row") or {}).get("source_media_path"),
                "identity": identity,
                "appearances": [],
            }
        for appearance in unknown_appearances:
            stem = normalized_stem(appearance.get("source_basename"))
            if stem in no_srt_sources:
                no_srt_sources[stem]["appearances"].append(appearance)

        needed_dates = {
            item["identity"].date
            for item in no_srt_sources.values()
            if item.get("identity") is not None and item["identity"].date
        }
        self._announce(
            f"Indexing JPG/JPEG stills for {len(needed_dates)} capture date(s) "
            f"across {len(no_srt_sources)} SRT-less source(s)"
        )
        jpg_index = index_jpg_photos(media_roots, needed_dates=needed_dates or None)

        jpg_observations: dict[str, dict[str, Any]] = {}
        inferences: list[dict[str, Any]] = []
        for stem, source in sorted(no_srt_sources.items()):
            inference = infer_jpg_exif_same_shoot(
                str(source["source_basename"] or stem),
                jpg_index=jpg_index,
                media_path=str(source["media_path"]) if source.get("media_path") else None,
            )
            if inference is None:
                continue
            observation = inference.as_source_observation()
            jpg_observations[stem] = observation
            inferences.append(inference.as_dict())

        # Reuse geographic clustering + reverse-geocode on JPG-inferred coordinates.
        recoveries, event_stats, clusters, plan_warnings = self._plan_jpg_forensic(
            unknown_appearances,
            jpg_observations=jpg_observations,
        )
        report.events_with_recoverable_gps = event_stats["events_with_recoverable_gps"]
        report.clips_with_recoverable_gps = event_stats["clips_with_recoverable_gps"]
        report.homogeneous_events = event_stats["homogeneous_events"]
        report.mixed_location_events = event_stats["mixed_location_events"]
        report.recovered_geographic_clusters = event_stats["recovered_geographic_clusters"]
        report.clusters = clusters
        report.warnings.extend(plan_warnings)
        report.recoveries = [asdict(item) for item in recoveries]
        report.candidates_moved_or_relabelled = 0  # forensic: never mutates

        by_confidence = Counter(
            item.get("confidence") or "unknown" for item in inferences
        )
        by_event = Counter()
        for appearance in unknown_appearances:
            stem = normalized_stem(appearance.get("source_basename"))
            if stem in jpg_observations:
                by_event[appearance.get("event_name") or "Unknown Location"] += 1

        nov8_sources = [
            item
            for item in inferences
            if "2025-11-08"
            in str((item.get("source_identity") or {}).get("date") or "")
            or "20251108" in str(item.get("source_basename") or "")
        ]

        self._announce("Building source-level evidence + editorial-group consensus")
        source_evidence = build_source_geo_evidence(
            unknown_appearances=unknown_appearances,
            srt_observations=srt_observations,
            jpg_observations=jpg_observations,
            location_resolver=self.location_resolver,
        )

        # Batch reverse-geocode JPG/SRT coords that still lack place labels.
        # Always enable Nominatim for this retry so catalog-only forensic runs
        # can still label out-of-catalog points (GPS provenance unchanged).
        self._announce("Retrying reverse-geocode for GPS evidence lacking place labels")
        label_resolver = build_location_resolver(
            self.repository,
            places_path=default_places_path(),
            enable_nominatim=True,
        )
        place_label_retry = fill_missing_place_labels(source_evidence, label_resolver)

        editorial_groups, editorial_summary = analyze_editorial_groups(
            unknown_appearances=unknown_appearances,
            source_evidence=source_evidence,
        )

        # Expand JPG index dates for unresolved dossier enumeration.
        unresolved_key_set = {
            (entry["stockify_run_id"], entry["stock_clip_id"])
            for entry in editorial_summary.get("still_fully_unresolved_keys") or []
        }
        unresolved_dates: set[str] = set()
        for item in unknown_appearances:
            if (item["stockify_run_id"], item["stock_clip_id"]) not in unresolved_key_set:
                continue
            identity = parse_dji_file_identity(item.get("source_basename"))
            if identity and identity.date:
                unresolved_dates.add(identity.date)
        dossier_dates = set(needed_dates) | unresolved_dates
        if dossier_dates - needed_dates:
            self._announce(
                f"Indexing additional JPG dates for unresolved dossiers: "
                f"{sorted(dossier_dates - needed_dates)}"
            )
            jpg_index = index_jpg_photos(media_roots, needed_dates=dossier_dates or None)

        self._announce("Building unresolved-event evidence dossiers")
        unresolved_dossiers = build_unresolved_evidence_dossiers(
            unknown_appearances=unknown_appearances,
            source_evidence=source_evidence,
            editorial_summary=editorial_summary,
            jpg_index=jpg_index,
            media_roots=media_roots,
            repository=self.repository,
        )

        report.jpg_exif_forensic = {
            "mode": "read_only_forensic",
            "evidence_source": JPG_EXIF_EVIDENCE_SOURCE,
            "note": (
                "JPG-derived coordinates are inferred same-shoot evidence, "
                "never labeled as direct source GPS. Editorial-group labels are "
                "separate consensus context via "
                f"{EDITORIAL_CONSENSUS_EVIDENCE}."
            ),
            "unknown_sources_without_srt_gps": len(no_srt_sources),
            "sources_with_jpg_inference": len(inferences),
            "clips_covered_by_jpg_inference": event_stats["clips_with_recoverable_gps"],
            "confidence_counts": dict(by_confidence),
            "review_required_sources": sum(
                1 for item in inferences if item.get("review_required")
            ),
            "events_with_jpg_evidence": dict(by_event),
            "focus_unknown_location_2025_11_08": {
                "sources": len(nov8_sources),
                "inferences": nov8_sources,
            },
            "source_inferences": inferences,
            "source_level_evidence": [
                source_evidence[stem].as_dict() for stem in sorted(source_evidence)
            ],
            "place_label_retry": place_label_retry,
            "hypothetical_recoveries": [asdict(item) for item in recoveries],
            "editorial_groups": [item.as_dict() for item in editorial_groups],
            "editorial_group_summary": editorial_summary,
            "unresolved_evidence_dossiers": unresolved_dossiers,
        }
        self._announce(
            f"Forensic JPG EXIF: inferred {len(inferences)} / "
            f"{len(no_srt_sources)} SRT-less source(s); "
            f"editorial groups={len(editorial_groups)}; "
            f"place-label retries labeled="
            f"{place_label_retry.get('labeled_sources', 0)}; "
            f"unresolved dossiers="
            f"{unresolved_dossiers.get('unresolved_events', 0)}; "
            f"group-consensus inheritors="
            f"{editorial_summary.get('additional_clips_eligible_for_group_consensus', 0)}; "
            f"no XML/DB mutation."
        )
        self._write_reports(
            report, report_path=report_path, text_report_path=text_report_path
        )
        return report

    def _plan_jpg_forensic(
        self,
        unknown_appearances: list[dict[str, Any]],
        *,
        jpg_observations: dict[str, dict[str, Any]],
    ) -> tuple[
        list[LocationRecoveryRow],
        dict[str, int],
        list[dict[str, Any]],
        list[str],
    ]:
        """Cluster/reverse-geocode JPG-inferred coords without mutating corpus."""
        by_event: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for appearance in unknown_appearances:
            stem = normalized_stem(appearance.get("source_basename"))
            if stem not in jpg_observations:
                continue
            event_name = appearance.get("event_name") or "Unknown Location"
            by_event[(appearance["stockify_run_id"], event_name)].append(appearance)

        recoveries: list[LocationRecoveryRow] = []
        cluster_rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        events_with_gps = 0
        clips_with_gps = 0
        homogeneous = 0
        mixed = 0
        clusters_total = 0

        for (run_id, event_name), members in sorted(by_event.items()):
            stems = sorted(
                {
                    normalized_stem(item.get("source_basename"))
                    for item in members
                    if normalized_stem(item.get("source_basename")) in jpg_observations
                }
            )
            if not stems:
                continue
            events_with_gps += 1
            gps_members = [
                item
                for item in members
                if normalized_stem(item.get("source_basename")) in jpg_observations
            ]
            clips_with_gps += len(
                {(item["stockify_run_id"], item["stock_clip_id"]) for item in gps_members}
            )
            source_points = [
                (
                    stem,
                    float(jpg_observations[stem]["lat"]),
                    float(jpg_observations[stem]["lon"]),
                )
                for stem in stems
            ]
            clusters = cluster_source_points(
                source_points,
                separation_meters=GEO_CLUSTER_SEPARATION_METERS,
            )
            is_mixed_event = len(clusters) > 1
            if is_mixed_event:
                mixed += 1
            else:
                homogeneous += 1
            clusters_total += len(clusters)
            capture_date = _event_capture_date(event_name, gps_members)

            for cluster in clusters:
                center_lat = float(cluster.center_lat)
                center_lon = float(cluster.center_lon)
                cluster_key = geographic_cluster_id(event_name, center_lat, center_lon)
                cluster_stems = set(cluster.source_keys)
                cluster_members = [
                    item
                    for item in gps_members
                    if normalized_stem(item.get("source_basename")) in cluster_stems
                ]
                stock_clip_ids = sorted(
                    {str(item["stock_clip_id"]) for item in cluster_members}
                )
                source_names = sorted(
                    {
                        str(item.get("source_basename") or "")
                        for item in cluster_members
                        if item.get("source_basename")
                    }
                )
                shard_paths = sorted(
                    {
                        str(item.get("relative_xml") or "")
                        for item in cluster_members
                        if item.get("relative_xml")
                    }
                )
                sample_count = sum(
                    int(jpg_observations[stem]["sample_count"]) for stem in cluster_stems
                )
                confidences = {
                    jpg_observations[stem].get("resolution_confidence") or "low"
                    for stem in cluster_stems
                }
                review_required = any(
                    jpg_observations[stem].get("review_required") for stem in cluster_stems
                ) or ("high" not in confidences)
                cluster_confidence = (
                    "high"
                    if confidences == {"high"}
                    else ("medium" if "medium" in confidences or "high" in confidences else "low")
                )

                place = resolve_place(
                    self.location_resolver,
                    center_lat,
                    center_lon,
                )
                location = None
                resolution_status = STATUS_UNRESOLVED
                if place is not None:
                    location = _structured_location(
                        place,
                        center_lat=center_lat,
                        center_lon=center_lon,
                        sample_count=sample_count,
                        evidence_sources=[
                            JPG_EXIF_EVIDENCE_SOURCE,
                            "review_location_recover_forensic",
                        ],
                        recovery_method=JPG_EXIF_REASON,
                        place_provider=place.get("provider"),
                        confidence=cluster_confidence,
                        review_required=review_required,
                        gps_kind="inferred_jpg_exif_same_shoot",
                    )
                    resolution_status = STATUS_FORENSIC_JPG

                resolved_location = (
                    str(location.get("public_label") or "") or None
                    if location is not None
                    else None
                )
                cluster_rows.append(
                    _cluster_report_row(
                        cluster_id=cluster_key,
                        original_event=event_name,
                        stock_clip_ids=stock_clip_ids,
                        source_names=source_names,
                        shard_paths=shard_paths,
                        center_lat=center_lat,
                        center_lon=center_lon,
                        source_count=int(cluster.source_count),
                        mixed_location_event=is_mixed_event,
                        resolution_status=resolution_status,
                        resolved_location=resolved_location,
                        resolved_city=(
                            str(location.get("city"))
                            if location and location.get("city")
                            else None
                        ),
                        resolved_region=(
                            str(location.get("region") or location.get("state"))
                            if location
                            and (location.get("region") or location.get("state"))
                            else None
                        ),
                        resolved_country=(
                            str(location.get("country"))
                            if location and location.get("country")
                            else None
                        ),
                        extra={
                            "evidence_source": JPG_EXIF_EVIDENCE_SOURCE,
                            "resolution_confidence": cluster_confidence,
                            "review_required": review_required,
                            "gps_kind": "inferred_jpg_exif_same_shoot",
                        },
                    )
                )
                if location is None:
                    continue

                new_event = event_base_name(
                    location, {"date": capture_date or "Unknown Date"}
                )
                for member in cluster_members:
                    stem = normalized_stem(member.get("source_basename"))
                    observation = jpg_observations[stem]
                    jpg_payload = observation.get("jpg_exif_same_shoot") or {}
                    row = member["row"]
                    time_of_day = row.get("time_of_day") or {"label": "unknown"}
                    if isinstance(time_of_day, str):
                        time_of_day = {"label": time_of_day}
                    new_base = project_base_label(location, time_of_day)
                    old_project = str(member["project_name"])
                    old_label = str(
                        row.get("generated_project_label")
                        or old_project.split(" — Clip ")[0]
                    )
                    new_label = _relabel_project(old_label, new_base)
                    if " — Clip " in old_project:
                        suffix = old_project.split(" — Clip ", 1)[1]
                        new_project = safe_filename(f"{new_label} — Clip {suffix}")
                    else:
                        new_project = safe_filename(new_label)
                    recoveries.append(
                        LocationRecoveryRow(
                            stockify_run_id=run_id,
                            stock_clip_id=member["stock_clip_id"],
                            original_event_name=event_name,
                            new_event_name=new_event,
                            original_project_name=old_project,
                            new_project_name=new_project,
                            source_media=observation.get("source_basename"),
                            srt_paths=[],
                            representative_lat=center_lat,
                            representative_lon=center_lon,
                            resolution_confidence=str(
                                observation.get("resolution_confidence")
                                or cluster_confidence
                            ),
                            recovery_reason=JPG_EXIF_REASON,
                            source_shard=member["relative_xml"],
                            input_xml=str(member["xml_path"]),
                            output_xml=None,
                            provenance={
                                "original_event": event_name,
                                "new_event": new_event,
                                "source_media": observation.get("source_basename"),
                                "srt_paths": [],
                                "representative_gps": {
                                    "lat": center_lat,
                                    "lon": center_lon,
                                    "kind": "inferred_jpg_exif_same_shoot",
                                },
                                "resolution_confidence": observation.get(
                                    "resolution_confidence"
                                ),
                                "review_required": bool(
                                    observation.get("review_required")
                                ),
                                "recovery_reason": JPG_EXIF_REASON,
                                "resolution_status": resolution_status,
                                "evidence_sources": [
                                    JPG_EXIF_EVIDENCE_SOURCE,
                                    "review_location_recover_forensic",
                                ],
                                "jpg_exif_same_shoot": jpg_payload,
                                "location": location,
                                "cluster_id": cluster_key,
                                "capture_date": capture_date,
                                "forensic_only": True,
                                "mutates_corpus": False,
                            },
                        )
                    )

        return (
            recoveries,
            {
                "events_with_recoverable_gps": events_with_gps,
                "clips_with_recoverable_gps": clips_with_gps,
                "homogeneous_events": homogeneous,
                "mixed_location_events": mixed,
                "recovered_geographic_clusters": clusters_total,
                "overrides_applied": 0,
                "overrides_unused": 0,
            },
            cluster_rows,
            warnings,
        )

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

    def _collect_appearances(
        self, shard_entries: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        appearances: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for entry in shard_entries:
            manifest = entry["manifest"]
            run_id = str(manifest.get("stockify_run_id") or "")
            if not run_id:
                continue
            for project in manifest.get("projects") or []:
                if project.get("representation") == "compilation":
                    continue
                if "Stock Compilation" in str(project.get("project_name") or ""):
                    continue
                for clip_id in project.get("stock_clip_ids") or []:
                    clip_id = str(clip_id)
                    key = (run_id, clip_id, entry["relative_xml"])
                    if key in seen:
                        continue
                    seen.add(key)
                    appearances.append(
                        {
                            "stockify_run_id": run_id,
                            "stock_clip_id": clip_id,
                            "project_name": str(
                                project.get("project_name") or clip_id
                            ),
                            "event_name": str(project.get("event_name") or ""),
                            "relative_xml": entry["relative_xml"],
                            "xml_path": entry["xml_path"],
                            "manifest_path": entry["manifest_path"],
                            "manifest": manifest,
                        }
                    )
        return appearances

    def _source_observations(
        self,
        unknown_appearances: list[dict[str, Any]],
        srt_index: dict[str, list[Path]],
    ) -> dict[str, dict[str, Any]]:
        """One observation per source basename (duplicate SRT copies collapse)."""
        observations: dict[str, dict[str, Any]] = {}
        for appearance in unknown_appearances:
            basename = appearance.get("source_basename") or ""
            stem = normalized_stem(basename)
            if not stem or stem in observations:
                continue
            paths = sorted({path.resolve() for path in srt_index.get(stem, [])})
            summary = None
            chosen: list[str] = []
            for path in paths:
                try:
                    info = parse_srt_info(path)
                except OSError:
                    continue
                summary = extract_gps_summary(info, allow_full_sidecar_fallback=True)
                if summary is None:
                    continue
                if not is_usable_gps(summary.get("center_lat"), summary.get("center_lon")):
                    continue
                chosen = [str(path)]
                # Duplicate copies for the same basename are not additional votes.
                break
            if summary is None or not chosen:
                continue
            observations[stem] = {
                "source_basename": basename,
                "stem": stem,
                "lat": float(summary["center_lat"]),
                "lon": float(summary["center_lon"]),
                "sample_count": int(summary.get("sample_count") or 1),
                "srt_paths": chosen + [str(path) for path in paths if str(path) not in chosen],
            }
        return observations

    def _plan_recoveries(
        self,
        unknown_appearances: list[dict[str, Any]],
        *,
        source_observations: dict[str, dict[str, Any]],
        output_root: Path,
        override_index: "LocationOverrideIndex | None" = None,
    ) -> tuple[
        list[LocationRecoveryRow],
        dict[str, int],
        list[dict[str, Any]],
        list[str],
    ]:
        by_event: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for appearance in unknown_appearances:
            event_name = appearance.get("event_name") or "Unknown Location"
            by_event[(appearance["stockify_run_id"], event_name)].append(appearance)

        recoveries: list[LocationRecoveryRow] = []
        cluster_rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        events_with_gps = 0
        clips_with_gps = 0
        homogeneous = 0
        mixed = 0
        clusters_total = 0
        overrides_applied = 0
        used_event_names: set[str] = set()
        matched_override_indexes: set[int] = set()

        for (run_id, event_name), members in sorted(by_event.items()):
            stems = sorted(
                {
                    normalized_stem(item.get("source_basename"))
                    for item in members
                    if normalized_stem(item.get("source_basename"))
                    in source_observations
                }
            )
            if not stems:
                continue
            events_with_gps += 1
            gps_members = [
                item
                for item in members
                if normalized_stem(item.get("source_basename")) in source_observations
            ]
            clips_with_gps += len(
                {(item["stockify_run_id"], item["stock_clip_id"]) for item in gps_members}
            )
            source_points = [
                (
                    stem,
                    float(source_observations[stem]["lat"]),
                    float(source_observations[stem]["lon"]),
                )
                for stem in stems
            ]
            # Split mixed Unknown events geographically before any override lookup.
            clusters = cluster_source_points(
                source_points,
                separation_meters=GEO_CLUSTER_SEPARATION_METERS,
            )
            is_mixed_event = len(clusters) > 1
            if is_mixed_event:
                mixed += 1
            else:
                homogeneous += 1
            clusters_total += len(clusters)

            capture_date = _event_capture_date(event_name, gps_members)
            for cluster in clusters:
                center_lat = float(cluster.center_lat)
                center_lon = float(cluster.center_lon)
                cluster_key = geographic_cluster_id(event_name, center_lat, center_lon)
                cluster_stems = set(cluster.source_keys)
                cluster_members = [
                    item
                    for item in gps_members
                    if normalized_stem(item.get("source_basename")) in cluster_stems
                ]
                stock_clip_ids = sorted(
                    {str(item["stock_clip_id"]) for item in cluster_members}
                )
                source_names = sorted(
                    {
                        str(item.get("source_basename") or "")
                        for item in cluster_members
                        if item.get("source_basename")
                    }
                )
                shard_paths = sorted(
                    {
                        str(item.get("relative_xml") or "")
                        for item in cluster_members
                        if item.get("relative_xml")
                    }
                )
                sample_count = sum(
                    int(source_observations[stem]["sample_count"])
                    for stem in cluster.source_keys
                )

                override_entry = None
                if override_index is not None:
                    override_entry = override_index.lookup(
                        cluster_id=cluster_key,
                        original_event=event_name,
                        latitude=center_lat,
                        longitude=center_lon,
                    )

                location: dict[str, Any] | None = None
                resolution_status = STATUS_UNRESOLVED
                recovery_reason = RECOVERY_REASON
                if override_entry is not None:
                    matched_override_indexes.add(override_entry.index)
                    location = _location_from_override(
                        override_entry.fields,
                        center_lat=center_lat,
                        center_lon=center_lon,
                        sample_count=sample_count,
                    )
                    resolution_status = STATUS_OVERRIDE
                    recovery_reason = OVERRIDE_REASON
                    overrides_applied += 1
                else:
                    place = resolve_place(
                        self.location_resolver,
                        center_lat,
                        center_lon,
                    )
                    if place is None:
                        self._announce(
                            f"No reverse-geocode for cluster {cluster_key} at "
                            f"{center_lat:.5f},{center_lon:.5f}"
                        )
                    else:
                        location = _location_from_place(
                            place,
                            center_lat=center_lat,
                            center_lon=center_lon,
                            sample_count=sample_count,
                        )
                        resolution_status = STATUS_AUTOMATIC
                        recovery_reason = RECOVERY_REASON

                resolved_location = (
                    str(location.get("public_label") or "") or None
                    if location is not None
                    else None
                )
                cluster_rows.append(
                    _cluster_report_row(
                        cluster_id=cluster_key,
                        original_event=event_name,
                        stock_clip_ids=stock_clip_ids,
                        source_names=source_names,
                        shard_paths=shard_paths,
                        center_lat=center_lat,
                        center_lon=center_lon,
                        source_count=int(cluster.source_count),
                        mixed_location_event=is_mixed_event,
                        resolution_status=resolution_status,
                        resolved_location=resolved_location,
                        resolved_city=(
                            str(location.get("city"))
                            if location and location.get("city")
                            else None
                        ),
                        resolved_region=(
                            str(location.get("region") or location.get("state"))
                            if location
                            and (location.get("region") or location.get("state"))
                            else None
                        ),
                        resolved_country=(
                            str(location.get("country"))
                            if location and location.get("country")
                            else None
                        ),
                    )
                )
                if location is None:
                    continue

                new_event = event_base_name(
                    location, {"date": capture_date or "Unknown Date"}
                )
                if new_event in used_event_names and len(clusters) > 1:
                    new_event = safe_filename(
                        f"{new_event} — Cluster {cluster.cluster_id}"
                    )
                used_event_names.add(new_event)

                for member in cluster_members:
                    stem = normalized_stem(member.get("source_basename"))
                    row = member["row"]
                    time_of_day = row.get("time_of_day") or {"label": "unknown"}
                    if isinstance(time_of_day, str):
                        time_of_day = {"label": time_of_day}
                    new_base = project_base_label(location, time_of_day)
                    old_project = str(member["project_name"])
                    old_label = str(
                        row.get("generated_project_label")
                        or old_project.split(" — Clip ")[0]
                    )
                    new_label = _relabel_project(old_label, new_base)
                    if " — Clip " in old_project:
                        suffix = old_project.split(" — Clip ", 1)[1]
                        new_project = safe_filename(f"{new_label} — Clip {suffix}")
                    else:
                        new_project = safe_filename(new_label)
                    observation = source_observations[stem]
                    recoveries.append(
                        LocationRecoveryRow(
                            stockify_run_id=run_id,
                            stock_clip_id=member["stock_clip_id"],
                            original_event_name=event_name,
                            new_event_name=new_event,
                            original_project_name=old_project,
                            new_project_name=new_project,
                            source_media=observation.get("source_basename"),
                            srt_paths=list(observation.get("srt_paths") or []),
                            representative_lat=center_lat,
                            representative_lon=center_lon,
                            resolution_confidence=CONFIDENCE,
                            recovery_reason=recovery_reason,
                            source_shard=member["relative_xml"],
                            input_xml=str(member["xml_path"]),
                            output_xml=str(output_root / member["relative_xml"]),
                            provenance={
                                "original_event": event_name,
                                "new_event": new_event,
                                "source_media": observation.get("source_basename"),
                                "srt_paths": list(observation.get("srt_paths") or []),
                                "representative_gps": {
                                    "lat": center_lat,
                                    "lon": center_lon,
                                },
                                "resolution_confidence": CONFIDENCE,
                                "recovery_reason": recovery_reason,
                                "resolution_status": resolution_status,
                                "location": location,
                                "cluster_id": cluster_key,
                                "geo_cluster_index": cluster.cluster_id,
                                "cluster_source_count": cluster.source_count,
                                "event_kind": (
                                    "homogeneous" if len(clusters) == 1 else "mixed"
                                ),
                                "generated_project_label": new_label,
                                "capture_date": capture_date,
                                "time_of_day": time_of_day,
                            },
                        )
                    )

        overrides_unused = 0
        if override_index is not None:
            for entry in override_index.entries:
                if entry.index in matched_override_indexes:
                    continue
                overrides_unused += 1
                target = entry.cluster_id or _override_target_label(entry)
                warnings.append(
                    f"Location override did not match any GPS cluster: {target}"
                )

        return (
            recoveries,
            {
                "events_with_recoverable_gps": events_with_gps,
                "clips_with_recoverable_gps": clips_with_gps,
                "homogeneous_events": homogeneous,
                "mixed_location_events": mixed,
                "recovered_geographic_clusters": clusters_total,
                "overrides_applied": overrides_applied,
                "overrides_unused": overrides_unused,
            },
            cluster_rows,
            warnings,
        )

    def _write_corpus(
        self,
        *,
        shard_entries: list[dict[str, Any]],
        output_root: Path,
        recoveries_by_shard: dict[str, list[LocationRecoveryRow]],
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
                shard_recoveries = recoveries_by_shard.get(relative, [])
                if not shard_recoveries:
                    shutil.copy2(entry["xml_path"], output_xml)
                    shutil.copy2(
                        entry["manifest_path"],
                        output_xml.with_name(f"{output_xml.stem}-shard-manifest.json"),
                    )
                    unchanged += 1
                    continue
                tree = parse_source(entry["xml_path"])
                root = tree.getroot()
                _apply_xml_recoveries(root, shard_recoveries)
                validation = validate_fcpxml(root)
                if not validation.passed:
                    raise VClipError(
                        "Located review XML failed FCPXML validation: "
                        + "; ".join(validation.errors[:10])
                    )
                ET.indent(root)
                output_xml.write_bytes(
                    ET.tostring(root, encoding="utf-8", xml_declaration=True)
                )
                _rewrite_manifest(
                    manifest=entry["manifest"],
                    output_xml=output_xml,
                    recoveries=shard_recoveries,
                )
                changed += 1
            except Exception as exc:  # noqa: BLE001
                failures.append({"relative_path": relative, "error": str(exc)})
                self._announce(f"FAILED {relative}: {exc}")
        return changed, unchanged, failures

    def _persist_candidate_updates(self, recoveries: list[LocationRecoveryRow]) -> None:
        now = utc_now()
        with self.repository.database.transaction() as connection:
            persist_review_location_candidate_updates(
                connection, recoveries, now=now
            )

    def _hypothetical_audit(
        self,
        *,
        appearances: list[dict[str, Any]],
        recoveries: list[LocationRecoveryRow],
        known_keys: set[tuple[str, str]],
    ) -> dict[str, Any]:
        rename = {
            (item.stockify_run_id, item.stock_clip_id): item for item in recoveries
        }
        seen: CounterLike = defaultdict(int)
        known_changed = []
        for appearance in appearances:
            key = (appearance["stockify_run_id"], appearance["stock_clip_id"])
            seen[key] += 1
            if key in known_keys and key in rename:
                known_changed.append(key)
        duplicates = [f"{run}:{clip}" for (run, clip), count in seen.items() if count != 1]
        return {
            "mode": "location_recover_dry_run_audit",
            "candidates_exactly_once": len(duplicates) == 0,
            "duplicate_or_missing_identities": duplicates[:50],
            "known_location_candidates_changed": [
                f"{run}:{clip}" for run, clip in known_changed
            ],
            "recovered_count": len(recoveries),
        }

    def _post_write_audit(
        self,
        *,
        input_root: Path,
        output_root: Path,
        recoveries: list[LocationRecoveryRow],
        known_keys: set[tuple[str, str]],
        appearances: list[dict[str, Any]],
    ) -> dict[str, Any]:
        output_entries = self._discover_shards(output_root)
        output_appearances = self._collect_appearances(output_entries)
        counts: dict[tuple[str, str], int] = defaultdict(int)
        output_names: dict[tuple[str, str], tuple[str, str]] = {}
        for item in output_appearances:
            key = (item["stockify_run_id"], item["stock_clip_id"])
            counts[key] += 1
            output_names[key] = (item.get("event_name") or "", item.get("project_name") or "")
        duplicates = [
            f"{run}:{clip}" for (run, clip), count in counts.items() if count != 1
        ]
        input_known = {
            (item["stockify_run_id"], item["stock_clip_id"]): (
                item.get("event_name") or "",
                item.get("project_name") or "",
            )
            for item in appearances
            if (item["stockify_run_id"], item["stock_clip_id"]) in known_keys
        }
        known_changed = []
        for key, names in input_known.items():
            if key not in output_names:
                known_changed.append(f"{key[0]}:{key[1]}:missing")
            elif output_names[key] != names:
                known_changed.append(f"{key[0]}:{key[1]}")
        recovered_present = all(
            counts.get((item.stockify_run_id, item.stock_clip_id), 0) == 1
            for item in recoveries
        )
        return {
            "mode": "location_recover_post_write_audit",
            "candidates_exactly_once": len(duplicates) == 0 and recovered_present,
            "duplicate_or_missing_identities": duplicates[:50],
            "known_location_candidates_changed": known_changed[:50],
            "recovered_count": len(recoveries),
            "output_candidates": len(output_appearances),
        }

    def _write_reports(
        self,
        report: ReviewLocationRecoverReport,
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
        text_report_path.write_text(
            format_location_recover_text(report), encoding="utf-8"
        )

    def _announce(self, message: str) -> None:
        if self.progress:
            self.progress(message)


# defaultdict[int] alias for type clarity in dry-run audit
CounterLike = dict


def _index_srts_by_basename(
    media_roots: list[Path],
    needed_stems: set[str],
) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = defaultdict(list)
    if not needed_stems:
        return {}
    for root in media_roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                name
                for name in dirnames
                if not name.startswith(".") and not name.endswith(".fcpbundle")
            ]
            for filename in filenames:
                if not filename.lower().endswith(".srt"):
                    continue
                stem = normalized_stem(filename)
                if stem not in needed_stems:
                    continue
                index[stem].append(Path(dirpath) / filename)
    return {stem: sorted(paths) for stem, paths in index.items()}


def _source_basename(row: dict[str, Any]) -> str:
    for key in (
        "source_filename",
        "source_name",
        "source_normalized_stem",
        "source_media_path",
    ):
        value = row.get(key)
        if value:
            return Path(str(value)).name
    return ""


def _is_unknown_candidate(row: dict[str, Any], event_name: str | None) -> bool:
    if event_name and "Unknown Location" in event_name:
        return True
    generated_event = str(row.get("generated_event_name") or "")
    if "Unknown Location" in generated_event:
        return True
    location = row.get("location") if isinstance(row.get("location"), dict) else {}
    label = str(
        location.get("public_label")
        or row.get("session_public_label")
        or ""
    ).strip()
    city = str(location.get("city") or row.get("session_city") or "").strip()
    if city and city.casefold() not in _UNKNOWN_LABELS:
        return False
    if label and label.casefold() not in _UNKNOWN_LABELS:
        return False
    return True


def persist_review_location_candidate_updates(
    connection: sqlite3.Connection,
    recoveries: Iterable[LocationRecoveryRow],
    *,
    now: str | None = None,
) -> int:
    """Apply historical materializer candidate/occurrence name+location writes.

    Caller owns the transaction. Used by review-location-recover persist and by
    historical location restore so both share one SQL implementation.
    """
    stamp = now or utc_now()
    count = 0
    for item in recoveries:
        location = (item.provenance or {}).get("location") or {}
        new_label = (item.provenance or {}).get("generated_project_label")
        connection.execute(
            """
            UPDATE stock_candidates
            SET location_json=?,
                generated_event_name=?,
                generated_project_label=COALESCE(?, generated_project_label),
                generated_clip_project_name=?,
                expected_export_basename=?,
                updated_at=?
            WHERE run_id=? AND stock_clip_id=?
            """,
            (
                json_dumps(location),
                item.new_event_name,
                new_label,
                item.new_project_name,
                item.new_project_name,
                stamp,
                item.stockify_run_id,
                item.stock_clip_id,
            ),
        )
        connection.execute(
            """
            UPDATE generated_occurrences
            SET generated_event_name=?, generated_project_name=?
            WHERE run_id=? AND stock_clip_id=? AND representation='individual'
            """,
            (
                item.new_event_name,
                item.new_project_name,
                item.stockify_run_id,
                item.stock_clip_id,
            ),
        )
        count += 1
    return count


def _event_capture_date(
    event_name: str, members: list[dict[str, Any]]
) -> str | None:
    if " — " in event_name:
        tail = event_name.rsplit(" — ", 1)[-1].strip()
        if len(tail) >= 10 and tail[0:4].isdigit():
            return tail[:10]
    for member in members:
        row = member.get("row") or {}
        capture = row.get("capture_time") if isinstance(row.get("capture_time"), dict) else {}
        for key in ("capture_date", "date", "captured_at_local"):
            value = capture.get(key) if capture else None
            if value:
                return str(value)[:10]
        session_date = row.get("session_capture_date")
        if session_date:
            return str(session_date)[:10]
    return None


def geographic_cluster_id(
    original_event: str, latitude: float, longitude: float
) -> str:
    """Stable identity: original event + representative GPS (not GPS alone)."""
    lat = f"{round(float(latitude), 6):.6f}"
    lon = f"{round(float(longitude), 6):.6f}"
    digest = hashlib.sha256(
        f"{original_event}\0{lat}\0{lon}".encode("utf-8")
    ).hexdigest()[:16]
    return f"gcluster_{digest}"


@dataclass(frozen=True)
class LocationOverrideEntry:
    index: int
    cluster_id: str | None
    original_event: str | None
    latitude: float | None
    longitude: float | None
    fields: dict[str, Any]


@dataclass
class LocationOverrideIndex:
    entries: list[LocationOverrideEntry]
    by_cluster_id: dict[str, LocationOverrideEntry] = field(default_factory=dict)
    by_event_gps: dict[tuple[str, str, str], LocationOverrideEntry] = field(
        default_factory=dict
    )

    def lookup(
        self,
        *,
        cluster_id: str,
        original_event: str,
        latitude: float,
        longitude: float,
    ) -> LocationOverrideEntry | None:
        by_id = self.by_cluster_id.get(cluster_id)
        if by_id is not None:
            return by_id
        key = (
            original_event,
            f"{round(float(latitude), 6):.6f}",
            f"{round(float(longitude), 6):.6f}",
        )
        return self.by_event_gps.get(key)


def load_location_overrides(path: Path) -> LocationOverrideIndex:
    if not path.is_file():
        raise VClipError(f"Location overrides file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VClipError(f"Could not read location overrides {path}: {exc}") from exc
    if isinstance(payload, dict):
        raw_items = payload.get("overrides")
        if raw_items is None:
            raise VClipError(
                "Location overrides JSON must contain an 'overrides' array"
            )
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raise VClipError("Location overrides JSON must be an object or array")
    if not isinstance(raw_items, list):
        raise VClipError("Location overrides 'overrides' value must be an array")

    entries: list[LocationOverrideEntry] = []
    by_cluster_id: dict[str, LocationOverrideEntry] = {}
    by_event_gps: dict[tuple[str, str, str], LocationOverrideEntry] = {}
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise VClipError(f"Override #{index} must be an object")
        cluster_id = item.get("cluster_id")
        cluster_id = str(cluster_id).strip() if cluster_id else None
        original_event = item.get("original_event")
        original_event = str(original_event).strip() if original_event else None
        lat_raw = item.get("representative_latitude", item.get("latitude"))
        lon_raw = item.get("representative_longitude", item.get("longitude"))
        latitude = float(lat_raw) if lat_raw is not None else None
        longitude = float(lon_raw) if lon_raw is not None else None
        if not cluster_id and not (
            original_event and latitude is not None and longitude is not None
        ):
            raise VClipError(
                f"Override #{index} must provide cluster_id or "
                "original_event + representative_latitude/longitude"
            )
        if latitude is not None and longitude is not None:
            if not is_usable_gps(latitude, longitude):
                raise VClipError(
                    f"Override #{index} GPS is not usable "
                    f"({latitude}, {longitude})"
                )
        fields = _override_location_fields(item, index=index)
        entry = LocationOverrideEntry(
            index=index,
            cluster_id=cluster_id,
            original_event=original_event,
            latitude=latitude,
            longitude=longitude,
            fields=fields,
        )
        entries.append(entry)
        if cluster_id:
            if cluster_id in by_cluster_id:
                raise VClipError(f"Duplicate override cluster_id: {cluster_id}")
            by_cluster_id[cluster_id] = entry
        if original_event and latitude is not None and longitude is not None:
            key = (
                original_event,
                f"{round(latitude, 6):.6f}",
                f"{round(longitude, 6):.6f}",
            )
            if key in by_event_gps:
                raise VClipError(
                    "Duplicate override for original_event + GPS: "
                    f"{original_event} @ {key[1]},{key[2]}"
                )
            by_event_gps[key] = entry
            # Also index the deterministic ID implied by event+GPS so either
            # targeting style can match the same cluster.
            implied_id = geographic_cluster_id(original_event, latitude, longitude)
            by_cluster_id.setdefault(implied_id, entry)

    return LocationOverrideIndex(
        entries=entries,
        by_cluster_id=by_cluster_id,
        by_event_gps=by_event_gps,
    )


def _override_location_fields(item: dict[str, Any], *, index: int) -> dict[str, Any]:
    city = item.get("city")
    country = item.get("country")
    if not city or not country:
        raise VClipError(
            f"Override #{index} requires city and country structured fields"
        )
    neighborhood = item.get("neighborhood") or item.get("locality")
    state = item.get("state") or item.get("region")
    return {
        "neighborhood": neighborhood,
        "locality": item.get("locality") or neighborhood,
        "city": city,
        "state": state,
        "region": item.get("region") or state,
        "country": country,
        "poi": item.get("poi"),
        "timezone": item.get("timezone"),
        "public_label": item.get("public_label"),
        "provider": "manual_gps_override",
    }


def _override_target_label(entry: LocationOverrideEntry) -> str:
    if entry.cluster_id:
        return entry.cluster_id
    if (
        entry.original_event
        and entry.latitude is not None
        and entry.longitude is not None
    ):
        return (
            f"{entry.original_event} @ "
            f"{round(entry.latitude, 6):.6f},{round(entry.longitude, 6):.6f}"
        )
    return f"override#{entry.index}"


def _location_from_place(
    place: dict[str, object],
    *,
    center_lat: float,
    center_lon: float,
    sample_count: int,
) -> dict[str, Any]:
    return _structured_location(
        place,
        center_lat=center_lat,
        center_lon=center_lon,
        sample_count=sample_count,
        evidence_sources=["srt_gps", "review_location_recover"],
        recovery_method=RECOVERY_REASON,
        place_provider=place.get("provider"),
    )


def _location_from_override(
    fields: dict[str, Any],
    *,
    center_lat: float,
    center_lon: float,
    sample_count: int,
) -> dict[str, Any]:
    # Preserve recovered GPS on the location payload; only the place label is manual.
    return _structured_location(
        fields,
        center_lat=center_lat,
        center_lon=center_lon,
        sample_count=sample_count,
        evidence_sources=["srt_gps", OVERRIDE_REASON],
        recovery_method=OVERRIDE_REASON,
        place_provider=OVERRIDE_REASON,
    )


def _structured_location(
    place: dict[str, object],
    *,
    center_lat: float,
    center_lon: float,
    sample_count: int,
    evidence_sources: list[str],
    recovery_method: str,
    place_provider: object | None,
    confidence: str | None = None,
    review_required: bool | None = None,
    gps_kind: str | None = None,
) -> dict[str, Any]:
    neighborhood = place.get("neighborhood") or place.get("locality")
    city = place.get("city")
    state = place.get("state") or place.get("region")
    country = place.get("country")
    if place.get("public_label"):
        public_label = str(place["public_label"])
    elif neighborhood and city:
        public_label = f"{neighborhood}, {city}"
    elif city and state:
        public_label = f"{city}, {state}"
    else:
        public_label = str(city or state or "Unknown Location")
    resolved_confidence = confidence or CONFIDENCE
    payload = {
        "status": "resolved",
        "confidence": resolved_confidence,
        "evidence_sources": list(evidence_sources),
        "center_lat": round(float(center_lat), 6),
        "center_lon": round(float(center_lon), 6),
        "sample_count": sample_count,
        "valid_sample_count": sample_count,
        "country": country,
        "state": state,
        "region": place.get("region") or state,
        "city": city,
        "locality": neighborhood,
        "neighborhood": neighborhood,
        "poi": place.get("poi"),
        "public_label": public_label,
        "timezone": place.get("timezone"),
        "place_provider": place_provider,
        "recovery": {
            "method": recovery_method,
            "confidence": resolved_confidence,
            "recovered_at": utc_now(),
        },
    }
    if review_required is not None:
        payload["review_required"] = bool(review_required)
    if gps_kind:
        payload["gps_kind"] = gps_kind
        # Explicitly distinguish inferred JPG coordinates from direct source GPS.
        payload["direct_source_gps"] = False
    return payload


def _apply_xml_recoveries(
    root: ET.Element, recoveries: list[LocationRecoveryRow]
) -> None:
    library = first_direct_child(root, "library")
    if library is None:
        raise VClipError("Shard FCPXML is missing <library>")
    projects_by_name: dict[str, ET.Element] = {}
    event_by_project: dict[ET.Element, ET.Element] = {}
    for event in list(library):
        if local_name(event.tag) != "event":
            continue
        for project in list(event):
            if local_name(project.tag) != "project":
                continue
            name = project.get("name") or ""
            projects_by_name[name] = project
            event_by_project[project] = event

    events_by_name: dict[str, ET.Element] = {
        event.get("name") or "": event
        for event in list(library)
        if local_name(event.tag) == "event"
    }

    for item in recoveries:
        project = projects_by_name.get(item.original_project_name)
        if project is None:
            # Resolve via stock_clip_id metadata when names already drifted.
            project = _find_project_by_clip_id(root, item.stock_clip_id)
        if project is None:
            raise VClipError(
                f"Could not find project {item.original_project_name!r} "
                f"for {item.stock_clip_id}"
            )
        old_event = event_by_project.get(project)
        project.set("name", item.new_project_name)
        target_event = events_by_name.get(item.new_event_name)
        if target_event is None:
            target_event = ET.Element(old_event.tag if old_event is not None else "event")
            target_event.set("name", item.new_event_name)
            if old_event is not None and old_event.get("uid"):
                target_event.set("uid", f"{old_event.get('uid')}-{uuid4().hex[:8]}")
            library.append(target_event)
            events_by_name[item.new_event_name] = target_event
        if old_event is not None and project in list(old_event):
            old_event.remove(project)
        target_event.append(project)
        event_by_project[project] = target_event
        projects_by_name[item.new_project_name] = project

    # Drop empty Unknown Location events created by splits.
    for event in list(library):
        if local_name(event.tag) != "event":
            continue
        projects = [
            child for child in list(event) if local_name(child.tag) == "project"
        ]
        if not projects and "Unknown Location" in (event.get("name") or ""):
            library.remove(event)


def _find_project_by_clip_id(root: ET.Element, clip_id: str) -> ET.Element | None:
    for project in root.iter():
        if local_name(project.tag) != "project":
            continue
        for node in project.iter():
            if local_name(node.tag) not in {"asset-clip", "video", "ref-clip", "sync-clip"}:
                continue
            metadata = read_vclip_metadata(node)
            if metadata.get("com.vclip.stock_clip_id") == clip_id:
                return project
    return None


def _rewrite_manifest(
    *,
    manifest: dict[str, Any],
    output_xml: Path,
    recoveries: list[LocationRecoveryRow],
) -> None:
    by_old_project = {item.original_project_name: item for item in recoveries}
    by_clip = {item.stock_clip_id: item for item in recoveries}
    payload = {key: value for key, value in manifest.items() if key != "path"}
    projects = []
    for project in payload.get("projects") or []:
        project = dict(project)
        name = project.get("project_name")
        clip_ids = [str(value) for value in project.get("stock_clip_ids") or []]
        recovery = by_old_project.get(str(name or ""))
        if recovery is None:
            for clip_id in clip_ids:
                if clip_id in by_clip:
                    recovery = by_clip[clip_id]
                    break
        if recovery is not None:
            project["project_name"] = recovery.new_project_name
            project["event_name"] = recovery.new_event_name
            project["stock_clip_ids"] = clip_ids
        projects.append(project)
    payload["projects"] = projects
    payload["project_count"] = len(projects)
    payload["location_recover"] = {
        "recovered_stock_clip_ids": sorted({item.stock_clip_id for item in recoveries}),
        "output_fcpxml": str(output_xml),
        "confidence": CONFIDENCE,
        "reason": RECOVERY_REASON,
    }
    target = output_xml.with_name(f"{output_xml.stem}-shard-manifest.json")
    target.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _cluster_report_row(
    *,
    cluster_id: str,
    original_event: str,
    stock_clip_ids: list[str],
    source_names: list[str],
    shard_paths: list[str],
    center_lat: float,
    center_lon: float,
    source_count: int,
    mixed_location_event: bool,
    resolution_status: str,
    resolved_location: str | None,
    resolved_city: str | None,
    resolved_region: str | None,
    resolved_country: str | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "cluster_id": cluster_id,
        "original_event": original_event,
        "candidate_count": len(stock_clip_ids),
        "source_count": source_count,
        "stock_clip_ids": list(stock_clip_ids),
        "source_names": list(source_names),
        "representative_latitude": round(float(center_lat), 6),
        "representative_longitude": round(float(center_lon), 6),
        "gps_confidence": (extra or {}).get("resolution_confidence") or CONFIDENCE,
        "resolution_status": resolution_status,
        # Backward-compatible alias used by earlier report consumers.
        "reverse_geocode_status": resolution_status,
        "resolved_location": resolved_location,
        "resolved_city": resolved_city,
        "resolved_region": resolved_region,
        "resolved_country": resolved_country,
        "shard_paths": list(shard_paths),
        "mixed_location_event": bool(mixed_location_event),
    }
    if extra:
        payload.update(extra)
    return payload


def _status_label(status: str | None) -> str:
    mapping = {
        STATUS_AUTOMATIC: "automatic reverse-geocode",
        STATUS_OVERRIDE: "manual GPS override",
        STATUS_UNRESOLVED: "unresolved GPS cluster",
        STATUS_FORENSIC_JPG: "forensic JPG EXIF same-shoot",
        "resolved": "automatic reverse-geocode",
        "failed": "unresolved GPS cluster",
    }
    return mapping.get(str(status or ""), str(status or "unknown"))


def _format_dossier_event(index: int, event: dict[str, Any]) -> list[str]:
    lines = [
        (
            f"{index}. {event.get('original_event_name')} | "
            f"{event.get('unresolved_clip_count')} unresolved clip(s), "
            f"{event.get('unique_sources')} source(s)"
            + (
                f" | scope={event.get('camera_scope')}"
                if event.get("camera_scope")
                else ""
            )
        ),
        f"   Shards: {', '.join(event.get('shard_paths') or [])}",
    ]
    prov = event.get("fcp_provenance") or {}
    if prov.get("source_event_names") or prov.get("libraries"):
        lines.append(
            f"   FCP events: {prov.get('source_event_names')} | "
            f"projects: {prov.get('source_project_names')}"
        )
    for source in event.get("sources") or []:
        weak = source.get("nearby_same_day_jpgs") or {}
        scope = source.get("camera_scope") or {}
        lines.append(
            f"   - {source.get('source_basename')} | "
            f"clips={len(source.get('stock_clip_ids') or [])} | "
            f"date={source.get('capture_date')} | "
            f"scope={scope.get('camera_scope') or event.get('camera_scope')} "
            f"family={scope.get('camera_family')} | "
            f"media={len(source.get('physical_media_paths') or [])} path(s) | "
            f"jpg_gps={len(weak.get('gps_photos') or [])} "
            f"jpg_nongps={len(weak.get('non_gps_photos') or [])} "
            f"seq_neighbors={len(source.get('nearby_dji_sequence_neighbors') or [])} "
            f"same_day_resolved={len(source.get('same_day_resolved_sources') or [])}"
        )
        if weak.get("rejection_reason"):
            lines.append(
                f"     jpg_status: {weak.get('rejection_reason')} "
                f"(accepted_inference={weak.get('accepted_inference')})"
            )
        loc_json = source.get("existing_location_json") or {}
        session = loc_json.get("session") or {}
        if any(session.values()) or loc_json.get("candidate_location") or loc_json.get(
            "source_media_location"
        ):
            lines.append(
                f"     location_json/session: "
                f"candidate={bool(loc_json.get('candidate_location'))} "
                f"media={bool(loc_json.get('source_media_location'))} "
                f"session_label={session.get('session_public_label')}"
            )
    lines.append("")
    return lines


def format_location_recover_text(report: ReviewLocationRecoverReport) -> str:
    audit = report.post_write_audit or {}
    title = (
        "Review location recovery — JPG EXIF forensic (read-only)"
        if report.forensic_jpg_exif
        else "Review location recovery"
    )
    lines = [
        title,
        "=" * len(title),
        f"Input root:                     {report.input_root}",
        f"Output root:                    {report.output_root}",
        f"Dry run:                        {str(report.dry_run).lower()}",
        f"Forensic JPG EXIF:              {str(report.forensic_jpg_exif).lower()}",
        f"Unknown events before:          {report.unknown_events_before:>7}",
        f"Unknown clips before:           {report.unknown_clips_before:>7}",
        f"Events with recoverable GPS:    {report.events_with_recoverable_gps:>7}",
        f"Clips with recoverable GPS:     {report.clips_with_recoverable_gps:>7}",
        f"Homogeneous events:             {report.homogeneous_events:>7}",
        f"Mixed-location events:          {report.mixed_location_events:>7}",
        f"Recovered geographic clusters:  {report.recovered_geographic_clusters:>7}",
        f"Candidates moved/relabelled:    {report.candidates_moved_or_relabelled:>7}",
        f"Manual GPS overrides applied:   {report.overrides_applied:>7}",
        f"Unused location overrides:      {report.overrides_unused:>7}",
        f"Unknown events after:           {report.unknown_events_after:>7}",
        f"Unknown clips after:            {report.unknown_clips_after:>7}",
        f"Shards changed:                 {report.shards_changed:>7}",
        f"Shards unchanged:               {report.shards_unchanged:>7}",
        f"Shards failed:                  {report.shards_failed:>7}",
        "",
    ]
    forensic = report.jpg_exif_forensic or {}
    if forensic:
        focus = forensic.get("focus_unknown_location_2025_11_08") or {}
        lines.extend(
            [
                "JPG EXIF same-shoot forensic",
                "----------------------------",
                f"Evidence source:                {forensic.get('evidence_source')}",
                f"SRT-less unknown sources:       {forensic.get('unknown_sources_without_srt_gps'):>7}",
                f"Sources with JPG inference:     {forensic.get('sources_with_jpg_inference'):>7}",
                f"Clips covered by JPG inference: {forensic.get('clips_covered_by_jpg_inference'):>7}",
                f"Review-required sources:        {forensic.get('review_required_sources'):>7}",
                f"Confidence counts:              {forensic.get('confidence_counts')}",
                f"Unknown Location — 2025-11-08 sources: {focus.get('sources'):>3}",
                f"Note: {forensic.get('note')}",
                "",
            ]
        )
        nov8 = list(focus.get("inferences") or [])
        if nov8:
            lines.append("Focus: Unknown Location — 2025-11-08")
            lines.append("------------------------------------")
            for item in nov8[:20]:
                lines.append(
                    f"- {item.get('source_basename')} | "
                    f"conf={item.get('confidence')} | "
                    f"review_required={item.get('review_required')} | "
                    f"{item.get('latitude')},{item.get('longitude')} | "
                    f"{item.get('association_reason')}"
                )
                for photo in (item.get("evidence_photos") or [])[:5]:
                    lines.append(
                        f"    photo seqΔ={photo.get('sequence_delta')} "
                        f"tΔ={photo.get('time_delta_seconds')}s "
                        f"cam={photo.get('camera_model')} "
                        f"role={photo.get('role')} "
                        f"{photo.get('path')}"
                    )
            lines.append("")

        editorial_summary = forensic.get("editorial_group_summary") or {}
        if editorial_summary:
            lines.extend(
                [
                    "Editorial-group geographic context (global)",
                    "------------------------------------------",
                    f"Current unknown clips (drone backlog): {editorial_summary.get('current_unknown_clips'):>7}",
                    f"Out of scope non-drone:                {editorial_summary.get('clips_out_of_scope_non_drone'):>7}",
                    f"Unknown including out-of-scope:        {editorial_summary.get('current_unknown_clips_including_out_of_scope'):>7}",
                    f"Clips with direct SRT GPS context:     {editorial_summary.get('clips_with_direct_srt_gps_context'):>7}",
                    f"Clips gaining source-level JPG context:{editorial_summary.get('clips_gaining_source_level_jpg_context'):>7}",
                    f"Additional clips eligible for group consensus: "
                    f"{editorial_summary.get('additional_clips_eligible_for_group_consensus'):>7}",
                    f"Clips only resolvable to city/metro/region: "
                    f"{editorial_summary.get('clips_only_resolvable_to_city_metro_region'):>7}",
                    f"Clips in genuinely mixed groups:       {editorial_summary.get('clips_in_genuinely_mixed_groups'):>7}",
                    f"Clips still fully unresolved (drone):  {editorial_summary.get('clips_still_fully_unresolved'):>7}",
                    f"Unresolved out-of-scope non-drone:     {editorial_summary.get('clips_out_of_scope_non_drone_unresolved'):>7}",
                    f"Camera family counts: {editorial_summary.get('camera_family_counts')}",
                    f"Coherence counts: {editorial_summary.get('coherence_counts')}",
                    f"Note: {editorial_summary.get('note')}",
                    "",
                ]
            )
        place_retry = forensic.get("place_label_retry") or {}
        if place_retry:
            lines.extend(
                [
                    "Place-label reverse-geocode retry",
                    "--------------------------------",
                    f"Attempted sources:  {place_retry.get('attempted_sources'):>7}",
                    f"Labeled sources:    {place_retry.get('labeled_sources'):>7}",
                    f"Failed sources:     {place_retry.get('failed_sources'):>7}",
                    f"Note: {place_retry.get('note')}",
                    "",
                ]
            )
            for item in (place_retry.get("details") or [])[:20]:
                if item.get("status") != "labeled":
                    continue
                lines.append(
                    f"  {item.get('source_basename')}: "
                    f"{item.get('public_label') or item.get('city')} "
                    f"({item.get('provider')}) "
                    f"@ {item.get('latitude')},{item.get('longitude')}"
                )
            if any(
                item.get("status") == "labeled"
                for item in (place_retry.get("details") or [])
            ):
                lines.append("")

        editorial_groups = list(forensic.get("editorial_groups") or [])
        if editorial_groups:
            lines.append("Unresolved editorial groups")
            lines.append("---------------------------")
            for index, group in enumerate(editorial_groups, start=1):
                lines.extend(
                    [
                        (
                            f"{index}. {group.get('original_event_name')} | "
                            f"{group.get('total_candidates')} clip(s), "
                            f"{group.get('unique_sources')} source(s) | "
                            f"coherence={group.get('geographic_coherence')} | "
                            f"label={group.get('recommended_group_label') or '(none)'}"
                        ),
                        f"   Shards: {', '.join(group.get('shard_paths') or [])}",
                        (
                            f"   SRT GPS sources: {group.get('sources_with_srt_gps')} | "
                            f"JPG GPS sources: {group.get('sources_with_jpg_gps')} | "
                            f"still unlocated sources: {group.get('still_unlocated_sources')}"
                        ),
                        (
                            f"   Cities: {group.get('cities_represented')} | "
                            f"Neighborhoods: {group.get('neighborhoods_represented')}"
                        ),
                        (
                            f"   Extent m: {group.get('geographic_extent_meters')} | "
                            f"confidence={group.get('confidence')} | "
                            f"review_required={group.get('review_required')}"
                        ),
                        (
                            f"   Inherit-eligible clips: "
                            f"{len(group.get('unknown_clips_eligible_to_inherit') or [])} "
                            f"| contradictions: {group.get('contradictory_evidence')}"
                        ),
                        "",
                    ]
                )

        dossiers = forensic.get("unresolved_evidence_dossiers") or {}
        events = list(dossiers.get("events_ranked_by_clip_count") or [])
        if events or dossiers.get("out_of_scope_non_drone_events_ranked"):
            lines.extend(
                [
                    "Fully unresolved evidence dossiers (drone backlog)",
                    "-------------------------------------------------",
                    (
                        f"Fully unresolved drone clips: {dossiers.get('fully_unresolved_clips')} | "
                        f"events: {dossiers.get('unresolved_events')}"
                    ),
                    (
                        f"Out-of-scope non-drone unresolved: "
                        f"{dossiers.get('out_of_scope_non_drone_clips')} | "
                        f"events: {dossiers.get('out_of_scope_non_drone_events')}"
                    ),
                    f"Note: {dossiers.get('note')}",
                    "",
                ]
            )
            for index, event in enumerate(events, start=1):
                lines.extend(_format_dossier_event(index, event))
            out_events = list(dossiers.get("out_of_scope_non_drone_events_ranked") or [])
            if out_events:
                lines.append("Out-of-scope non-drone unresolved (excluded from backlog)")
                lines.append("--------------------------------------------------------")
                for index, event in enumerate(out_events, start=1):
                    lines.extend(_format_dossier_event(index, event))
    if not report.forensic_jpg_exif:
        lines.extend(
            [
                "Post-write audit",
                "----------------",
                f"Candidates exactly once:        {audit.get('candidates_exactly_once')}",
                f"Known-location changed:         {len(audit.get('known_location_candidates_changed') or []):>7}",
                "",
            ]
        )
    if report.clusters:
        lines.append("Geographic clusters")
        lines.append("-------------------")
        for index, cluster in enumerate(report.clusters, start=1):
            status = cluster.get("resolution_status") or cluster.get(
                "reverse_geocode_status"
            )
            status_label = _status_label(status)
            location = cluster.get("resolved_location")
            location_text = location if location else "(unresolved)"
            mixed = "yes" if cluster.get("mixed_location_event") else "no"
            resolved = status in {
                STATUS_AUTOMATIC,
                STATUS_OVERRIDE,
                STATUS_FORENSIC_JPG,
                "resolved",
            }
            lines.extend(
                [
                    (
                        f"{index}. {cluster.get('original_event')} | "
                        f"{cluster.get('candidate_count')} clip(s), "
                        f"{cluster.get('source_count')} source(s) | "
                        f"mixed={mixed} | {status_label}"
                    ),
                    f"   Cluster ID: {cluster.get('cluster_id')}",
                    (
                        f"   GPS: {cluster.get('representative_latitude')}, "
                        f"{cluster.get('representative_longitude')} "
                        f"({cluster.get('gps_confidence')})"
                    ),
                    (
                        f"   Location: {location_text}"
                        + (
                            f" | {cluster.get('resolved_city')}, "
                            f"{cluster.get('resolved_region')}, "
                            f"{cluster.get('resolved_country')}"
                            if resolved
                            else ""
                        )
                    ),
                    f"   Sources: {', '.join(cluster.get('source_names') or [])}",
                    f"   Clips: {', '.join(cluster.get('stock_clip_ids') or [])}",
                    f"   Shards: {', '.join(cluster.get('shard_paths') or [])}",
                    "",
                ]
            )
    if report.warnings:
        lines.append("Warnings")
        lines.append("--------")
        for warning in report.warnings:
            lines.append(f"  {warning}")
        lines.append("")
    if report.failures:
        lines.append("Failures")
        lines.append("--------")
        for failure in report.failures:
            lines.append(f"  {failure.get('relative_path')}: {failure.get('error')}")
        lines.append("")
    return "\n".join(lines)
