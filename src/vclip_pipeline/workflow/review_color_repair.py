"""Repair confirmed Mini 5 Pro ← Air 3 wrong Camera LUT cohort in review shards."""

from __future__ import annotations

import copy
import json
import shutil
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..db.repository import CatalogRepository
from ..errors import VClipError
from ..stockify.core import local_name
from ..stockify.fcpxml import (
    asset_conversion_lut,
    build_resource_index,
    first_direct_child,
    is_custom_lut_filter,
    parse_source,
    read_vclip_metadata,
    validate_fcpxml,
    video_treatment_signature,
)
from ..util import json_dumps, utc_now
from .catalog import WorkflowCatalog
from .review_dlog_lut_audit import (
    COLOR_MD_DLOG_M,
    CLASS_WRONG,
    classify_dlog_candidate,
    collect_xml_lut_details,
    color_md_for_source,
    detect_camera_model,
    index_srts_by_basename,
    normalize_lut_identity,
    srt_lookup_stems,
)

TARGET_CAMERA_MODEL = "DJI Mini 5 Pro"
WRONG_LUT_IDENTITY = "DJI Air 3 D-Log M to Rec.709 V1_"
TARGET_LUT_OVERRIDE = (
    "LUT:908403d40286925c5b19129c4be6c0f4 "
    "(DJI Mini 5 Pro D-Log M to Rec.709 LUT)"
)
TARGET_LUT_IDENTITY = "DJI Mini 5 Pro D-Log M to Rec.709 LUT"
REPAIR_REASON = "mini5_air3_wrong_camera_lut"
REPAIR_STATUS_REPAIRED = "repaired"
REPAIR_STATUS_SKIPPED = "skipped"
REPAIR_STATUS_REJECTED = "rejected"


@dataclass
class CanonicalCameraLutTemplate:
    custom_lut_override: str
    effect_name: str
    effect_uid: str
    filter_name: str
    params: list[dict[str, str]]
    donor_stock_clip_id: str
    donor_shard: str
    donor_asset_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ColorRepairRow:
    stockify_run_id: str
    stock_clip_id: str
    source_media: str | None
    camera_model: str
    color_md: str
    previous_camera_lut: str | None
    new_camera_lut: str
    previous_xml_lut_identity: str | None
    new_xml_lut_identity: str
    source_shard: str
    project_name: str | None
    input_xml: str
    output_xml: str | None
    repair_reason: str
    status: str
    srt_path: str | None = None
    previous_effect_signature: str | None = None
    new_effect_signature: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReviewColorRepairReport:
    input_root: str
    output_root: str
    dry_run: bool
    media_roots: list[str] = field(default_factory=list)
    target_camera_model: str = TARGET_CAMERA_MODEL
    wrong_lut_identity: str = WRONG_LUT_IDENTITY
    target_lut_override: str = TARGET_LUT_OVERRIDE
    donor_template: dict[str, Any] | None = None
    candidates_scanned: int = 0
    eligible_repairs: int = 0
    repaired: int = 0
    skipped_already_correct: int = 0
    rejected_non_cohort: int = 0
    shards_changed: int = 0
    shards_unchanged: int = 0
    shards_failed: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    repairs: list[dict[str, Any]] = field(default_factory=list)
    rejections: list[dict[str, Any]] = field(default_factory=list)
    post_write_audit: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_root": self.input_root,
            "output_root": self.output_root,
            "dry_run": self.dry_run,
            "media_roots": list(self.media_roots),
            "target_camera_model": self.target_camera_model,
            "wrong_lut_identity": self.wrong_lut_identity,
            "target_lut_override": self.target_lut_override,
            "donor_template": self.donor_template,
            "candidates_scanned": self.candidates_scanned,
            "eligible_repairs": self.eligible_repairs,
            "repaired": self.repaired,
            "skipped_already_correct": self.skipped_already_correct,
            "rejected_non_cohort": self.rejected_non_cohort,
            "shards_changed": self.shards_changed,
            "shards_unchanged": self.shards_unchanged,
            "shards_failed": self.shards_failed,
            "failures": list(self.failures),
            "warnings": list(self.warnings),
            "repairs": list(self.repairs),
            "rejections": list(self.rejections),
            "post_write_audit": dict(self.post_write_audit),
        }


class ReviewColorRepairService:
    """Repair the confirmed Mini 5 Pro / Air 3 wrong Camera LUT cohort."""

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
        input_root: Path,
        output_root: Path,
        media_roots: Iterable[Path],
        report_path: Path,
        text_report_path: Path,
        dry_run: bool = False,
        overwrite: bool = False,
    ) -> ReviewColorRepairReport:
        input_root = input_root.expanduser().resolve()
        output_root = output_root.expanduser().resolve()
        report_path = report_path.expanduser().resolve()
        text_report_path = text_report_path.expanduser().resolve()
        media_roots = [Path(root).expanduser().resolve() for root in media_roots]
        if not input_root.is_dir():
            raise VClipError(f"Input root not found: {input_root}")
        if not media_roots:
            raise VClipError("At least one --media-root is required")
        if (
            output_root.exists()
            and any(output_root.iterdir())
            and not overwrite
            and not dry_run
        ):
            raise VClipError(
                f"Output root is not empty: {output_root} (pass --overwrite)"
            )

        self._announce(f"Scanning review corpus for color repair: {input_root}")
        shard_entries = self._discover_shards(input_root)
        identities = self._collect_identities(shard_entries)
        pairs = {(item["stockify_run_id"], item["stock_clip_id"]) for item in identities}
        rows = self.repository.candidates_by_run_and_ids(pairs)

        report = ReviewColorRepairReport(
            input_root=str(input_root),
            output_root=str(output_root),
            dry_run=dry_run,
            media_roots=[str(root) for root in media_roots],
            candidates_scanned=len(identities),
        )

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

        self._announce("Discovering canonical Mini 5 Pro Camera LUT template")
        template = discover_canonical_mini5_lut_template(shard_entries)
        if template is None:
            raise VClipError(
                "Could not find a known-good Mini 5 Pro D-Log M → Rec.709 "
                f"Camera LUT donor in {input_root}"
            )
        report.donor_template = template.as_dict()
        self._announce(
            f"Donor template from {template.donor_stock_clip_id} "
            f"({template.donor_shard})"
        )

        repairs: list[ColorRepairRow] = []
        rejections: list[dict[str, Any]] = []
        skipped_correct = 0
        project_index_by_shard: dict[str, dict[str, dict[str, Any]]] = {}

        for entry in shard_entries:
            relative = entry["relative_xml"]
            try:
                project_index_by_shard[relative] = self._index_projects(entry["xml_path"])
            except Exception as exc:  # noqa: BLE001
                report.failures.append({"relative_path": relative, "error": str(exc)})
                self._announce(f"FAILED parse {relative}: {exc}")

        for identity in identities:
            run_id = identity["stockify_run_id"]
            clip_id = identity["stock_clip_id"]
            row = rows.get((run_id, clip_id))
            if row is None:
                rejections.append(
                    {
                        "stock_clip_id": clip_id,
                        "stockify_run_id": run_id,
                        "reason": "db_missing",
                    }
                )
                continue
            project_info = project_index_by_shard.get(identity["relative_xml"], {}).get(
                clip_id
            )
            if project_info is None:
                rejections.append(
                    {
                        "stock_clip_id": clip_id,
                        "stockify_run_id": run_id,
                        "reason": "project_missing",
                    }
                )
                continue

            decision = evaluate_mini5_air3_repair_eligibility(
                row=row,
                project_info=project_info,
                srt_index=srt_index,
            )
            if decision["status"] == REPAIR_STATUS_SKIPPED:
                skipped_correct += 1
                continue
            if decision["status"] != REPAIR_STATUS_REPAIRED:
                rejections.append(
                    {
                        "stock_clip_id": clip_id,
                        "stockify_run_id": run_id,
                        "reason": decision.get("reason"),
                        "classification": decision.get("classification"),
                        "color_md": decision.get("color_md"),
                        "camera_model": decision.get("camera_model"),
                        "xml_identity": decision.get("xml_identity"),
                        "db_camera_lut": row.get("camera_lut"),
                    }
                )
                continue

            repairs.append(
                ColorRepairRow(
                    stockify_run_id=run_id,
                    stock_clip_id=clip_id,
                    source_media=decision.get("source_name"),
                    camera_model=TARGET_CAMERA_MODEL,
                    color_md=COLOR_MD_DLOG_M,
                    previous_camera_lut=row.get("camera_lut"),
                    new_camera_lut=TARGET_LUT_OVERRIDE,
                    previous_xml_lut_identity=decision.get("xml_identity"),
                    new_xml_lut_identity=TARGET_LUT_IDENTITY,
                    source_shard=identity["relative_xml"],
                    project_name=project_info.get("project_name")
                    or identity.get("project_name"),
                    input_xml=str(entry["xml_path"]),
                    output_xml=None if dry_run else str(output_root / identity["relative_xml"]),
                    repair_reason=REPAIR_REASON,
                    status=REPAIR_STATUS_REPAIRED,
                    srt_path=decision.get("srt_path"),
                    previous_effect_signature=row.get("effect_signature"),
                    provenance={
                        "classification": CLASS_WRONG,
                        "donor_stock_clip_id": template.donor_stock_clip_id,
                        "donor_shard": template.donor_shard,
                        "source_media_id": row.get("source_media_id"),
                        "camera_model_source": decision.get("camera_model_source"),
                    },
                )
            )

        report.eligible_repairs = len(repairs)
        report.skipped_already_correct = skipped_correct
        report.rejected_non_cohort = len(rejections)
        report.rejections = rejections

        by_shard: dict[str, list[ColorRepairRow]] = defaultdict(list)
        for item in repairs:
            by_shard[item.source_shard].append(item)

        if dry_run:
            self._announce(
                f"Dry run: would repair {len(repairs)} candidate(s) "
                f"across {len(by_shard)} shard(s)."
            )
            report.shards_changed = len(by_shard)
            report.shards_unchanged = max(0, len(shard_entries) - len(by_shard))
            report.repaired = len(repairs)
            report.repairs = [item.as_dict() for item in repairs]
            report.post_write_audit = {
                "mode": "dry_run",
                "would_repair": len(repairs),
                "eligible_shards": sorted(by_shard),
            }
        else:
            output_root.mkdir(parents=True, exist_ok=True)
            changed, unchanged, failures, signatures = self._write_corpus(
                shard_entries=shard_entries,
                output_root=output_root,
                repairs_by_shard=by_shard,
                template=template,
                overwrite=overwrite,
            )
            report.shards_changed = changed
            report.shards_unchanged = unchanged
            report.shards_failed = len(failures)
            report.failures.extend(failures)
            failed_shards = {item["relative_path"] for item in failures}
            persisted = [
                item for item in repairs if item.source_shard not in failed_shards
            ]
            for item in persisted:
                item.output_xml = str(output_root / item.source_shard)
                item.new_effect_signature = signatures.get(
                    (item.stockify_run_id, item.stock_clip_id)
                )
            self._persist_candidate_updates(persisted)
            self.catalog.record_review_color_repairs(repairs=persisted)
            report.repaired = len(persisted)
            report.repairs = [item.as_dict() for item in persisted]
            report.post_write_audit = self._post_write_audit(
                output_root=output_root,
                repairs=persisted,
                media_roots=media_roots,
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
        tree = parse_source(xml_path)
        root = tree.getroot()
        resources = first_direct_child(root, "resources")
        resource_index = build_resource_index(resources) if resources is not None else {}
        indexed: dict[str, dict[str, Any]] = {}
        for project in _iter_projects(root):
            clip = _primary_clip(project)
            if clip is None:
                continue
            metadata = read_vclip_metadata(clip)
            clip_id = metadata.get("com.vclip.stock_clip_id")
            if not clip_id:
                continue
            lut_details = collect_xml_lut_details(clip, resource_index)
            indexed[str(clip_id)] = {
                "project_name": project.get("name"),
                "clip": clip,
                "xml_lut_details": lut_details,
                "xml_normalized_lut_identity": next(
                    (
                        detail.get("normalized_lut_identity")
                        for detail in lut_details
                        if detail.get("normalized_lut_identity")
                    ),
                    None,
                ),
                "asset_custom_lut_override": next(
                    (
                        detail.get("asset_custom_lut_override")
                        for detail in lut_details
                        if detail.get("asset_custom_lut_override")
                    ),
                    None,
                ),
            }
        return indexed

    def _write_corpus(
        self,
        *,
        shard_entries: list[dict[str, Any]],
        output_root: Path,
        repairs_by_shard: dict[str, list[ColorRepairRow]],
        template: CanonicalCameraLutTemplate,
        overwrite: bool,
    ) -> tuple[int, int, list[dict[str, str]], dict[tuple[str, str], str]]:
        changed = 0
        unchanged = 0
        failures: list[dict[str, str]] = []
        signatures: dict[tuple[str, str], str] = {}
        for entry in shard_entries:
            relative = entry["relative_xml"]
            output_xml = output_root / relative
            try:
                if output_xml.exists() and not overwrite:
                    raise VClipError(f"Output exists: {output_xml}")
                output_xml.parent.mkdir(parents=True, exist_ok=True)
                shard_repairs = repairs_by_shard.get(relative, [])
                if not shard_repairs:
                    shutil.copy2(entry["xml_path"], output_xml)
                    shutil.copy2(
                        entry["manifest_path"],
                        output_xml.with_name(f"{output_xml.stem}-shard-manifest.json"),
                    )
                    unchanged += 1
                    continue
                tree = parse_source(entry["xml_path"])
                root = tree.getroot()
                applied = apply_camera_lut_repairs(
                    root,
                    repairs=shard_repairs,
                    template=template,
                )
                validation = validate_fcpxml(root)
                if not validation.passed:
                    raise VClipError(
                        "Color-repaired review XML failed FCPXML validation: "
                        + "; ".join(validation.errors[:10])
                    )
                for key, signature in applied.items():
                    signatures[key] = signature
                ET.indent(root)
                output_xml.write_bytes(
                    ET.tostring(root, encoding="utf-8", xml_declaration=True)
                )
                _rewrite_manifest(
                    manifest=entry["manifest"],
                    output_xml=output_xml,
                    repairs=shard_repairs,
                    template=template,
                )
                changed += 1
            except Exception as exc:  # noqa: BLE001
                failures.append({"relative_path": relative, "error": str(exc)})
                self._announce(f"FAILED {relative}: {exc}")
        return changed, unchanged, failures, signatures

    def _persist_candidate_updates(self, repairs: list[ColorRepairRow]) -> None:
        now = utc_now()
        with self.repository.database.transaction() as connection:
            for item in repairs:
                connection.execute(
                    """
                    UPDATE stock_candidates
                    SET camera_lut=?,
                        effect_signature=COALESCE(?, effect_signature),
                        updated_at=?
                    WHERE run_id=? AND stock_clip_id=?
                    """,
                    (
                        item.new_camera_lut,
                        item.new_effect_signature,
                        now,
                        item.stockify_run_id,
                        item.stock_clip_id,
                    ),
                )
                media_id = (item.provenance or {}).get("source_media_id")
                if media_id:
                    connection.execute(
                        """
                        UPDATE source_media
                        SET camera_lut=?, updated_at=?
                        WHERE id=?
                        """,
                        (item.new_camera_lut, now, media_id),
                    )

    def _post_write_audit(
        self,
        *,
        output_root: Path,
        repairs: list[ColorRepairRow],
        media_roots: list[Path],
    ) -> dict[str, Any]:
        if not repairs:
            return {
                "repaired": 0,
                "still_wrong_camera_lut": 0,
                "db_xml_mismatches": 0,
            }
        still_wrong = 0
        db_xml_mismatches = 0
        by_shard: dict[str, list[ColorRepairRow]] = defaultdict(list)
        for item in repairs:
            by_shard[item.source_shard].append(item)
        for relative, shard_repairs in by_shard.items():
            xml_path = output_root / relative
            tree = parse_source(xml_path)
            root = tree.getroot()
            resources = first_direct_child(root, "resources")
            resource_index = (
                build_resource_index(resources) if resources is not None else {}
            )
            for item in shard_repairs:
                clip = _find_clip(root, item.stock_clip_id)
                if clip is None:
                    still_wrong += 1
                    continue
                details = collect_xml_lut_details(clip, resource_index)
                identity = next(
                    (
                        detail.get("normalized_lut_identity")
                        for detail in details
                        if detail.get("normalized_lut_identity")
                    ),
                    None,
                )
                classification, _, _, agree = classify_dlog_candidate(
                    source_camera_model=TARGET_CAMERA_MODEL,
                    db_camera_lut=item.new_camera_lut,
                    xml_details=details,
                )
                if classification == CLASS_WRONG:
                    still_wrong += 1
                if identity != TARGET_LUT_IDENTITY or agree is False:
                    db_xml_mismatches += 1
        return {
            "repaired": len(repairs),
            "still_wrong_camera_lut": still_wrong,
            "db_xml_mismatches": db_xml_mismatches,
            "media_roots_used": [str(path) for path in media_roots],
        }

    def _write_reports(
        self,
        report: ReviewColorRepairReport,
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
        text_report_path.write_text(format_color_repair_text(report), encoding="utf-8")

    def _announce(self, message: str) -> None:
        if self.progress:
            self.progress(message)


def evaluate_mini5_air3_repair_eligibility(
    *,
    row: dict[str, Any],
    project_info: dict[str, Any],
    srt_index: dict[str, list[Path]],
) -> dict[str, Any]:
    """Return repair decision for the Mini5←Air3 cohort only."""
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
    camera_model, camera_evidence = detect_camera_model(
        str(media_path) if media_path else None,
        srt_path,
        source_name_str,
    )
    xml_details = list(project_info.get("xml_lut_details") or [])
    xml_identity = project_info.get("xml_normalized_lut_identity")
    db_lut = row.get("camera_lut")
    db_identity = normalize_lut_identity(db_lut)
    classification, _, _, db_xml_agree = classify_dlog_candidate(
        source_camera_model=camera_model,
        db_camera_lut=db_lut,
        xml_details=xml_details,
    )

    base = {
        "color_md": color_md,
        "camera_model": camera_model,
        "camera_model_source": camera_evidence,
        "srt_path": srt_path,
        "source_name": source_name_str,
        "xml_identity": xml_identity,
        "classification": classification,
        "db_xml_agree": db_xml_agree,
    }

    if (
        color_md == COLOR_MD_DLOG_M
        and camera_model == TARGET_CAMERA_MODEL
        and xml_identity == TARGET_LUT_IDENTITY
        and db_identity == TARGET_LUT_IDENTITY
    ):
        return {**base, "status": REPAIR_STATUS_SKIPPED, "reason": "already_correct"}

    if color_md != COLOR_MD_DLOG_M:
        return {**base, "status": REPAIR_STATUS_REJECTED, "reason": "color_md_not_dlog_m"}
    if camera_model != TARGET_CAMERA_MODEL:
        return {
            **base,
            "status": REPAIR_STATUS_REJECTED,
            "reason": "camera_model_not_mini5_pro",
        }
    if classification != CLASS_WRONG:
        return {
            **base,
            "status": REPAIR_STATUS_REJECTED,
            "reason": "not_dlog_wrong_camera_lut",
        }
    if xml_identity != WRONG_LUT_IDENTITY:
        return {
            **base,
            "status": REPAIR_STATUS_REJECTED,
            "reason": "xml_identity_not_air3_wrong_lut",
        }
    if db_identity != WRONG_LUT_IDENTITY:
        return {
            **base,
            "status": REPAIR_STATUS_REJECTED,
            "reason": "db_camera_lut_not_air3_wrong_lut",
        }
    if db_xml_agree is not True:
        return {
            **base,
            "status": REPAIR_STATUS_REJECTED,
            "reason": "db_xml_disagree_on_wrong_lut",
        }
    return {**base, "status": REPAIR_STATUS_REPAIRED, "reason": REPAIR_REASON}


def discover_canonical_mini5_lut_template(
    shard_entries: list[dict[str, Any]],
) -> CanonicalCameraLutTemplate | None:
    """Find a surviving Mini 5 Pro conversion LUT and copy its XML representation."""
    for entry in shard_entries:
        tree = parse_source(entry["xml_path"])
        root = tree.getroot()
        resources = first_direct_child(root, "resources")
        if resources is None:
            continue
        resource_index = build_resource_index(resources)
        for project in _iter_projects(root):
            clip = _primary_clip(project)
            if clip is None:
                continue
            metadata = read_vclip_metadata(clip)
            clip_id = metadata.get("com.vclip.stock_clip_id")
            if not clip_id:
                continue
            asset_ref = clip.get("ref")
            asset = resource_index.get(asset_ref) if asset_ref else None
            override = asset_conversion_lut(asset)
            if normalize_lut_identity(override) != TARGET_LUT_IDENTITY:
                continue
            if (override or "").strip() != TARGET_LUT_OVERRIDE:
                # Accept equivalent identity even if hash formatting differs slightly.
                if TARGET_LUT_IDENTITY not in (override or ""):
                    continue
            for child in list(clip):
                if local_name(child.tag) != "filter-video":
                    continue
                if not is_camera_conversion_lut_filter(child, resource_index):
                    continue
                effect = resource_index.get(child.get("ref") or "")
                params = [
                    {
                        "name": param.get("name") or "",
                        "key": param.get("key") or "",
                        "value": param.get("value") or "",
                    }
                    for param in list(child)
                    if local_name(param.tag) == "param"
                ]
                if not params:
                    continue
                return CanonicalCameraLutTemplate(
                    custom_lut_override=override or TARGET_LUT_OVERRIDE,
                    effect_name=(
                        child.get("name")
                        or (effect.get("name") if effect is not None else None)
                        or "Custom LUT"
                    ),
                    effect_uid=(
                        effect.get("uid")
                        if effect is not None
                        else "FxPlug:14B39AEF-607D-42DF-98DD-DB3DD345E925"
                    ),
                    filter_name=child.get("name") or "Custom LUT",
                    params=params,
                    donor_stock_clip_id=str(clip_id),
                    donor_shard=entry["relative_xml"],
                    donor_asset_id=asset.get("id") if asset is not None else None,
                )
    return None


def is_camera_conversion_lut_filter(
    filter_video: ET.Element,
    resource_index: dict[str, ET.Element],
) -> bool:
    """True for technical Custom LUT conversion filters (opaque LUT + Rec.709 IO)."""
    if not is_custom_lut_filter(filter_video, resource_index):
        return False
    has_opaque_lut = False
    has_rec709_io = 0
    for param in list(filter_video):
        if local_name(param.tag) != "param":
            continue
        name = (param.get("name") or "").casefold()
        key = param.get("key") or ""
        value = param.get("value") or ""
        if (name == "lut" or key == "3") and _looks_opaque_lut(value):
            has_opaque_lut = True
        if name in {"input", "output"} and "rec" in value.casefold() and "709" in value:
            has_rec709_io += 1
        if key in {"100/101", "100/102"} and "709" in value:
            has_rec709_io += 1
    return has_opaque_lut and has_rec709_io >= 2


def apply_camera_lut_repairs(
    root: ET.Element,
    *,
    repairs: list[ColorRepairRow],
    template: CanonicalCameraLutTemplate,
) -> dict[tuple[str, str], str]:
    """Mutate Camera LUT conversion on eligible clips; preserve unrelated effects."""
    resources = first_direct_child(root, "resources")
    if resources is None:
        raise VClipError("FCPXML missing <resources>")
    resource_index = build_resource_index(resources)
    repair_ids = {item.stock_clip_id: item for item in repairs}
    signatures: dict[tuple[str, str], str] = {}
    updated_assets: set[str] = set()

    for project in _iter_projects(root):
        clip = _primary_clip(project)
        if clip is None:
            continue
        metadata = read_vclip_metadata(clip)
        clip_id = metadata.get("com.vclip.stock_clip_id")
        if not clip_id or clip_id not in repair_ids:
            continue
        item = repair_ids[clip_id]
        asset_ref = clip.get("ref")
        asset = resource_index.get(asset_ref) if asset_ref else None
        if asset is None:
            raise VClipError(f"Missing asset for {clip_id}")
        asset_id = asset.get("id") or asset_ref or ""
        if asset_id not in updated_assets:
            asset.set("customLUTOverride", template.custom_lut_override)
            updated_assets.add(asset_id)

        effect_id = _ensure_custom_lut_effect(
            resources,
            resource_index,
            effect_name=template.effect_name,
            effect_uid=template.effect_uid,
        )
        replaced = 0
        for child in list(clip):
            if local_name(child.tag) != "filter-video":
                continue
            if not is_camera_conversion_lut_filter(child, resource_index):
                continue
            child.set("ref", effect_id)
            child.set("name", template.filter_name)
            for param in list(child):
                if local_name(param.tag) == "param":
                    child.remove(param)
            for param in template.params:
                node = ET.SubElement(child, _tagged(child, "param"))
                if param.get("name"):
                    node.set("name", param["name"])
                if param.get("key"):
                    node.set("key", param["key"])
                if param.get("value") is not None:
                    node.set("value", param["value"])
            replaced += 1
        if replaced == 0:
            raise VClipError(
                f"No camera-conversion Custom LUT filter found to repair on {clip_id}"
            )
        signatures[(item.stockify_run_id, clip_id)] = video_treatment_signature(clip)
    return signatures


def _ensure_custom_lut_effect(
    resources: ET.Element,
    resource_index: dict[str, ET.Element],
    *,
    effect_name: str,
    effect_uid: str,
) -> str:
    for resource_id, resource in resource_index.items():
        if local_name(resource.tag) != "effect":
            continue
        if (resource.get("uid") or "") == effect_uid and "lut" in (
            resource.get("name") or ""
        ).casefold():
            return resource_id
    new_id = _next_resource_id(resource_index)
    effect = ET.SubElement(resources, _tagged(resources, "effect"))
    effect.set("id", new_id)
    effect.set("name", effect_name)
    effect.set("uid", effect_uid)
    resource_index[new_id] = effect
    return new_id


def _next_resource_id(resource_index: dict[str, ET.Element]) -> str:
    max_num = 0
    for resource_id in resource_index:
        if resource_id.startswith("r") and resource_id[1:].isdigit():
            max_num = max(max_num, int(resource_id[1:]))
    return f"r{max_num + 1}"


def _tagged(reference: ET.Element, name: str) -> str:
    if isinstance(reference.tag, str) and reference.tag.startswith("{"):
        uri = reference.tag[1:].split("}", 1)[0]
        return f"{{{uri}}}{name}"
    return name


def _looks_opaque_lut(value: str) -> bool:
    lower = value.casefold()
    return lower.startswith("pd94b") or "ozxml" in lower or len(value) > 240


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


def _find_clip(root: ET.Element, stock_clip_id: str) -> ET.Element | None:
    for node in root.iter():
        if local_name(node.tag) not in {
            "asset-clip",
            "ref-clip",
            "sync-clip",
            "mc-clip",
        }:
            continue
        metadata = read_vclip_metadata(node)
        if metadata.get("com.vclip.stock_clip_id") == stock_clip_id:
            return node
    return None


def _rewrite_manifest(
    *,
    manifest: dict[str, Any],
    output_xml: Path,
    repairs: list[ColorRepairRow],
    template: CanonicalCameraLutTemplate,
) -> None:
    payload = copy.deepcopy(manifest)
    payload["color_repair"] = {
        "repaired_stock_clip_ids": [item.stock_clip_id for item in repairs],
        "repair_reason": REPAIR_REASON,
        "target_camera_lut": template.custom_lut_override,
        "donor_stock_clip_id": template.donor_stock_clip_id,
        "donor_shard": template.donor_shard,
        "count": len(repairs),
    }
    output_xml.with_name(f"{output_xml.stem}-shard-manifest.json").write_text(
        json_dumps(payload) + "\n",
        encoding="utf-8",
    )


def format_color_repair_text(report: ReviewColorRepairReport) -> str:
    lines = [
        "REVIEW COLOR REPAIR (Mini 5 Pro ← Air 3 wrong Camera LUT)",
        "=" * 100,
        f"Input:     {report.input_root}",
        f"Output:    {report.output_root}",
        f"Dry run:   {report.dry_run}",
        f"Scanned:   {report.candidates_scanned:,}",
        f"Eligible:  {report.eligible_repairs:,}",
        f"Repaired:  {report.repaired:,}",
        f"Skipped already-correct: {report.skipped_already_correct:,}",
        f"Rejected non-cohort:     {report.rejected_non_cohort:,}",
        f"Shards changed/unchanged/failed: "
        f"{report.shards_changed}/{report.shards_unchanged}/{report.shards_failed}",
        "",
    ]
    donor = report.donor_template or {}
    if donor:
        lines.extend(
            [
                "Canonical donor template",
                "-" * 100,
                f"clip:     {donor.get('donor_stock_clip_id')}",
                f"shard:    {donor.get('donor_shard')}",
                f"override: {donor.get('custom_lut_override')}",
                f"effect:   {donor.get('effect_name')} | {donor.get('effect_uid')}",
                f"params:   {len(donor.get('params') or [])}",
                "",
            ]
        )
    audit = report.post_write_audit or {}
    if audit:
        lines.extend(
            [
                "Post-write audit",
                "-" * 100,
                f"still_wrong_camera_lut: {audit.get('still_wrong_camera_lut')}",
                f"db_xml_mismatches:      {audit.get('db_xml_mismatches')}",
                "",
            ]
        )
    if report.repairs:
        lines.extend(["Repaired samples", "-" * 100])
        for item in report.repairs[:20]:
            lines.append(
                f"- {item.get('stock_clip_id')} | {item.get('source_media')} | "
                f"{item.get('source_shard')} | "
                f"{item.get('previous_xml_lut_identity')} → "
                f"{item.get('new_xml_lut_identity')}"
            )
        lines.append("")
    if report.failures:
        lines.extend(["Failures", "-" * 100])
        for item in report.failures:
            lines.append(f"- {item.get('relative_path')}: {item.get('error')}")
        lines.append("")
    rejection_reasons = Counter(
        item.get("reason") or "unknown" for item in report.rejections
    )
    if rejection_reasons:
        lines.extend(["Rejection reasons", "-" * 100])
        for reason, count in rejection_reasons.most_common():
            lines.append(f"{reason:<40} {count:>6,}")
        lines.append("")
    return "\n".join(lines)
