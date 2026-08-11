"""Read-only LUT/effect integrity audit over a final review shard corpus."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable

from ..db.repository import CatalogRepository
from ..errors import VClipError
from ..stockify.core import local_name
from ..stockify.fcpxml import (
    build_resource_index,
    first_direct_child,
    has_custom_lut_effect,
    parse_source,
    read_vclip_metadata,
    video_effect_names,
    video_treatment_signature,
)
from ..util import utc_now
from .review_dlog_lut_audit import (
    COLOR_MD_DLOG_M,
    DlogAuditRecord,
    LutSignature,
    classify_dlog_candidate,
    collect_xml_lut_details,
    color_md_for_source,
    detect_camera_model,
    format_dlog_audit_text,
    historical_bucket,
    index_srts_by_basename,
    signature_key_for_detail,
    srt_lookup_stems,
    summarize_dlog_records,
    write_dlog_csv,
)

KNOWN_REGRESSION_CLIP_ID = "VCLIP_790B5CE0B04FB42A7B1D"
COHORT_MAY_2025 = date(2025, 5, 1)
COHORT_OCT_2024 = date(2024, 10, 1)
COHORT_APRIL_2025_END = date(2025, 4, 30)
_DJI_DATE_RE = re.compile(r"(20\d{2})(\d{2})(\d{2})")


@dataclass
class ColorIntegrityRecord:
    stockify_run_id: str
    stock_clip_id: str
    project_name: str | None
    event_name: str | None
    source_media_name: str | None
    capture_date: str | None
    original_duration_seconds: float | None
    proposed_duration_seconds: float | None
    final_duration_seconds: float | None
    candidate_tier: str | None
    db_camera_lut: str | None
    db_effect_signature: str | None
    db_final_effect_signature: str | None
    db_has_lut: bool
    xml_has_lut: bool | None
    xml_custom_lut_names: list[str] = field(default_factory=list)
    xml_lut_params: list[dict[str, str]] = field(default_factory=list)
    xml_effect_names: list[str] = field(default_factory=list)
    xml_treatment_signature: str | None = None
    media_prefix: str | None = None
    camera_family: str | None = None
    year_month: str | None = None
    historical_cohort: str | None = None
    shard_path: str | None = None
    status: str = "ok"
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ColorIntegrityReport:
    input_root: str
    database_path: str
    generated_at: str
    manifest_identities: int = 0
    resolved_projects: int = 0
    unresolved_identities: int = 0
    db_lut_xml_lut: int = 0
    db_lut_xml_no_lut: int = 0
    db_no_lut_xml_lut: int = 0
    db_no_lut_xml_no_lut: int = 0
    by_year_month: dict[str, dict[str, int]] = field(default_factory=dict)
    by_capture_date: dict[str, dict[str, int]] = field(default_factory=dict)
    by_camera_lut: dict[str, dict[str, int]] = field(default_factory=dict)
    by_media_prefix: dict[str, dict[str, int]] = field(default_factory=dict)
    by_candidate_tier: dict[str, dict[str, int]] = field(default_factory=dict)
    by_historical_cohort: dict[str, dict[str, int]] = field(default_factory=dict)
    post_may_2025_db_lut_xml_missing: list[dict[str, Any]] = field(default_factory=list)
    known_regression_case: dict[str, Any] | None = None
    failures: list[dict[str, Any]] = field(default_factory=list)
    records: list[ColorIntegrityRecord] = field(default_factory=list)
    media_roots: list[str] = field(default_factory=list)
    camera_lut_signatures: list[dict[str, Any]] = field(default_factory=list)
    dlog_audit: dict[str, Any] = field(default_factory=dict)
    dlog_records: list[dict[str, Any]] = field(default_factory=list)
    csv_report_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_root": self.input_root,
            "database_path": self.database_path,
            "generated_at": self.generated_at,
            "media_roots": list(self.media_roots),
            "manifest_identities": self.manifest_identities,
            "resolved_projects": self.resolved_projects,
            "unresolved_identities": self.unresolved_identities,
            "aggregates": {
                "db_lut_xml_lut": self.db_lut_xml_lut,
                "db_lut_xml_no_lut": self.db_lut_xml_no_lut,
                "db_no_lut_xml_lut": self.db_no_lut_xml_lut,
                "db_no_lut_xml_no_lut": self.db_no_lut_xml_no_lut,
            },
            "by_year_month": self.by_year_month,
            "by_capture_date": self.by_capture_date,
            "by_camera_lut": self.by_camera_lut,
            "by_media_prefix": self.by_media_prefix,
            "by_candidate_tier": self.by_candidate_tier,
            "by_historical_cohort": self.by_historical_cohort,
            "post_may_2025_db_lut_xml_missing": self.post_may_2025_db_lut_xml_missing,
            "known_regression_case": self.known_regression_case,
            "failures": self.failures,
            "camera_lut_signatures": list(self.camera_lut_signatures),
            "dlog_audit": dict(self.dlog_audit),
            "dlog_records": list(self.dlog_records),
            "csv_report_path": self.csv_report_path,
            "records": [item.as_dict() for item in self.records],
        }


class ReviewColorIntegrityService:
    """Audit Custom LUT / effect presence across final shard XML vs DB metadata."""

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
        input_root: Path,
        report_path: Path,
        text_report_path: Path,
        media_roots: Iterable[Path] | None = None,
        csv_report_path: Path | None = None,
    ) -> ColorIntegrityReport:
        input_root = input_root.expanduser().resolve()
        report_path = report_path.expanduser().resolve()
        text_report_path = text_report_path.expanduser().resolve()
        media_roots = [Path(root).expanduser().resolve() for root in (media_roots or [])]
        csv_report_path = (
            csv_report_path.expanduser().resolve() if csv_report_path else None
        )
        if not input_root.is_dir():
            raise VClipError(f"Input root not found: {input_root}")

        self._announce(f"Auditing final corpus color integrity: {input_root}")
        shard_entries = self._discover_shards(input_root)
        identities = self._collect_identities(shard_entries)
        pairs = {(item["stockify_run_id"], item["stock_clip_id"]) for item in identities}
        rows = self.repository.candidates_by_run_and_ids(pairs)

        # Parse each shard once with the production FCPXML loader.
        project_index_by_shard: dict[str, dict[str, dict[str, Any]]] = {}
        shard_errors: dict[str, str] = {}
        for entry in shard_entries:
            relative = entry["relative_xml"]
            try:
                project_index_by_shard[relative] = self._index_projects(entry["xml_path"])
            except Exception as exc:  # noqa: BLE001
                shard_errors[relative] = str(exc)
                self._announce(f"FAILED parse {relative}: {exc}")

        records: list[ColorIntegrityRecord] = []
        for identity in identities:
            run_id = identity["stockify_run_id"]
            clip_id = identity["stock_clip_id"]
            relative = identity["relative_xml"]
            row = rows.get((run_id, clip_id))
            if relative in shard_errors:
                records.append(
                    ColorIntegrityRecord(
                        stockify_run_id=run_id,
                        stock_clip_id=clip_id,
                        project_name=identity.get("project_name"),
                        event_name=identity.get("event_name"),
                        source_media_name=None,
                        capture_date=None,
                        original_duration_seconds=None,
                        proposed_duration_seconds=None,
                        final_duration_seconds=None,
                        candidate_tier=None,
                        db_camera_lut=row.get("camera_lut") if row else None,
                        db_effect_signature=row.get("effect_signature") if row else None,
                        db_final_effect_signature=(
                            row.get("final_effect_signature") if row else None
                        ),
                        db_has_lut=_db_has_lut(row.get("camera_lut") if row else None),
                        xml_has_lut=None,
                        shard_path=relative,
                        status="xml_parse_error",
                        error=shard_errors[relative],
                    )
                )
                continue
            if row is None:
                records.append(
                    ColorIntegrityRecord(
                        stockify_run_id=run_id,
                        stock_clip_id=clip_id,
                        project_name=identity.get("project_name"),
                        event_name=identity.get("event_name"),
                        source_media_name=None,
                        capture_date=None,
                        original_duration_seconds=None,
                        proposed_duration_seconds=None,
                        final_duration_seconds=None,
                        candidate_tier=None,
                        db_camera_lut=None,
                        db_effect_signature=None,
                        db_final_effect_signature=None,
                        db_has_lut=False,
                        xml_has_lut=None,
                        shard_path=relative,
                        status="db_missing",
                    )
                )
                continue

            project_info = project_index_by_shard.get(relative, {}).get(clip_id)
            if project_info is None and identity.get("project_name"):
                # Fallback: match by project name when metadata is absent.
                for candidate in project_index_by_shard.get(relative, {}).values():
                    if candidate.get("project_name") == identity.get("project_name"):
                        project_info = candidate
                        break
            if project_info is None:
                records.append(
                    self._record_from_db(
                        row,
                        identity=identity,
                        xml_has_lut=None,
                        status="project_missing",
                    )
                )
                continue

            records.append(
                self._record_from_db(
                    row,
                    identity=identity,
                    xml_has_lut=project_info["xml_has_lut"],
                    xml_custom_lut_names=project_info["xml_custom_lut_names"],
                    xml_lut_params=project_info["xml_lut_params"],
                    xml_effect_names=project_info["xml_effect_names"],
                    xml_treatment_signature=project_info["xml_treatment_signature"],
                    project_name=project_info.get("project_name")
                    or identity.get("project_name"),
                    event_name=project_info.get("event_name")
                    or identity.get("event_name"),
                    status="ok",
                )
            )

        report = self._build_report(
            input_root=input_root,
            records=records,
        )
        report.media_roots = [str(root) for root in media_roots]
        if media_roots:
            signatures, dlog_records, dlog_summary = self._run_dlog_audit(
                identities=identities,
                rows=rows,
                project_index_by_shard=project_index_by_shard,
                media_roots=media_roots,
            )
            report.camera_lut_signatures = [item.as_dict() for item in signatures]
            report.dlog_records = [item.as_dict() for item in dlog_records]
            report.dlog_audit = dlog_summary
            if csv_report_path is not None:
                write_dlog_csv(csv_report_path, dlog_records)
                report.csv_report_path = str(csv_report_path)
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

    def _collect_identities(
        self, shard_entries: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        identities: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for entry in shard_entries:
            manifest = entry["manifest"]
            run_id = str(manifest.get("stockify_run_id") or "")
            if not run_id:
                continue
            # Individual projects only — top-level stock_clip_ids can still list
            # compilation members that have no dedicated <project> to audit.
            for project in manifest.get("projects") or []:
                if project.get("representation") == "compilation":
                    continue
                if "Stock Compilation" in str(project.get("project_name") or ""):
                    continue
                for clip_id in project.get("stock_clip_ids") or []:
                    clip_id = str(clip_id)
                    key = (run_id, clip_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    identities.append(
                        {
                            "stockify_run_id": run_id,
                            "stock_clip_id": clip_id,
                            "project_name": project.get("project_name"),
                            "event_name": project.get("event_name"),
                            "relative_xml": entry["relative_xml"],
                        }
                    )
        return identities

    def _index_projects(self, xml_path: Path) -> dict[str, dict[str, Any]]:
        """Parse one shard with production helpers; index by stock_clip_id."""
        tree = parse_source(xml_path)
        root = tree.getroot()
        resources = first_direct_child(root, "resources")
        resource_index = build_resource_index(resources) if resources is not None else {}
        indexed: dict[str, dict[str, Any]] = {}
        for project in _iter_projects(root):
            project_name = project.get("name")
            event_name = _event_name_for_project(root, project)
            clip = _primary_clip(project)
            if clip is None:
                continue
            metadata = read_vclip_metadata(clip)
            clip_id = metadata.get("com.vclip.stock_clip_id")
            if not clip_id:
                continue
            effect_names = video_effect_names(clip, resource_index)
            lut_details = collect_xml_lut_details(clip, resource_index)
            lut_params: list[dict[str, str]] = []
            lut_names: list[str] = []
            for detail in lut_details:
                lut_names.append(str(detail.get("effect_name") or "Custom LUT"))
                lut_params.extend(list(detail.get("params") or []))
            indexed[str(clip_id)] = {
                "project_name": project_name,
                "event_name": event_name,
                "xml_has_lut": bool(lut_details) or has_custom_lut_effect(effect_names),
                "xml_custom_lut_names": lut_names,
                "xml_lut_params": lut_params,
                "xml_lut_details": lut_details,
                "xml_effect_names": effect_names,
                "xml_treatment_signature": video_treatment_signature(clip),
            }
        return indexed

    def _run_dlog_audit(
        self,
        *,
        identities: list[dict[str, Any]],
        rows: dict[tuple[str, str], dict[str, Any]],
        project_index_by_shard: dict[str, dict[str, dict[str, Any]]],
        media_roots: list[Path],
    ) -> tuple[list[LutSignature], list[DlogAuditRecord], dict[str, Any]]:
        needed_stems: set[str] = set()
        for identity in identities:
            row = rows.get((identity["stockify_run_id"], identity["stock_clip_id"]))
            if row is None:
                continue
            needed_stems.update(
                srt_lookup_stems(
                    row.get("source_name"),
                    row.get("source_filename"),
                    row.get("source_normalized_stem"),
                    Path(str(row["source_media_path"])).name
                    if row.get("source_media_path")
                    else None,
                )
            )
        self._announce(f"Indexing SRT color_md for {len(needed_stems)} source stem(s)")
        srt_index = index_srts_by_basename(media_roots, needed_stems)

        signatures: dict[str, LutSignature] = {}
        dlog_records: list[DlogAuditRecord] = []
        color_md_source_counts: Counter[str] = Counter()
        sources_with_srt = 0
        sources_seen: set[str] = set()

        for identity in identities:
            run_id = identity["stockify_run_id"]
            clip_id = identity["stock_clip_id"]
            row = rows.get((run_id, clip_id))
            if row is None:
                continue
            # Prefer clean asset/source_name over FCP "(fcp1)" filenames.
            source_name = (
                row.get("source_name")
                or row.get("source_filename")
                or row.get("source_normalized_stem")
            )
            source_name_str = str(source_name) if source_name else None
            media_path = row.get("source_media_path")
            color_md, srt_path = color_md_for_source(
                source_name_str,
                srt_index,
                row.get("source_filename"),
                row.get("source_normalized_stem"),
                Path(str(media_path)).name if media_path else None,
            )
            stem = srt_lookup_stems(
                source_name_str,
                row.get("source_filename"),
                row.get("source_normalized_stem"),
            )
            stem = stem[0] if stem else ""
            if stem and stem not in sources_seen:
                sources_seen.add(stem)
                if srt_path:
                    sources_with_srt += 1
                if color_md:
                    color_md_source_counts[color_md] += 1
            camera_model, camera_evidence = detect_camera_model(
                str(media_path) if media_path else None,
                srt_path,
                source_name_str,
            )
            project_info = project_index_by_shard.get(identity["relative_xml"], {}).get(
                clip_id
            )
            xml_details = list((project_info or {}).get("xml_lut_details") or [])
            for detail in xml_details:
                key = signature_key_for_detail(detail)
                signature = signatures.get(key)
                if signature is None:
                    signature = LutSignature(
                        signature_key=key,
                        effect_name=detail.get("effect_name"),
                        effect_uid=detail.get("effect_uid"),
                        filter_attributes=dict(detail.get("filter_attributes") or {}),
                        params=list(detail.get("params") or []),
                        normalized_lut_identity=detail.get("normalized_lut_identity"),
                        lut_camera_model=detail.get("lut_camera_model"),
                        asset_custom_lut_override=detail.get(
                            "asset_custom_lut_override"
                        ),
                        lut_data_fingerprint=detail.get("lut_data_fingerprint"),
                    )
                    signatures[key] = signature
                signature.occurrence_count += 1
                if clip_id not in signature.example_stock_clip_ids:
                    signature.example_stock_clip_ids.append(clip_id)
                project_name = (project_info or {}).get("project_name") or identity.get(
                    "project_name"
                )
                if project_name and project_name not in signature.example_projects:
                    signature.example_projects.append(str(project_name))
                signature.source_camera_models[camera_model or "unknown"] += 1

            if color_md != COLOR_MD_DLOG_M:
                continue
            capture = _capture_date(row, source_name_str)
            classification, xml_identity, xml_lut_model, db_xml_agree = (
                classify_dlog_candidate(
                    source_camera_model=camera_model,
                    db_camera_lut=row.get("camera_lut"),
                    xml_details=xml_details,
                )
            )
            dlog_records.append(
                DlogAuditRecord(
                    stock_clip_id=clip_id,
                    stockify_run_id=run_id,
                    source_name=source_name_str,
                    capture_date=capture,
                    color_md=color_md,
                    camera_model=camera_model,
                    camera_model_source=camera_evidence,
                    shard_path=identity.get("relative_xml"),
                    project_name=(project_info or {}).get("project_name")
                    or identity.get("project_name"),
                    db_camera_lut=row.get("camera_lut"),
                    xml_custom_luts=[
                        str(
                            detail.get("normalized_lut_identity")
                            or detail.get("effect_name")
                            or "Custom LUT"
                        )
                        for detail in xml_details
                    ],
                    xml_normalized_lut_identity=xml_identity,
                    xml_lut_camera_model=xml_lut_model,
                    db_xml_agree=db_xml_agree,
                    classification=classification,
                    year_month=(capture[:7] if capture else None),
                    historical_bucket=historical_bucket(capture),
                    srt_path=srt_path,
                )
            )

        ordered_signatures = sorted(
            signatures.values(),
            key=lambda item: (-item.occurrence_count, item.signature_key),
        )
        dlog_summary = summarize_dlog_records(dlog_records)
        dlog_summary.update(
            {
                "sources_scanned": len(sources_seen),
                "sources_with_physical_srt": sources_with_srt,
                "raw_color_md_source_counts": dict(color_md_source_counts),
                "note": (
                    "color_md=dlog_m is definitive D-Log M source evidence; "
                    "color_md=default is preserved literally and is not assumed Normal."
                ),
            }
        )
        return ordered_signatures, dlog_records, dlog_summary

    def _record_from_db(
        self,
        row: dict[str, Any],
        *,
        identity: dict[str, Any],
        xml_has_lut: bool | None,
        status: str,
        xml_custom_lut_names: list[str] | None = None,
        xml_lut_params: list[dict[str, str]] | None = None,
        xml_effect_names: list[str] | None = None,
        xml_treatment_signature: str | None = None,
        project_name: str | None = None,
        event_name: str | None = None,
    ) -> ColorIntegrityRecord:
        source_name = (
            row.get("source_name")
            or row.get("source_filename")
            or row.get("source_normalized_stem")
        )
        capture_date = _capture_date(row, source_name)
        media_prefix, camera_family = _media_family(source_name)
        return ColorIntegrityRecord(
            stockify_run_id=str(row["run_id"]),
            stock_clip_id=str(row["stock_clip_id"]),
            project_name=project_name or identity.get("project_name"),
            event_name=event_name or identity.get("event_name"),
            source_media_name=str(source_name) if source_name else None,
            capture_date=capture_date,
            original_duration_seconds=_as_float(row.get("original_duration_seconds")),
            proposed_duration_seconds=_as_float(row.get("proposed_duration_seconds")),
            final_duration_seconds=_as_float(row.get("final_duration_seconds")),
            candidate_tier=row.get("candidate_tier"),
            db_camera_lut=row.get("camera_lut"),
            db_effect_signature=row.get("effect_signature"),
            db_final_effect_signature=row.get("final_effect_signature"),
            db_has_lut=_db_has_lut(row.get("camera_lut")),
            xml_has_lut=xml_has_lut,
            xml_custom_lut_names=list(xml_custom_lut_names or []),
            xml_lut_params=list(xml_lut_params or []),
            xml_effect_names=list(xml_effect_names or []),
            xml_treatment_signature=xml_treatment_signature,
            media_prefix=media_prefix,
            camera_family=camera_family,
            year_month=(capture_date[:7] if capture_date else None),
            historical_cohort=_historical_cohort(capture_date),
            shard_path=identity.get("relative_xml"),
            status=status,
        )

    def _build_report(
        self,
        *,
        input_root: Path,
        records: list[ColorIntegrityRecord],
    ) -> ColorIntegrityReport:
        ok = [item for item in records if item.status == "ok"]
        unresolved = [item for item in records if item.status != "ok"]

        def bucket(items: list[ColorIntegrityRecord]) -> dict[str, int]:
            return {
                "db_lut_xml_lut": sum(
                    1 for item in items if item.db_has_lut and item.xml_has_lut
                ),
                "db_lut_xml_no_lut": sum(
                    1 for item in items if item.db_has_lut and item.xml_has_lut is False
                ),
                "db_no_lut_xml_lut": sum(
                    1
                    for item in items
                    if (not item.db_has_lut) and item.xml_has_lut
                ),
                "db_no_lut_xml_no_lut": sum(
                    1
                    for item in items
                    if (not item.db_has_lut) and item.xml_has_lut is False
                ),
                "unresolved": sum(1 for item in items if item.status != "ok"),
                "total": len(items),
            }

        by_year_month: dict[str, list[ColorIntegrityRecord]] = defaultdict(list)
        by_capture_date: dict[str, list[ColorIntegrityRecord]] = defaultdict(list)
        by_camera_lut: dict[str, list[ColorIntegrityRecord]] = defaultdict(list)
        by_media_prefix: dict[str, list[ColorIntegrityRecord]] = defaultdict(list)
        by_tier: dict[str, list[ColorIntegrityRecord]] = defaultdict(list)
        by_cohort: dict[str, list[ColorIntegrityRecord]] = defaultdict(list)
        for item in records:
            by_year_month[item.year_month or "unknown"].append(item)
            by_capture_date[item.capture_date or "unknown"].append(item)
            by_camera_lut[item.db_camera_lut or "(none)"].append(item)
            by_media_prefix[item.media_prefix or "unknown"].append(item)
            by_tier[item.candidate_tier or "unknown"].append(item)
            by_cohort[item.historical_cohort or "unknown"].append(item)

        post_may_anomalies = [
            item.as_dict()
            for item in ok
            if item.historical_cohort == "2025-05-01_onward"
            and item.db_has_lut
            and item.xml_has_lut is False
        ]
        known = next(
            (
                item.as_dict()
                for item in records
                if item.stock_clip_id == KNOWN_REGRESSION_CLIP_ID
            ),
            None,
        )

        return ColorIntegrityReport(
            input_root=str(input_root),
            database_path=str(self.repository.database.path),
            generated_at=utc_now(),
            manifest_identities=len(records),
            resolved_projects=len(ok),
            unresolved_identities=len(unresolved),
            db_lut_xml_lut=sum(1 for item in ok if item.db_has_lut and item.xml_has_lut),
            db_lut_xml_no_lut=sum(
                1 for item in ok if item.db_has_lut and item.xml_has_lut is False
            ),
            db_no_lut_xml_lut=sum(
                1 for item in ok if (not item.db_has_lut) and item.xml_has_lut
            ),
            db_no_lut_xml_no_lut=sum(
                1 for item in ok if (not item.db_has_lut) and item.xml_has_lut is False
            ),
            by_year_month={key: bucket(value) for key, value in sorted(by_year_month.items())},
            by_capture_date={
                key: bucket(value) for key, value in sorted(by_capture_date.items())
            },
            by_camera_lut={key: bucket(value) for key, value in sorted(by_camera_lut.items())},
            by_media_prefix={
                key: bucket(value) for key, value in sorted(by_media_prefix.items())
            },
            by_candidate_tier={key: bucket(value) for key, value in sorted(by_tier.items())},
            by_historical_cohort={
                key: bucket(value) for key, value in sorted(by_cohort.items())
            },
            post_may_2025_db_lut_xml_missing=post_may_anomalies,
            known_regression_case=known,
            failures=[
                {
                    "status": item.status,
                    "stockify_run_id": item.stockify_run_id,
                    "stock_clip_id": item.stock_clip_id,
                    "project_name": item.project_name,
                    "shard_path": item.shard_path,
                    "error": item.error,
                }
                for item in unresolved
            ],
            records=records,
        )

    def _write_reports(
        self,
        report: ColorIntegrityReport,
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
        text_report_path.write_text(format_color_integrity_text(report), encoding="utf-8")

    def _announce(self, message: str) -> None:
        if self.progress:
            self.progress(message)


def naive_project_effect_inventory(
    xml_path: Path, project_name: str
) -> dict[str, Any] | None:
    """Legacy hand-parser that fails on namespaced FCPXML tags.

    Kept for tests that reproduce the previous all-xml_parse_error / miss behavior.
    """
    root = ET.parse(xml_path).getroot()
    # Intentionally ignore namespaces — this is the historical bug.
    project = next(
        (node for node in root.iter("project") if node.get("name") == project_name),
        None,
    )
    if project is None:
        return None
    effect_defs = {
        effect.get("id"): {
            "name": effect.get("name") or "",
            "uid": effect.get("uid") or "",
        }
        for effect in root.iter("effect")
        if effect.get("id")
    }
    used_refs = []
    params = []
    for element in project.iter():
        ref = element.get("ref")
        if ref and ref in effect_defs:
            used_refs.append((element.tag, ref, effect_defs[ref]))
        if element.tag == "param":
            params.append(
                {
                    "name": element.get("name") or "",
                    "key": element.get("key") or "",
                    "value": element.get("value") or "",
                }
            )
    text = " ".join(
        [
            *(item[2]["name"] for item in used_refs),
            *(item[2]["uid"] for item in used_refs),
            *(item["name"] for item in params),
            *(item["key"] for item in params),
            *(item["value"] for item in params),
        ]
    ).lower()
    return {
        "has_lut": "lut" in text,
        "used_refs": used_refs,
        "params": params,
    }


def _iter_projects(root: ET.Element) -> list[ET.Element]:
    library = first_direct_child(root, "library")
    if library is None:
        return [
            node
            for node in root.iter()
            if local_name(node.tag) == "project"
            and "Stock Compilation" not in (node.get("name") or "")
        ]
    projects: list[ET.Element] = []
    for event in library:
        if local_name(event.tag) != "event":
            continue
        for project in event:
            if local_name(project.tag) != "project":
                continue
            if "Stock Compilation" in (project.get("name") or ""):
                continue
            projects.append(project)
    return projects


def _event_name_for_project(root: ET.Element, project: ET.Element) -> str | None:
    library = first_direct_child(root, "library")
    if library is None:
        return None
    for event in library:
        if local_name(event.tag) != "event":
            continue
        if project in list(event):
            return event.get("name")
    return None


def _primary_clip(project: ET.Element) -> ET.Element | None:
    sequence = first_direct_child(project, "sequence")
    spine = first_direct_child(sequence, "spine") if sequence is not None else None
    if spine is None:
        return None
    for node in spine.iter():
        if node is spine:
            continue
        if local_name(node.tag) in {"asset-clip", "video", "ref-clip", "sync-clip"}:
            return node
    return None


def _db_has_lut(value: Any) -> bool:
    """True when DB camera_lut looks like a real conversion/Custom LUT identity."""
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    lower = text.casefold().replace(" ", "")
    if lower in {"0", "none", "(none)", "0(none)"}:
        return False
    # Asset metadata fallback often stores disabled log-conversion markers.
    if "rawtologconversion=0" in lower:
        return False
    return True


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _capture_date(row: dict[str, Any], source_name: str | None) -> str | None:
    capture = row.get("capture_time") if isinstance(row.get("capture_time"), dict) else {}
    for key in ("captured_at_local", "capture_date", "date"):
        value = capture.get(key) if capture else None
        if value:
            return str(value)[:10]
    session_date = row.get("session_capture_date") or row.get("session_captured_at_local")
    if session_date:
        return str(session_date)[:10]
    if source_name:
        match = _DJI_DATE_RE.search(str(source_name).replace("-", "").replace("_", ""))
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return None


def _historical_cohort(capture_date: str | None) -> str:
    if not capture_date:
        return "unknown"
    try:
        parsed = date.fromisoformat(capture_date[:10])
    except ValueError:
        return "unknown"
    if parsed < COHORT_OCT_2024:
        return "before_2024-10-01"
    if parsed <= COHORT_APRIL_2025_END:
        return "2024-10-01_to_2025-04-30"
    return "2025-05-01_onward"


def _media_family(source_name: str | None) -> tuple[str | None, str | None]:
    if not source_name:
        return None, None
    text = str(source_name)
    prefix = text.split("_", 1)[0] if "_" in text else text[:3]
    upper = text.upper()
    if upper.startswith("DJI"):
        if "AIR" in upper:
            family = "DJI_Air"
        elif "MINI" in upper:
            family = "DJI_Mini"
        elif "MAVIC" in upper:
            family = "DJI_Mavic"
        else:
            family = "DJI"
    elif upper.startswith("IMG") or upper.startswith("DSC"):
        family = "camera_photo"
    else:
        family = prefix
    return prefix, family


def format_color_integrity_text(report: ColorIntegrityReport) -> str:
    lines = [
        "VCLIP FINAL CORPUS LUT / EFFECT INTEGRITY AUDIT",
        "=" * 100,
        f"Input root:                    {report.input_root}",
        f"Generated at:                  {report.generated_at}",
        f"Manifest identities:           {report.manifest_identities:,}",
        f"Resolved projects:             {report.resolved_projects:,}",
        f"Unresolved identities:         {report.unresolved_identities:,}",
        "",
        f"DB LUT + XML LUT:              {report.db_lut_xml_lut:,}",
        f"DB LUT + XML NO LUT:           {report.db_lut_xml_no_lut:,}",
        f"DB NO LUT + XML LUT:           {report.db_no_lut_xml_lut:,}",
        f"DB NO LUT + XML NO LUT:        {report.db_no_lut_xml_no_lut:,}",
        "",
        "Historical cohorts (analysis only; pre-May-2025 missing LUT is not auto-invalid)",
        "-" * 100,
    ]
    for cohort in (
        "before_2024-10-01",
        "2024-10-01_to_2025-04-30",
        "2025-05-01_onward",
        "unknown",
    ):
        bucket = report.by_historical_cohort.get(cohort)
        if not bucket:
            continue
        lines.append(
            f"{cohort:<32} total={bucket['total']:<5} "
            f"both={bucket['db_lut_xml_lut']:<5} "
            f"db_only={bucket['db_lut_xml_no_lut']:<5} "
            f"xml_only={bucket['db_no_lut_xml_lut']:<5} "
            f"neither={bucket['db_no_lut_xml_no_lut']:<5}"
        )

    lines.extend(
        [
            "",
            "By candidate tier",
            "-" * 100,
        ]
    )
    for tier, bucket in report.by_candidate_tier.items():
        lines.append(
            f"{tier:<32} total={bucket['total']:<5} "
            f"both={bucket['db_lut_xml_lut']:<5} "
            f"db_only={bucket['db_lut_xml_no_lut']:<5}"
        )

    lines.extend(
        [
            "",
            "By camera LUT value",
            "-" * 100,
        ]
    )
    for lut, bucket in sorted(
        report.by_camera_lut.items(),
        key=lambda item: (-item[1]["total"], item[0]),
    )[:40]:
        lines.append(
            f"{bucket['total']:>5}  both={bucket['db_lut_xml_lut']:<5} "
            f"db_only={bucket['db_lut_xml_no_lut']:<5}  {lut}"
        )

    lines.extend(
        [
            "",
            "By media prefix / camera family",
            "-" * 100,
        ]
    )
    for prefix, bucket in sorted(
        report.by_media_prefix.items(),
        key=lambda item: (-item[1]["total"], item[0]),
    )[:40]:
        lines.append(
            f"{bucket['total']:>5}  both={bucket['db_lut_xml_lut']:<5} "
            f"db_only={bucket['db_lut_xml_no_lut']:<5}  {prefix}"
        )

    lines.extend(
        [
            "",
            "TARGETED ANOMALIES — 2025-05-01 onward, DB LUT present, XML LUT missing",
            "=" * 100,
            f"Count: {len(report.post_may_2025_db_lut_xml_missing):,}",
        ]
    )
    for index, item in enumerate(report.post_may_2025_db_lut_xml_missing[:100], 1):
        lines.extend(
            [
                "",
                f"{index}. {item.get('project_name')}",
                f"   ID:       {item.get('stock_clip_id')}",
                f"   Run:      {item.get('stockify_run_id')}",
                f"   Event:    {item.get('event_name')}",
                f"   Source:   {item.get('source_media_name')}",
                f"   Capture:  {item.get('capture_date')}",
                f"   Tier:     {item.get('candidate_tier')}",
                f"   DB LUT:   {item.get('db_camera_lut')}",
                f"   Shard:    {item.get('shard_path')}",
            ]
        )

    lines.extend(
        [
            "",
            f"KNOWN REGRESSION CASE — {KNOWN_REGRESSION_CLIP_ID}",
            "=" * 100,
        ]
    )
    known = report.known_regression_case
    if known is None:
        lines.append("Not present in audited corpus.")
    else:
        lines.extend(
            [
                f"Status:           {known.get('status')}",
                f"Run:              {known.get('stockify_run_id')}",
                f"Project:          {known.get('project_name')}",
                f"Event:            {known.get('event_name')}",
                f"Source:           {known.get('source_media_name')}",
                f"Capture:          {known.get('capture_date')}",
                f"Tier:             {known.get('candidate_tier')}",
                f"DB camera_lut:    {known.get('db_camera_lut')}",
                f"DB effect_sig:    {known.get('db_effect_signature')}",
                f"DB final_effect:  {known.get('db_final_effect_signature')}",
                f"XML has LUT:      {known.get('xml_has_lut')}",
                f"XML LUT names:    {known.get('xml_custom_lut_names')}",
                f"XML effects:      {known.get('xml_effect_names')}",
                f"Shard:            {known.get('shard_path')}",
            ]
        )

    if report.failures:
        lines.extend(["", "FAILURES", "=" * 100])
        status_counts = Counter(item["status"] for item in report.failures)
        for status, count in status_counts.most_common():
            lines.append(f"{status:<20} {count:,}")
        for item in report.failures[:50]:
            lines.append(
                f"{item['status']:<20} {item['stockify_run_id']} / "
                f"{item['stock_clip_id']} / {item.get('project_name')}"
            )
        if len(report.failures) > 50:
            lines.append(f"… {len(report.failures) - 50} more")

    if report.camera_lut_signatures or report.dlog_records:
        lines.extend(
            [
                "",
                format_dlog_audit_text(
                    signatures=report.camera_lut_signatures,
                    dlog_summary=report.dlog_audit,
                    records=report.dlog_records,
                ).rstrip(),
            ]
        )

    lines.append("")
    return "\n".join(lines)
