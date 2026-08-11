"""D-Log M Camera LUT integrity helpers for review-color-integrity."""

from __future__ import annotations

import csv
import hashlib
import os
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from ..stockify.core import local_name
from ..stockify.fcpxml import asset_conversion_lut, is_custom_lut_filter
from ..stockify.sidecars import (
    extract_srt_color_md,
    normalized_stem,
    sidecar_stem_variants,
)

# FCP duplicate / range suffixes that break basename↔SRT stem equality.
_FCP_MEDIA_SUFFIX_RE = re.compile(
    r"(?:\s*\(fcp\d+\))"  # " (fcp1)"
    r"|(?:\s*\[[^\]]*\])"  # " [00002945 +2518]"
    r"|(?:\s*copy(?:\s+\d+)?)$",
    re.I,
)

COLOR_MD_DLOG_M = "dlog_m"

CLASS_CORRECT = "DLOG_CORRECT_CAMERA_LUT"
CLASS_WRONG = "DLOG_WRONG_CAMERA_LUT"
CLASS_DB_XML_MISSING = "DLOG_DB_LUT_XML_MISSING"
CLASS_XML_DB_MISSING = "DLOG_XML_LUT_DB_MISSING"
CLASS_UNKNOWN_IDENTITY = "DLOG_CUSTOM_LUT_UNKNOWN_IDENTITY"
CLASS_NO_LUT = "DLOG_NO_CAMERA_LUT"

_DLOG_CLASSES = (
    CLASS_CORRECT,
    CLASS_WRONG,
    CLASS_DB_XML_MISSING,
    CLASS_XML_DB_MISSING,
    CLASS_UNKNOWN_IDENTITY,
    CLASS_NO_LUT,
)

# Ordered longest-first so "Mini 5 Pro" wins over bare "Mini".
_CAMERA_MODEL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"dji\s*mini\s*5\s*pro", re.I), "DJI Mini 5 Pro"),
    (re.compile(r"dji\s*mini\s*4\s*pro", re.I), "DJI Mini 4 Pro"),
    (re.compile(r"dji\s*mini\s*3\s*pro", re.I), "DJI Mini 3 Pro"),
    (re.compile(r"dji\s*air\s*3\s*s", re.I), "DJI Air 3S"),
    (re.compile(r"dji\s*air\s*3", re.I), "DJI Air 3"),
    (re.compile(r"dji\s*air\s*2\s*s", re.I), "DJI Air 2S"),
    (re.compile(r"dji\s*mavic\s*3", re.I), "DJI Mavic 3"),
)

_DLOG_TO_REC709_RE = re.compile(
    r"d[-\s]?log\s*m?.{0,24}rec\.?\s*709|rec\.?\s*709.{0,24}d[-\s]?log",
    re.I,
)


@dataclass
class LutSignature:
    signature_key: str
    effect_name: str | None
    effect_uid: str | None
    filter_attributes: dict[str, str]
    params: list[dict[str, str]]
    normalized_lut_identity: str | None
    lut_camera_model: str | None
    asset_custom_lut_override: str | None = None
    lut_data_fingerprint: str | None = None
    occurrence_count: int = 0
    example_stock_clip_ids: list[str] = field(default_factory=list)
    example_projects: list[str] = field(default_factory=list)
    source_camera_models: Counter = field(default_factory=Counter)

    def as_dict(self) -> dict[str, Any]:
        return {
            "signature_key": self.signature_key,
            "effect_name": self.effect_name,
            "effect_uid": self.effect_uid,
            "filter_attributes": dict(self.filter_attributes),
            "params": list(self.params),
            "asset_custom_lut_override": self.asset_custom_lut_override,
            "lut_data_fingerprint": self.lut_data_fingerprint,
            "normalized_lut_identity": self.normalized_lut_identity,
            "lut_camera_model": self.lut_camera_model,
            "occurrence_count": self.occurrence_count,
            "example_stock_clip_ids": list(self.example_stock_clip_ids[:10]),
            "example_projects": list(self.example_projects[:10]),
            "source_camera_models": dict(self.source_camera_models),
        }


@dataclass
class DlogAuditRecord:
    stock_clip_id: str
    stockify_run_id: str
    source_name: str | None
    capture_date: str | None
    color_md: str
    camera_model: str | None
    camera_model_source: str | None
    shard_path: str | None
    project_name: str | None
    db_camera_lut: str | None
    xml_custom_luts: list[str]
    xml_normalized_lut_identity: str | None
    xml_lut_camera_model: str | None
    db_xml_agree: bool | None
    classification: str
    year_month: str | None
    historical_bucket: str | None
    srt_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def index_srts_by_basename(
    media_roots: Iterable[Path],
    needed_stems: set[str] | None = None,
) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = defaultdict(list)
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
                if needed_stems is not None and stem not in needed_stems:
                    continue
                index[stem].append(Path(dirpath) / filename)
    return {stem: sorted(paths) for stem, paths in index.items()}


def srt_lookup_stems(*values: str | None) -> list[str]:
    """Ordered unique stems to try when joining media names to physical SRTs."""
    ordered: list[str] = []
    seen: set[str] = set()

    def _add(stem: str) -> None:
        if not stem or stem in seen:
            return
        seen.add(stem)
        ordered.append(stem)

    for value in values:
        stem = normalized_stem(value)
        if not stem:
            continue
        _add(stem)
        for variant in sidecar_stem_variants(stem):
            _add(variant)
        cleaned = stem
        while True:
            next_cleaned = _FCP_MEDIA_SUFFIX_RE.sub("", cleaned).strip()
            if next_cleaned == cleaned:
                break
            cleaned = next_cleaned
            _add(cleaned)
            for variant in sidecar_stem_variants(cleaned):
                _add(variant)
    return ordered


def color_md_for_source(
    source_basename: str | None,
    srt_index: dict[str, list[Path]],
    *extra_names: str | None,
) -> tuple[str | None, str | None]:
    """Return (color_md literal, chosen srt path) for a source basename."""
    for stem in srt_lookup_stems(source_basename, *extra_names):
        for path in srt_index.get(stem, []):
            value = extract_srt_color_md(path)
            if value:
                return value, str(path)
        paths = srt_index.get(stem) or []
        if paths:
            return None, str(paths[0])
    return None, None


def detect_camera_model(*texts: str | None) -> tuple[str | None, str | None]:
    """Return (canonical model, evidence snippet) from trustworthy path/text evidence."""
    for text in texts:
        if not text:
            continue
        for pattern, model in _CAMERA_MODEL_PATTERNS:
            match = pattern.search(str(text))
            if match:
                return model, match.group(0)
    return None, None


def normalize_lut_identity(*texts: str | None) -> str | None:
    """Normalize discovered Custom LUT / camera-LUT labels for comparison."""
    for text in texts:
        if not text:
            continue
        raw = str(text).strip()
        if not raw:
            continue
        # FCP often stores "LUT:<hash> (<human label>)".
        paren = re.search(r"\(([^)]+)\)\s*$", raw)
        if paren and (
            "d-log" in paren.group(1).casefold()
            or "dlog" in paren.group(1).casefold()
            or "rec.709" in paren.group(1).casefold()
            or "lut" in paren.group(1).casefold()
        ):
            cleaned = re.sub(r"\s+", " ", paren.group(1)).strip()
            if cleaned:
                return cleaned
        cleaned = re.sub(r"^lut\s*:\s*[0-9a-f]{8,}\s*", "", raw, flags=re.I)
        cleaned = re.sub(r"^lut\s*:\s*", "", cleaned, flags=re.I)
        cleaned = cleaned.strip("()[] ")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            continue
        lower = cleaned.casefold()
        # Ignore opaque ozxml/base64 Custom LUT payloads and report fingerprints.
        if (
            lower.startswith("pd94b")
            or "ozxml" in lower
            or lower.startswith("<opaque-lut-data")
            or len(cleaned) > 240
        ):
            continue
        if lower in {"custom lut", "lut"}:
            continue
        if "lut" not in lower and "d-log" not in lower and "dlog" not in lower:
            if "rec.709" not in lower and "rec709" not in lower:
                continue
        return cleaned
    return None


def lut_identity_camera_model(identity: str | None) -> str | None:
    model, _ = detect_camera_model(identity)
    return model


def is_dlog_m_to_rec709_identity(identity: str | None) -> bool:
    if not identity:
        return False
    return bool(_DLOG_TO_REC709_RE.search(identity))


def historical_bucket(capture_date: str | None) -> str:
    if not capture_date:
        return "unknown"
    try:
        parsed = date.fromisoformat(capture_date[:10])
    except ValueError:
        return "unknown"
    if parsed.year <= 2024:
        return "through_2024"
    if parsed.year == 2025 and parsed.month == 1:
        return "2025-01"
    if parsed.year == 2025 and parsed.month == 2:
        return "2025-02"
    if parsed.year == 2025 and parsed.month == 3:
        return "2025-03"
    if parsed.year == 2025 and parsed.month == 4:
        return "2025-04"
    return "2025-05_onward"


def collect_xml_lut_details(
    clip,
    resource_index: dict[str, Any],
) -> list[dict[str, Any]]:
    """Collect Custom LUT filter signatures from a clip using production helpers."""
    details: list[dict[str, Any]] = []
    asset_ref = clip.get("ref")
    asset = resource_index.get(asset_ref) if asset_ref else None
    asset_lut = asset_conversion_lut(asset)
    for child in list(clip):
        if local_name(child.tag) != "filter-video":
            continue
        if not is_custom_lut_filter(child, resource_index):
            continue
        ref = child.get("ref")
        resource = resource_index.get(ref) if ref else None
        raw_params = [
            {
                "name": param.get("name") or "",
                "key": param.get("key") or "",
                "value": param.get("value") or "",
            }
            for param in list(child)
            if local_name(param.tag) == "param"
        ]
        filter_attributes = {
            key: value for key, value in child.attrib.items() if value is not None
        }
        # Prefer human-readable identities from asset override / LUT Name params.
        # Never use opaque ozxml/base64 LUT data blobs as identity text.
        identity = normalize_lut_identity(
            asset_lut,
            *(
                param.get("value")
                for param in raw_params
                if not _looks_opaque(param.get("value"))
            ),
            child.get("name"),
            resource.get("name") if resource is not None else None,
        )
        data_fingerprint = _lut_data_fingerprint(raw_params)
        details.append(
            {
                "effect_name": (
                    child.get("name")
                    or (resource.get("name") if resource is not None else None)
                    or "Custom LUT"
                ),
                "effect_uid": resource.get("uid") if resource is not None else None,
                "effect_resource_name": (
                    resource.get("name") if resource is not None else None
                ),
                "filter_attributes": filter_attributes,
                "params": [
                    {
                        "name": param["name"],
                        "key": param["key"],
                        "value": _compact_param_value(param["value"]),
                    }
                    for param in raw_params
                ],
                "asset_custom_lut_override": asset_lut,
                "lut_data_fingerprint": data_fingerprint,
                "normalized_lut_identity": identity,
                "lut_camera_model": lut_identity_camera_model(identity),
            }
        )
    return details


def _looks_opaque(value: str | None) -> bool:
    if not value:
        return False
    lower = value.casefold()
    if value.startswith("<opaque-lut-data"):
        return True
    return lower.startswith("pd94b") or "ozxml" in lower or len(value) > 240


def _compact_param_value(value: str) -> str:
    """Keep reports readable: opaque ozxml payloads become fingerprints."""
    if not _looks_opaque(value):
        return value
    digest = hashlib.sha1(value.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"<opaque-lut-data sha1={digest} len={len(value)}>"


def _lut_data_fingerprint(params: list[dict[str, str]]) -> str | None:
    for param in params:
        name = (param.get("name") or "").casefold()
        key = (param.get("key") or "").casefold()
        value = param.get("value") or ""
        if name == "lut" or key == "3" or _looks_opaque(value):
            if value.startswith("<opaque-lut-data"):
                return value
            if value:
                digest = hashlib.sha1(
                    value.encode("utf-8", errors="replace")
                ).hexdigest()[:16]
                return f"sha1:{digest}"
    return None


def signature_key_for_detail(detail: dict[str, Any]) -> str:
    identity = detail.get("normalized_lut_identity") or ""
    uid = detail.get("effect_uid") or ""
    name = detail.get("effect_name") or ""
    fingerprint = detail.get("lut_data_fingerprint") or ""
    override = detail.get("asset_custom_lut_override") or ""
    return f"{name}||{uid}||{identity}||{fingerprint}||{override}"


def classify_dlog_candidate(
    *,
    source_camera_model: str | None,
    db_camera_lut: str | None,
    xml_details: list[dict[str, Any]],
) -> tuple[str, str | None, str | None, bool | None]:
    """Return (classification, xml_identity, xml_lut_model, db_xml_agree)."""
    db_has = _db_has_lut(db_camera_lut)
    xml_has = bool(xml_details)
    identities = [
        detail.get("normalized_lut_identity")
        for detail in xml_details
        if detail.get("normalized_lut_identity")
    ]
    xml_identity = identities[0] if identities else None
    xml_lut_model = None
    for detail in xml_details:
        if detail.get("lut_camera_model"):
            xml_lut_model = detail["lut_camera_model"]
            break
    db_identity = normalize_lut_identity(db_camera_lut)
    db_xml_agree: bool | None
    if not db_has and not xml_has:
        db_xml_agree = True
    elif db_has and xml_has:
        db_xml_agree = bool(
            db_identity
            and xml_identity
            and db_identity.casefold() == xml_identity.casefold()
        ) or (
            bool(db_camera_lut)
            and bool(xml_identity)
            and xml_identity.casefold() in str(db_camera_lut).casefold()
        )
    else:
        db_xml_agree = False

    if not xml_has and not db_has:
        return CLASS_NO_LUT, xml_identity, xml_lut_model, db_xml_agree
    if not xml_has and db_has:
        return CLASS_DB_XML_MISSING, xml_identity, xml_lut_model, db_xml_agree
    if xml_has and not identities:
        return CLASS_UNKNOWN_IDENTITY, xml_identity, xml_lut_model, db_xml_agree

    recognized = [
        identity
        for identity in identities
        if is_dlog_m_to_rec709_identity(identity)
    ]
    if not recognized:
        return CLASS_UNKNOWN_IDENTITY, xml_identity, xml_lut_model, db_xml_agree

    if source_camera_model is None:
        # Recognized conversion LUT but source camera unknown — do not guess.
        return CLASS_UNKNOWN_IDENTITY, xml_identity, xml_lut_model, db_xml_agree

    if xml_lut_model is None:
        return CLASS_UNKNOWN_IDENTITY, xml_identity, xml_lut_model, db_xml_agree
    if xml_lut_model.casefold() != source_camera_model.casefold():
        return CLASS_WRONG, xml_identity, xml_lut_model, db_xml_agree
    if not db_has:
        # Correct XML Camera LUT is authoritative; still flag DB gap separately.
        return CLASS_XML_DB_MISSING, xml_identity, xml_lut_model, db_xml_agree
    return CLASS_CORRECT, xml_identity, xml_lut_model, db_xml_agree


def _db_has_lut(value: Any) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    lower = text.casefold().replace(" ", "")
    if lower in {"0", "none", "(none)", "0(none)"}:
        return False
    if "rawtologconversion=0" in lower:
        return False
    return True


def summarize_dlog_records(
    records: list[DlogAuditRecord],
) -> dict[str, Any]:
    by_class = Counter(item.classification for item in records)
    by_month: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_bucket: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for item in records:
        month = item.year_month or "unknown"
        bucket = item.historical_bucket or "unknown"
        by_month[month][item.classification] += 1
        by_month[month]["total"] += 1
        by_bucket[bucket][item.classification] += 1
        by_bucket[bucket]["total"] += 1
    bucket_order = [
        "through_2024",
        "2025-01",
        "2025-02",
        "2025-03",
        "2025-04",
        "2025-05_onward",
        "unknown",
    ]
    return {
        "dlog_m_candidates": len(records),
        "classification_counts": {key: by_class.get(key, 0) for key in _DLOG_CLASSES},
        "by_year_month": {
            key: dict(by_month[key]) for key in sorted(by_month)
        },
        "by_historical_bucket": {
            key: dict(by_bucket.get(key, {})) for key in bucket_order if key in by_bucket
        },
    }


def write_dlog_csv(path: Path, records: list[DlogAuditRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "stock_clip_id",
        "stockify_run_id",
        "source_name",
        "capture_date",
        "color_md",
        "camera_model",
        "camera_model_source",
        "shard_path",
        "project_name",
        "db_camera_lut",
        "xml_custom_luts",
        "xml_normalized_lut_identity",
        "xml_lut_camera_model",
        "db_xml_agree",
        "classification",
        "year_month",
        "historical_bucket",
        "srt_path",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in records:
            row = item.as_dict()
            row["xml_custom_luts"] = " | ".join(item.xml_custom_luts)
            writer.writerow(row)


def format_dlog_audit_text(
    *,
    signatures: list[dict[str, Any]],
    dlog_summary: dict[str, Any],
    records: list[DlogAuditRecord] | list[dict[str, Any]],
) -> str:
    lines = [
        "D-LOG M CAMERA LUT INTEGRITY AUDIT",
        "=" * 100,
        "",
        "1) Distinct Custom LUT / camera-LUT signatures in surviving projects",
        "-" * 100,
        f"Distinct signatures: {len(signatures):,}",
        "",
    ]
    for index, signature in enumerate(signatures, start=1):
        lines.extend(
            [
                (
                    f"{index}. occurrences={signature.get('occurrence_count')} | "
                    f"identity={signature.get('normalized_lut_identity')!r} | "
                    f"lut_model={signature.get('lut_camera_model')}"
                ),
                f"   effect_name: {signature.get('effect_name')}",
                f"   effect_uid:  {signature.get('effect_uid')}",
                f"   asset_override: {signature.get('asset_custom_lut_override')}",
                f"   lut_data_fp: {signature.get('lut_data_fingerprint')}",
                f"   filter_attrs:{signature.get('filter_attributes')}",
                f"   params:      {signature.get('params')}",
                (
                    f"   examples:    "
                    f"{', '.join(signature.get('example_stock_clip_ids') or [])}"
                ),
                (
                    f"   projects:    "
                    f"{', '.join(signature.get('example_projects') or [])}"
                ),
                (
                    f"   source cams: "
                    f"{signature.get('source_camera_models')}"
                ),
                "",
            ]
        )

    counts = dlog_summary.get("classification_counts") or {}
    lines.extend(
        [
            "2) Definitive color_md=dlog_m candidate classifications",
            "-" * 100,
            f"D-Log M candidates: {dlog_summary.get('dlog_m_candidates', 0):,}",
        ]
    )
    for key in _DLOG_CLASSES:
        lines.append(f"{key:<32} {counts.get(key, 0):>7,}")

    lines.extend(
        [
            "",
            "Historical breakdown",
            "-" * 100,
        ]
    )
    for bucket, bucket_counts in (dlog_summary.get("by_historical_bucket") or {}).items():
        total = bucket_counts.get("total", 0)
        correct = bucket_counts.get(CLASS_CORRECT, 0)
        no_lut = bucket_counts.get(CLASS_NO_LUT, 0)
        db_only = bucket_counts.get(CLASS_DB_XML_MISSING, 0)
        lines.append(
            f"{bucket:<16} total={total:<5} correct={correct:<5} "
            f"no_xml_lut={no_lut + db_only:<5} "
            f"wrong={bucket_counts.get(CLASS_WRONG, 0):<5} "
            f"unknown_id={bucket_counts.get(CLASS_UNKNOWN_IDENTITY, 0):<5}"
        )

    lines.extend(["", "Sample D-Log findings", "-" * 100])
    normalized_records = [
        item.as_dict() if isinstance(item, DlogAuditRecord) else item for item in records
    ]
    for classification in _DLOG_CLASSES:
        samples = [
            item
            for item in normalized_records
            if item.get("classification") == classification
        ][:5]
        if not samples:
            continue
        lines.append(f"{classification}:")
        for item in samples:
            lines.append(
                f"  - {item.get('stock_clip_id')} | {item.get('source_name')} | "
                f"cam={item.get('camera_model')} | "
                f"xml={item.get('xml_normalized_lut_identity')} | "
                f"{item.get('capture_date')} | {item.get('shard_path')}"
            )
        lines.append("")
    return "\n".join(lines)
