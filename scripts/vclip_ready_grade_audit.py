#!/usr/bin/env python3
"""Audit deterministic color-grade state for reconstructed VClip ready cuts.

A ready cut is considered fully production graded only when the available
evidence supports the complete grade chain:

1. Source color mode is known.
2. If source is DJI D-Log M, the correct camera conversion LUT is applied.
3. At least one enabled Final Cut Custom LUT filter has a non-default LUT chosen.
4. If an approved creative-palette registry is supplied, every selected creative
   LUT is explicitly approved.

The audit is read-only. It emits both per-appearance and deduplicated per-VClip
reports, plus creative LUT usage/stack summaries and a registry template.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from vclip_pipeline.stockify.fcpxml import (
    build_resource_index,
    first_direct_child,
    local_name,
    read_vclip_metadata,
)

XML_EXTENSIONS = {".fcpxml", ".xml"}
CUSTOM_LUT_UID = "FxPlug:14B39AEF-607D-42DF-98DD-DB3DD345E925"
CAMERA_LUT_RE = re.compile(r"^LUT:([^ ]+) \((.*)\)$")

KNOWN_CAMERA_LUTS: dict[str, dict[str, str]] = {
    "944ff715997edde7b09b7b767fd51df2": {
        "camera_family": "DJI Air 3",
        "name": "DJI Air 3 D-Log M to Rec.709 V1_",
    },
    "908403d40286925c5b19129c4be6c0f4": {
        "camera_family": "DJI Mini 5 Pro",
        "name": "DJI Mini 5 Pro D-Log M to Rec.709 LUT",
    },
    "e4ec75ff88a16f2fff92080e19eb16db": {
        "camera_family": "DJI OSMO Pocket 3",
        "name": "DJI OSMO Pocket 3 D-Log M to Rec.709 V1",
    },
    "8f34ac94d7264b05a359d088adef7c3b": {
        "camera_family": "DJI Mini 4 Pro",
        "name": "DJI Mini 4 Pro D-Log M to Rec.709 V1_",
    },
}


@dataclass(frozen=True)
class SourceEvidence:
    source_name: str
    color_modes: tuple[str, ...]
    aircraft_names: tuple[str, ...]
    srt_paths: tuple[str, ...]


def norm_source(value: str | None) -> str:
    if not value:
        return ""
    return Path(value).stem.casefold()


def norm_text(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def is_dlog_m(value: str | None) -> bool:
    return "dlogm" in norm_text(value)


def is_none_camera_lut(value: str | None) -> bool:
    if not value:
        return True
    return value.strip().casefold() in {"0", "0 (none)", "none"}


def camera_lut_parts(raw: str | None) -> tuple[str, str]:
    if not raw:
        return "", ""
    match = CAMERA_LUT_RE.match(raw.strip())
    if not match:
        return "", raw.strip()
    return match.group(1), match.group(2)


def canonical_camera_family(value: str | None) -> str:
    text = norm_text(value)
    if "air3" in text:
        return "DJI Air 3"
    if "mini5pro" in text:
        return "DJI Mini 5 Pro"
    if "mini4pro" in text:
        return "DJI Mini 4 Pro"
    if "pocket3" in text or "osmopocket3" in text:
        return "DJI OSMO Pocket 3"
    return ""


def expected_camera_lut_ids(aircraft_names: Iterable[str]) -> set[str]:
    expected: set[str] = set()
    for value in aircraft_names:
        family = canonical_camera_family(value)
        for lut_id, meta in KNOWN_CAMERA_LUTS.items():
            if family and meta["camera_family"] == family:
                expected.add(lut_id)
    return expected


def parse_registry(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None or not path.is_file():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return {
        row.get("lut_identity", "").strip(): row
        for row in rows
        if row.get("lut_identity", "").strip()
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def decode_lut_param(raw: str | None) -> dict[str, str]:
    """Decode Final Cut's outer OXML wrapper enough to compare default/data value."""
    if not raw:
        return {"default_value": "", "data_value": "", "decode_status": "missing"}
    try:
        decoded = base64.b64decode("".join(raw.split()), validate=True)
        root = ET.fromstring(decoded)
    except Exception:
        return {
            "default_value": "",
            "data_value": "",
            "decode_status": "opaque",
        }

    parameter = next(
        (
            elem
            for elem in root.iter()
            if local_name(elem.tag) == "parameter"
            and elem.get("name") == "LUT"
        ),
        None,
    )
    if parameter is None:
        return {
            "default_value": "",
            "data_value": "",
            "decode_status": "no_parameter",
        }

    default_value = ""
    data_value = ""
    for child in parameter:
        tag = local_name(child.tag)
        if tag == "defaultVal":
            default_value = (child.text or "").strip()
        elif tag == "dataValue":
            data_value = (child.text or "").strip()

    return {
        "default_value": default_value,
        "data_value": data_value,
        "decode_status": "decoded",
    }


def custom_lut_filters(
    clip: ET.Element,
    resource_index: dict[str, ET.Element],
    registry: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    stack: list[dict[str, Any]] = []
    for filt in clip.iter():
        if local_name(filt.tag) != "filter-video":
            continue
        effect = resource_index.get(filt.get("ref") or "")
        if (
            effect is None
            or local_name(effect.tag) != "effect"
            or effect.get("uid") != CUSTOM_LUT_UID
        ):
            continue

        enabled_raw = (filt.get("enabled") or "1").strip().casefold()
        enabled = enabled_raw not in {"0", "false", "no"}

        lut_raw = ""
        mix = "<default>"
        for elem in filt.iter():
            if local_name(elem.tag) != "param":
                continue
            name = elem.get("name") or ""
            key = elem.get("key") or ""
            value = elem.get("value") or ""
            if name == "LUT" and key == "3":
                lut_raw = value
            elif name == "Mix":
                mix = value or "<default>"

        decoded = decode_lut_param(lut_raw)
        selected = bool(
            enabled
            and lut_raw
            and decoded["decode_status"] == "decoded"
            and decoded["data_value"]
            and decoded["data_value"] != decoded["default_value"]
        )
        if decoded["decode_status"] == "decoded" and not selected:
            identity = ""
        else:
            identity = (
                "CLUT_"
                + hashlib.sha256(lut_raw.encode("utf-8")).hexdigest()[:16].upper()
                if lut_raw
                else ""
            )

        reg = registry.get(identity, {})
        stack.append(
            {
                "stack_index": len(stack) + 1,
                "enabled": enabled,
                "selected": selected,
                "lut_identity": identity,
                "lut_name": reg.get("lut_name", ""),
                "package": reg.get("package", ""),
                "production_approved": reg.get("production_approved", ""),
                "mix": mix,
                "decode_status": decoded["decode_status"],
            }
        )
    return stack


def load_source_evidence(report_root: Path | None) -> dict[str, SourceEvidence]:
    if report_root is None or not report_root.is_dir():
        return {}

    colors: dict[str, set[str]] = defaultdict(set)
    aircraft: dict[str, set[str]] = defaultdict(set)
    srts: dict[str, set[str]] = defaultdict(set)

    for path in sorted(report_root.rglob("*.json"), key=lambda p: str(p).casefold()):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        sources = payload.get("sources")
        if not isinstance(sources, list):
            continue
        for source in sources:
            if not isinstance(source, dict):
                continue
            key = norm_source(source.get("source_name"))
            if not key:
                continue
            color = str(source.get("srt_color_md") or "").strip()
            plane = str(source.get("aircraft_name") or "").strip()
            srt = str(source.get("srt_path") or "").strip()
            if color:
                colors[key].add(color)
            if plane:
                aircraft[key].add(plane)
            if srt:
                srts[key].add(srt)

    keys = set(colors) | set(aircraft) | set(srts)
    return {
        key: SourceEvidence(
            source_name=key,
            color_modes=tuple(sorted(colors.get(key, set()))),
            aircraft_names=tuple(sorted(aircraft.get(key, set()))),
            srt_paths=tuple(sorted(srts.get(key, set()))),
        )
        for key in keys
    }


def camera_status(
    *,
    color_modes: tuple[str, ...],
    aircraft_names: tuple[str, ...],
    camera_lut_raw: str,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    known_colors = {value for value in color_modes if value}
    dlog_values = {value for value in known_colors if is_dlog_m(value)}
    non_dlog_values = known_colors - dlog_values

    lut_id, lut_name = camera_lut_parts(camera_lut_raw)
    lut_meta = KNOWN_CAMERA_LUTS.get(lut_id)
    expected = expected_camera_lut_ids(aircraft_names)

    if dlog_values and non_dlog_values:
        return "REVIEW_COLOR_EVIDENCE_CONFLICT", [
            "source_reports_disagree_on_color_mode"
        ]

    if dlog_values:
        if is_none_camera_lut(camera_lut_raw):
            return "FAIL_MISSING_DLOG_CAMERA_LUT", [
                "dlog_m_source_without_camera_conversion"
            ]
        if not lut_id:
            return "REVIEW_UNPARSED_CAMERA_LUT", [
                "dlog_m_source_has_unparsed_camera_lut"
            ]
        if lut_meta is None:
            return "REVIEW_UNKNOWN_CAMERA_LUT", [
                "dlog_m_source_uses_unregistered_camera_lut"
            ]
        if expected and lut_id not in expected:
            return "FAIL_WRONG_CAMERA_LUT_FOR_AIRCRAFT", [
                "recognized_dlog_conversion_but_camera_family_mismatch"
            ]
        reasons.append("dlog_m_source")
        reasons.append("recognized_dlog_to_rec709_camera_lut")
        if expected:
            reasons.append("camera_lut_matches_aircraft_family")
        return "PASS", reasons

    if non_dlog_values:
        if is_none_camera_lut(camera_lut_raw):
            return "NOT_REQUIRED", ["source_not_reported_as_dlog_m"]
        if lut_meta is not None:
            return "REVIEW_DLOG_LUT_ON_NON_DLOG_SOURCE", [
                "recognized_dlog_conversion_on_non_dlog_source"
            ]
        return "REVIEW_CAMERA_LUT_ON_NON_DLOG_SOURCE", [
            "camera_lut_present_on_non_dlog_source"
        ]

    if lut_meta is not None:
        return "PASS_INFERRED_FROM_CAMERA_LUT", [
            "source_color_unknown",
            f"recognized_conversion:{lut_name or lut_meta['name']}",
        ]

    if is_none_camera_lut(camera_lut_raw):
        return "REVIEW_SOURCE_COLOR_UNKNOWN", [
            "source_color_unknown",
            "no_camera_lut",
        ]

    return "REVIEW_SOURCE_COLOR_AND_CAMERA_LUT", [
        "source_color_unknown",
        "camera_lut_unrecognized",
    ]


def creative_status(
    stack: list[dict[str, Any]],
    registry_supplied: bool,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    enabled = [row for row in stack if row["enabled"]]
    selected = [row for row in enabled if row["selected"]]

    if not stack:
        return "FAIL_NO_CUSTOM_LUT_EFFECT", ["no_custom_lut_filter"]
    if not enabled:
        return "FAIL_CUSTOM_LUT_DISABLED", ["custom_lut_filters_all_disabled"]
    if not selected:
        opaque = [
            row
            for row in enabled
            if row["decode_status"] not in {"decoded", "missing"}
        ]
        if opaque:
            return "REVIEW_CUSTOM_LUT_OPAQUE", [
                "custom_lut_filter_present_but_selection_could_not_be_decoded"
            ]
        return "FAIL_NO_CUSTOM_LUT_SELECTED", [
            "custom_lut_effect_present_but_default_lut_selected"
        ]

    reasons.append(f"selected_custom_luts:{len(selected)}")
    if len(selected) > 1:
        reasons.append("stacked_custom_luts")

    if registry_supplied:
        unresolved = [row for row in selected if not row["lut_name"]]
        unapproved = [
            row
            for row in selected
            if row["production_approved"].strip().casefold()
            not in {"1", "true", "yes", "y", "approved"}
        ]
        if unresolved:
            return "REVIEW_UNNAMED_CUSTOM_LUT", reasons + [
                "selected_lut_missing_from_registry"
            ]
        if unapproved:
            return "REVIEW_OUTSIDE_APPROVED_PALETTE", reasons + [
                "selected_lut_not_production_approved"
            ]
        return (
            "PASS_STACKED_APPROVED" if len(selected) > 1 else "PASS_APPROVED",
            reasons,
        )

    return "PASS_UNNAMED", reasons + ["lut_registry_not_supplied"]


def final_grade_status(camera: str, creative: str) -> str:
    camera_ok = camera in {"PASS", "PASS_INFERRED_FROM_CAMERA_LUT", "NOT_REQUIRED"}
    creative_ok = creative.startswith("PASS")
    if camera_ok and creative_ok:
        return (
            "PRODUCTION_GRADE_STACKED"
            if "STACKED" in creative
            else "PRODUCTION_GRADE"
        )
    if camera.startswith("FAIL"):
        return camera
    if creative.startswith("FAIL"):
        if camera in {"PASS", "PASS_INFERRED_FROM_CAMERA_LUT"}:
            return "TECHNICALLY_CONVERTED_NOT_CREATIVE_GRADED"
        return creative
    return "REVIEW"


def parse_ready_appearances(
    corpus_root: Path,
    source_evidence: dict[str, SourceEvidence],
    registry: dict[str, dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    appearances: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    xml_files = sorted(
        (
            path
            for path in corpus_root.rglob("*")
            if path.is_file() and path.suffix.casefold() in XML_EXTENSIONS
        ),
        key=lambda path: str(path).casefold(),
    )

    for file_index, path in enumerate(xml_files, 1):
        try:
            root = ET.parse(path).getroot()
            resources = first_direct_child(root, "resources")
            if resources is None:
                continue
            index = build_resource_index(resources)
        except Exception as exc:
            errors.append({"file": str(path), "error": f"{type(exc).__name__}: {exc}"})
            continue

        for event in root.iter():
            if local_name(event.tag) != "event":
                continue
            event_name = event.get("name") or ""
            for project in list(event):
                if local_name(project.tag) != "project":
                    continue
                project_name = project.get("name") or ""
                sequence = first_direct_child(project, "sequence")
                if sequence is None:
                    continue
                for clip in sequence.iter():
                    if local_name(clip.tag) != "asset-clip":
                        continue
                    metadata = read_vclip_metadata(clip)
                    if metadata.get("com.vclip.telemetry.variant") != "ready_cut":
                        continue
                    stock_id = metadata.get("com.vclip.stock_clip_id") or ""
                    if not stock_id:
                        continue
                    ref = clip.get("ref") or ""
                    asset = index.get(ref)
                    if asset is None or local_name(asset.tag) != "asset":
                        continue

                    source_name = clip.get("name") or asset.get("name") or ""
                    evidence = source_evidence.get(norm_source(source_name))
                    colors = evidence.color_modes if evidence else ()
                    aircraft = evidence.aircraft_names if evidence else ()
                    srts = evidence.srt_paths if evidence else ()

                    camera_raw = asset.get("customLUTOverride") or ""
                    camera_lut_id, camera_lut_name = camera_lut_parts(camera_raw)
                    cam_status, cam_reasons = camera_status(
                        color_modes=colors,
                        aircraft_names=aircraft,
                        camera_lut_raw=camera_raw,
                    )

                    stack = custom_lut_filters(clip, index, registry)
                    creative, creative_reasons = creative_status(stack, bool(registry))
                    final = final_grade_status(cam_status, creative)

                    selected_stack = [
                        row for row in stack if row["enabled"] and row["selected"]
                    ]
                    stack_text = " + ".join(
                        (row["lut_name"] or row["lut_identity"] or "<unresolved>")
                        + f"@{row['mix']}"
                        for row in selected_stack
                    )

                    appearances.append(
                        {
                            "stock_clip_id": stock_id,
                            "xml_file": str(path),
                            "event_name": event_name,
                            "project_name": project_name,
                            "source_name": source_name,
                            "source_color_modes": " | ".join(colors),
                            "aircraft_names": " | ".join(aircraft),
                            "srt_paths": " | ".join(srts),
                            "camera_lut_raw": camera_raw,
                            "camera_lut_id": camera_lut_id,
                            "camera_lut_name": camera_lut_name,
                            "camera_lut_status": cam_status,
                            "camera_lut_reasons": " | ".join(cam_reasons),
                            "custom_lut_filter_count": len(stack),
                            "selected_custom_lut_count": len(selected_stack),
                            "custom_lut_stack": stack_text,
                            "custom_lut_stack_json": json.dumps(stack, ensure_ascii=False),
                            "creative_grade_status": creative,
                            "creative_grade_reasons": " | ".join(creative_reasons),
                            "grade_status": final,
                        }
                    )

        if file_index % 50 == 0:
            print(f"scanned {file_index}/{len(xml_files)} XML files")

    return appearances, errors


def dedupe_clips(appearances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in appearances:
        grouped[row["stock_clip_id"]].append(row)

    rows: list[dict[str, Any]] = []
    for stock_id, members in sorted(grouped.items()):
        signatures = {
            (
                row["source_color_modes"],
                row["aircraft_names"],
                row["camera_lut_raw"],
                row["custom_lut_stack"],
                row["grade_status"],
            )
            for row in members
        }
        representative = sorted(
            members,
            key=lambda row: (row["project_name"], row["xml_file"]),
        )[0]
        grade_counts = Counter(row["grade_status"] for row in members)
        camera_counts = Counter(row["camera_lut_status"] for row in members)
        creative_counts = Counter(row["creative_grade_status"] for row in members)

        rows.append(
            {
                "stock_clip_id": stock_id,
                "appearances": len(members),
                "grade_signature_count": len(signatures),
                "deterministic_consensus": "YES" if len(signatures) == 1 else "NO",
                "grade_status": (
                    representative["grade_status"]
                    if len(signatures) == 1
                    else "CONFLICTING_GRADE_STATE"
                ),
                "camera_lut_status": (
                    representative["camera_lut_status"]
                    if len(signatures) == 1
                    else "CONFLICT"
                ),
                "creative_grade_status": (
                    representative["creative_grade_status"]
                    if len(signatures) == 1
                    else "CONFLICT"
                ),
                "source_color_modes": representative["source_color_modes"],
                "aircraft_names": representative["aircraft_names"],
                "camera_lut_id": representative["camera_lut_id"],
                "camera_lut_name": representative["camera_lut_name"],
                "custom_lut_stack": representative["custom_lut_stack"],
                "selected_custom_lut_count": representative["selected_custom_lut_count"],
                "sample_project": representative["project_name"],
                "sample_xml": representative["xml_file"],
                "grade_status_counts": " | ".join(
                    f"{key}:{value}" for key, value in grade_counts.most_common()
                ),
                "camera_status_counts": " | ".join(
                    f"{key}:{value}" for key, value in camera_counts.most_common()
                ),
                "creative_status_counts": " | ".join(
                    f"{key}:{value}" for key, value in creative_counts.most_common()
                ),
            }
        )
    return rows


def lut_usage(clip_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counter: Counter[tuple[str, str]] = Counter()
    samples: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in clip_rows:
        if row["deterministic_consensus"] != "YES":
            continue
        for token in [
            part.strip() for part in row["custom_lut_stack"].split("+") if part.strip()
        ]:
            if "@" in token:
                identity, mix = token.rsplit("@", 1)
            else:
                identity, mix = token, "<unknown>"
            key = (identity.strip(), mix.strip())
            counter[key] += 1
            if len(samples[key]) < 5:
                samples[key].append(row["sample_project"])

    return [
        {
            "uses": count,
            "lut": key[0],
            "mix": key[1],
            "sample_projects": " | ".join(samples[key]),
        }
        for key, count in counter.most_common()
    ]


def stack_usage(clip_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter(
        row["custom_lut_stack"]
        for row in clip_rows
        if row["deterministic_consensus"] == "YES" and row["custom_lut_stack"]
    )
    return [
        {"uses": count, "custom_lut_stack": stack}
        for stack, count in counter.most_common()
    ]


def registry_template(
    appearances: list[dict[str, Any]],
    registry: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    mixes: dict[str, Counter[str]] = defaultdict(Counter)
    samples: dict[str, list[str]] = defaultdict(list)

    for row in appearances:
        stack = json.loads(row["custom_lut_stack_json"])
        for item in stack:
            identity = item.get("lut_identity") or ""
            if not identity or not item.get("selected"):
                continue
            counter[identity] += 1
            mixes[identity][item.get("mix") or "<default>"] += 1
            if len(samples[identity]) < 8:
                samples[identity].append(row["project_name"])

    rows: list[dict[str, Any]] = []
    for identity, count in counter.most_common():
        existing = registry.get(identity, {})
        rows.append(
            {
                "lut_identity": identity,
                "lut_name": existing.get("lut_name", ""),
                "package": existing.get("package", ""),
                "production_approved": existing.get("production_approved", ""),
                "occurrences": count,
                "mix_values": " | ".join(
                    f"{mix}:{n}" for mix, n in mixes[identity].most_common()
                ),
                "sample_projects": " | ".join(samples[identity]),
            }
        )
    return rows


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--corpus-root", type=Path, required=True)
    p.add_argument("--reconstruction-reports", type=Path)
    p.add_argument("--lut-registry", type=Path)
    p.add_argument("--output-root", type=Path, required=True)
    return p


def main() -> int:
    args = parser().parse_args()
    corpus = args.corpus_root.expanduser().resolve()
    reports = (
        args.reconstruction_reports.expanduser().resolve()
        if args.reconstruction_reports
        else None
    )
    registry_path = (
        args.lut_registry.expanduser().resolve() if args.lut_registry else None
    )
    output = args.output_root.expanduser().resolve()

    if not corpus.is_dir():
        raise SystemExit(f"Corpus root does not exist: {corpus}")
    output.mkdir(parents=True, exist_ok=True)

    registry = parse_registry(registry_path)
    evidence = load_source_evidence(reports)
    print("source evidence rows:", len(evidence))

    appearances, errors = parse_ready_appearances(corpus, evidence, registry)
    clips = dedupe_clips(appearances)
    usage = lut_usage(clips)
    stacks = stack_usage(clips)
    template = registry_template(appearances, registry)

    write_csv(output / "ready-grade-appearances.csv", appearances)
    write_csv(output / "ready-grade-clips.csv", clips)
    write_csv(output / "creative-lut-usage.csv", usage)
    write_csv(output / "creative-lut-stack-usage.csv", stacks)
    write_csv(output / "creative-lut-registry-template.csv", template)
    write_csv(output / "parse-errors.csv", errors)

    grade_counts = Counter(row["grade_status"] for row in clips)
    camera_counts = Counter(row["camera_lut_status"] for row in clips)
    creative_counts = Counter(row["creative_grade_status"] for row in clips)
    consensus = sum(row["deterministic_consensus"] == "YES" for row in clips)

    summary = {
        "ready_cut_appearances": len(appearances),
        "unique_ready_cut_ids": len(clips),
        "deterministic_consensus_ids": consensus,
        "conflicting_ids": len(clips) - consensus,
        "source_evidence_rows": len(evidence),
        "grade_status": dict(grade_counts),
        "camera_lut_status": dict(camera_counts),
        "creative_grade_status": dict(creative_counts),
        "unique_selected_lut_identities": len(template),
        "parse_errors": len(errors),
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print()
    print("VCLIP READY-CUT COLOR GRADE AUDIT")
    print("=================================")
    print("ready-cut appearances       :", len(appearances))
    print("unique ready-cut IDs        :", len(clips))
    print("deterministic consensus IDs :", consensus)
    print("conflicting IDs             :", len(clips) - consensus)
    print("source evidence rows        :", len(evidence))
    print("creative LUT identities     :", len(template))
    print("parse errors                :", len(errors))

    print()
    print("GRADE STATUS")
    print("------------")
    for key, value in grade_counts.most_common():
        print(f"{value:5d}  {key}")

    print()
    print("CAMERA LUT STATUS")
    print("-----------------")
    for key, value in camera_counts.most_common():
        print(f"{value:5d}  {key}")

    print()
    print("CREATIVE GRADE STATUS")
    print("---------------------")
    for key, value in creative_counts.most_common():
        print(f"{value:5d}  {key}")

    print()
    print("TOP CREATIVE LUTS")
    print("-----------------")
    for row in usage[:30]:
        print(f"{row['uses']:5d}  {row['lut']}  mix={row['mix']}")

    print()
    print("TOP LUT STACKS")
    print("--------------")
    for row in stacks[:20]:
        print(f"{row['uses']:5d}  {row['custom_lut_stack']}")

    print()
    print("output:", output)
    print("VCLIP READY-CUT COLOR GRADE AUDIT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
