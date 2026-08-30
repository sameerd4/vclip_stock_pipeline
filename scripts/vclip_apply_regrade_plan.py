#!/usr/bin/env python3
"""Generate Final Cut re-grade variants from a deterministic VClip regrade plan.

Stage 3 of the Vancouver Jan 18, 2025 trial.

For each planned ready_cut this script:
- clones the exact historical project
- preserves source ref/start/duration and Camera LUT resources
- removes ONLY Final Cut Custom LUT filter(s)
- inserts the calibrated VClip Production Palette v1 Custom LUT payload
- preserves all other video treatment
- writes explicit VClip regrade provenance metadata

Nothing in the historical source XML or rendered masters is modified. Output is
one importable FCPXML per source shard, plus a manifest. REVIEW rows are skipped
by default; pass --include-review to include them as clearly-labeled variants.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from vclip_pipeline.stockify.core import stable_uid
from vclip_pipeline.stockify.fcpxml import (
    add_vclip_metadata,
    build_resource_index,
    first_direct_child,
    local_name,
    read_vclip_metadata,
    validate_fcpxml,
    write_fcpxml,
)

CUSTOM_LUT_UID = "FxPlug:14B39AEF-607D-42DF-98DD-DB3DD345E925"
POLICY_VERSION = "production-palette-policy-v2"
REgrade_VERSION = "vclip-regrade-v1"

ROOT = (
    Path.home()
    / "Desktop"
    / "vclip-work"
    / "work"
    / "regrade-trial-vancouver-jan18-v1"
)
DEFAULT_PLAN = ROOT / "regrade-plan-v2.csv"
DEFAULT_REGISTRY = (
    Path.home()
    / "Desktop"
    / "vclip-work"
    / "work"
    / "lut-census-v1"
    / "production-palette-v1-registry.csv"
)
DEFAULT_OUTPUT = ROOT / "fcpxml-regrade-v1"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


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


def find_calibration(explicit: Path | None) -> Path:
    if explicit is not None:
        path = explicit.expanduser().resolve()
        if path.exists():
            return path
        raise RuntimeError(f"Calibration FCPXML not found: {path}")

    names = [
        "VClip Production Palette v1 — Calibration.fcpxmld",
        "VClip Production Palette v1 — Calibration.fcpxml",
        "VClip Production Palette v1 - Calibration.fcpxmld",
        "VClip Production Palette v1 - Calibration.fcpxml",
    ]
    roots = [
        Path.home() / "Downloads",
        Path.home() / "Desktop",
        Path.home() / "Documents",
        Path.home() / "Desktop" / "vclip-work" / "work" / "lut-census-v1",
    ]
    for root in roots:
        for name in names:
            path = root / name
            if path.exists():
                return path.resolve()
        if root.is_dir():
            for path in sorted(root.glob("*Production Palette*v1*Calibration*.fcpxml*")):
                if path.exists():
                    return path.resolve()
    raise RuntimeError(
        "Could not auto-find the exported calibration FCPXML. "
        "Pass --calibration '/path/to/VClip Production Palette v1 — Calibration.fcpxmld'."
    )


def read_fcpxml_bytes(path: Path) -> bytes:
    if path.is_dir():
        info = path / "Info.fcpxml"
        if not info.is_file():
            raise RuntimeError(f"FCPXML bundle lacks Info.fcpxml: {path}")
        return info.read_bytes()
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            matches = [
                name
                for name in zf.namelist()
                if name.endswith("/Info.fcpxml")
                and not name.startswith("__MACOSX/")
            ]
            if len(matches) != 1:
                raise RuntimeError(
                    f"Expected one Info.fcpxml inside {path}, found {len(matches)}"
                )
            return zf.read(matches[0])
    if path.is_file():
        return path.read_bytes()
    raise RuntimeError(f"Not a readable FCPXML input: {path}")


def parse_fcpxml_root(path: Path) -> ET.Element:
    try:
        return ET.fromstring(read_fcpxml_bytes(path))
    except ET.ParseError as exc:
        raise RuntimeError(f"Could not parse FCPXML {path}: {exc}") from exc


def calibration_lut_name(clip_name: str) -> str:
    match = re.match(r"^\s*\d+\s*[—-]\s*(.+?)\s*$", clip_name)
    return match.group(1).strip() if match else ""


def custom_lut_effect_id(index: dict[str, ET.Element]) -> str:
    ids = [
        rid
        for rid, resource in index.items()
        if local_name(resource.tag) == "effect"
        and resource.get("uid") == CUSTOM_LUT_UID
    ]
    if len(ids) != 1:
        raise RuntimeError(
            f"Expected exactly one Custom LUT effect resource, found {ids}"
        )
    return ids[0]


def extract_calibration_filters(
    calibration: Path,
    registry: dict[str, str],
) -> dict[str, ET.Element]:
    root = parse_fcpxml_root(calibration)
    resources = first_direct_child(root, "resources")
    if resources is None:
        raise RuntimeError("Calibration FCPXML has no resources")
    index = build_resource_index(resources)

    filters: dict[str, ET.Element] = {}
    for clip in root.iter():
        if local_name(clip.tag) != "asset-clip":
            continue
        lut_name = calibration_lut_name(clip.get("name") or "")
        if not lut_name or lut_name not in registry:
            continue
        matches = []
        for child in list(clip):
            if local_name(child.tag) != "filter-video":
                continue
            effect = index.get(child.get("ref") or "")
            if (
                effect is not None
                and local_name(effect.tag) == "effect"
                and effect.get("uid") == CUSTOM_LUT_UID
            ):
                matches.append(child)
        if len(matches) != 1:
            raise RuntimeError(
                f"Calibration clip {clip.get('name')} has {len(matches)} Custom LUT filters"
            )
        cloned = copy.deepcopy(matches[0])
        lut_param = next(
            (
                node
                for node in cloned.iter()
                if local_name(node.tag) == "param"
                and node.get("name") == "LUT"
                and node.get("key") == "3"
            ),
            None,
        )
        if lut_param is None or not lut_param.get("value"):
            raise RuntimeError(f"Calibration clip {clip.get('name')} lacks LUT payload")
        identity = (
            "CLUT_"
            + hashlib.sha256(lut_param.get("value", "").encode("utf-8"))
            .hexdigest()[:16]
            .upper()
        )
        expected = registry[lut_name]
        if identity != expected:
            raise RuntimeError(
                f"Calibration mismatch for {lut_name}: {identity} != {expected}"
            )
        filters[lut_name] = cloned

    missing = sorted(set(registry) - set(filters))
    if missing:
        raise RuntimeError("Calibration missing LUTs: " + ", ".join(missing))
    return filters


def load_registry(path: Path) -> dict[str, str]:
    rows = read_csv(path)
    registry = {
        row["lut_name"].strip(): row["lut_identity"].strip()
        for row in rows
        if row.get("lut_name") and row.get("lut_identity")
    }
    if len(registry) != 10:
        raise RuntimeError(f"Expected 10 production palette LUTs, found {len(registry)}")
    return registry


def clip_custom_lut_children(
    clip: ET.Element,
    index: dict[str, ET.Element],
) -> list[ET.Element]:
    rows: list[ET.Element] = []
    for child in list(clip):
        if local_name(child.tag) != "filter-video":
            continue
        effect = index.get(child.get("ref") or "")
        if (
            effect is not None
            and local_name(effect.tag) == "effect"
            and effect.get("uid") == CUSTOM_LUT_UID
        ):
            rows.append(child)
    descendant_count = 0
    for node in clip.iter():
        if node is clip or local_name(node.tag) != "filter-video":
            continue
        effect = index.get(node.get("ref") or "")
        if (
            effect is not None
            and local_name(effect.tag) == "effect"
            and effect.get("uid") == CUSTOM_LUT_UID
        ):
            descendant_count += 1
    if descendant_count != len(rows):
        raise RuntimeError(
            "Custom LUT filter is nested rather than a direct asset-clip child; refusing unsafe rewrite"
        )
    return rows


def non_lut_signature(
    clip: ET.Element,
    index: dict[str, ET.Element],
) -> str:
    serialized: list[str] = []
    custom = set(clip_custom_lut_children(clip, index))
    for child in list(clip):
        if child in custom or local_name(child.tag) == "metadata":
            continue
        serialized.append(ET.tostring(child, encoding="unicode"))
    return hashlib.sha256("\n".join(serialized).encode("utf-8")).hexdigest()


def locate_ready_clip(
    project: ET.Element,
    stock_clip_id: str,
) -> ET.Element:
    sequence = first_direct_child(project, "sequence")
    if sequence is None:
        raise RuntimeError("project missing sequence")
    matches = []
    for clip in sequence.iter():
        if local_name(clip.tag) != "asset-clip":
            continue
        meta = read_vclip_metadata(clip)
        if (
            meta.get("com.vclip.stock_clip_id") == stock_clip_id
            and meta.get("com.vclip.telemetry.variant") == "ready_cut"
        ):
            matches.append(clip)
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one ready_cut clip for {stock_clip_id}, found {len(matches)}"
        )
    return matches[0]


def find_source_project(
    root: ET.Element,
    project_name: str,
    stock_clip_id: str,
) -> ET.Element:
    candidates = [
        node
        for node in root.iter()
        if local_name(node.tag) == "project"
        and (node.get("name") or "") == project_name
    ]
    for project in candidates:
        try:
            locate_ready_clip(project, stock_clip_id)
            return project
        except RuntimeError:
            continue
    raise RuntimeError(
        f"Could not find source project {project_name!r} containing {stock_clip_id}"
    )


def replace_custom_lut(
    clip: ET.Element,
    index: dict[str, ET.Element],
    template_filter: ET.Element,
    target_effect_ref: str,
) -> tuple[int, str, str]:
    before = non_lut_signature(clip, index)
    old_filters = clip_custom_lut_children(clip, index)
    if not old_filters:
        raise RuntimeError("Target ready cut has no existing Custom LUT filter")

    children = list(clip)
    first_index = min(children.index(node) for node in old_filters)
    for node in old_filters:
        clip.remove(node)

    new_filter = copy.deepcopy(template_filter)
    new_filter.set("ref", target_effect_ref)
    clip.insert(first_index, new_filter)

    after = non_lut_signature(clip, index)
    if before != after:
        raise RuntimeError("Non-Custom-LUT video treatment changed unexpectedly")
    return len(old_filters), before, after


def new_project_name(row: dict[str, str]) -> str:
    status = row.get("recommendation_status") or "REVIEW"
    prefix = "REVIEW — " if status != "AUTO_READY" else ""
    return (
        f"{prefix}{row['project_name']} — Regrade v1 — {row['recommended_lut']}"
    )


def build_output_for_source(
    source_xml: Path,
    rows: list[dict[str, str]],
    calibration_filters: dict[str, ET.Element],
    output_dir: Path,
) -> tuple[Path, list[dict[str, Any]]]:
    source_root = parse_fcpxml_root(source_xml)
    source_resources = first_direct_child(source_root, "resources")
    if source_resources is None:
        raise RuntimeError(f"Source XML has no resources: {source_xml}")
    source_index = build_resource_index(source_resources)
    target_effect_ref = custom_lut_effect_id(source_index)

    out_root = ET.Element(
        "fcpxml",
        {"version": source_root.get("version") or "1.12"},
    )
    out_root.append(copy.deepcopy(source_resources))
    library = ET.SubElement(out_root, "library")
    event_name = f"VClip Regrade Trial — Vancouver Jan 18 — {source_xml.stem[:42]}"
    event = ET.SubElement(
        library,
        "event",
        {
            "name": event_name,
            "uid": stable_uid("vclip-regrade-event", str(source_xml), POLICY_VERSION),
        },
    )

    manifest: list[dict[str, Any]] = []
    for row in rows:
        source_project = find_source_project(
            source_root,
            row["project_name"],
            row["stock_clip_id"],
        )
        project = copy.deepcopy(source_project)
        old_project_name = project.get("name") or row["project_name"]
        project.set("name", new_project_name(row))
        project.set(
            "uid",
            stable_uid(
                "vclip-regrade-project",
                row["stock_clip_id"],
                row["recommended_lut"],
                POLICY_VERSION,
            ),
        )
        clip = locate_ready_clip(project, row["stock_clip_id"])

        timing_before = (
            clip.get("ref") or "",
            clip.get("start") or "",
            clip.get("duration") or "",
            clip.get("offset") or "",
        )
        old_count, _before, _after = replace_custom_lut(
            clip,
            source_index,
            calibration_filters[row["recommended_lut"]],
            target_effect_ref,
        )
        timing_after = (
            clip.get("ref") or "",
            clip.get("start") or "",
            clip.get("duration") or "",
            clip.get("offset") or "",
        )
        if timing_before != timing_after:
            raise RuntimeError(
                f"Source timing changed for {row['stock_clip_id']}: {timing_before} -> {timing_after}"
            )

        add_vclip_metadata(
            clip,
            {
                "com.vclip.regrade.version": REgrade_VERSION,
                "com.vclip.regrade.policy_version": row.get("palette_policy_version")
                or POLICY_VERSION,
                "com.vclip.regrade.parent_stock_clip_id": row["stock_clip_id"],
                "com.vclip.regrade.original_project_name": old_project_name,
                "com.vclip.regrade.original_custom_lut_stack": row.get(
                    "current_custom_lut_stack"
                )
                or "",
                "com.vclip.regrade.selected_lut": row["recommended_lut"],
                "com.vclip.regrade.confidence": row.get(
                    "recommendation_confidence"
                )
                or "",
                "com.vclip.regrade.rule": row.get("recommendation_rule") or "",
                "com.vclip.regrade.status": row.get("recommendation_status") or "",
                "com.vclip.regrade.scene_caption": row.get("scene_caption") or "",
                "com.vclip.regrade.lighting": row.get("lighting") or "",
                "com.vclip.regrade.weather": row.get("weather") or "",
            },
        )
        event.append(project)
        manifest.append(
            {
                "stock_clip_id": row["stock_clip_id"],
                "source_xml": str(source_xml),
                "source_project": old_project_name,
                "generated_project": project.get("name") or "",
                "old_custom_lut_stack": row.get("current_custom_lut_stack") or "",
                "new_lut": row["recommended_lut"],
                "confidence": row.get("recommendation_confidence") or "",
                "rule": row.get("recommendation_rule") or "",
                "status": row.get("recommendation_status") or "",
                "old_custom_lut_filter_count": old_count,
                "timing_preserved": "YES",
                "non_lut_treatment_preserved": "YES",
            }
        )

    validation = validate_fcpxml(out_root)
    if not validation.passed:
        raise RuntimeError(
            "Generated FCPXML failed validation: " + " | ".join(validation.errors)
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(source_xml).encode("utf-8")).hexdigest()[:8]
    output_path = output_dir / f"{source_xml.stem}--regrade-v1-{digest}.fcpxml"
    write_fcpxml(out_root, output_path)
    return output_path, manifest


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    p.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    p.add_argument("--calibration", type=Path)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--include-review", action="store_true")
    return p


def main() -> int:
    args = parser().parse_args()
    plan_path = args.plan.expanduser().resolve()
    registry_path = args.registry.expanduser().resolve()
    output = args.output_root.expanduser().resolve()

    if not plan_path.is_file():
        raise SystemExit(f"Regrade plan not found: {plan_path}")
    if not registry_path.is_file():
        raise SystemExit(f"Palette registry not found: {registry_path}")

    try:
        calibration = find_calibration(args.calibration)
        registry = load_registry(registry_path)
        calibration_filters = extract_calibration_filters(calibration, registry)
    except RuntimeError as exc:
        raise SystemExit(f"CALIBRATION ERROR: {exc}") from exc

    plan = read_csv(plan_path)
    selected = [
        row
        for row in plan
        if row.get("recommended_lut") in registry
        and (
            args.include_review
            or row.get("recommendation_status") == "AUTO_READY"
        )
    ]
    skipped_review = [
        row
        for row in plan
        if row.get("recommendation_status") != "AUTO_READY"
        and not args.include_review
    ]

    groups: dict[Path, list[dict[str, str]]] = defaultdict(list)
    for row in selected:
        groups[Path(row["xml_file"])].append(row)

    print("VCLIP APPLY RE-GRADE PLAN PREFLIGHT")
    print("==================================")
    print("plan             :", plan_path)
    print("calibration      :", calibration)
    print("selected variants:", len(selected))
    print("review skipped   :", len(skipped_review))
    print("source shards    :", len(groups))
    print("output           :", output)
    print()

    output.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    outputs: list[Path] = []
    failures: list[dict[str, str]] = []

    for index, (source_xml, rows) in enumerate(sorted(groups.items()), start=1):
        print(f"{index}/{len(groups)}  {source_xml.name}  projects={len(rows)}")
        try:
            path, rows_manifest = build_output_for_source(
                source_xml,
                rows,
                calibration_filters,
                output,
            )
            outputs.append(path)
            manifest.extend(rows_manifest)
            print("  ->", path)
        except Exception as exc:
            failure = {
                "source_xml": str(source_xml),
                "project_count": str(len(rows)),
                "error": f"{type(exc).__name__}: {exc}",
            }
            failures.append(failure)
            print("  FAILED", failure["error"])

    write_csv(output / "regrade-manifest.csv", manifest)
    write_csv(output / "regrade-generation-failures.csv", failures)
    write_csv(output / "review-skipped.csv", skipped_review)

    lut_counts = Counter(row["new_lut"] for row in manifest)
    summary = {
        "regrade_version": REgrade_VERSION,
        "policy_version": POLICY_VERSION,
        "calibration": str(calibration),
        "plan": str(plan_path),
        "selected_variants": len(selected),
        "generated_variants": len(manifest),
        "review_skipped": len(skipped_review),
        "source_shards": len(groups),
        "output_fcpxml_files": [str(path) for path in outputs],
        "failures": failures,
        "lut_counts": dict(lut_counts),
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print()
    print("VCLIP RE-GRADE FCPXML GENERATION")
    print("================================")
    print("generated variants :", len(manifest))
    print("FCPXML files       :", len(outputs))
    print("review skipped     :", len(skipped_review))
    print("failed shards      :", len(failures))
    print()
    print("LUT COUNTS")
    print("----------")
    for lut, count in lut_counts.most_common():
        print(f"{count:5d}  {lut}")
    print()
    print("IMPORT FILES")
    print("------------")
    for path in outputs:
        print(path)
    if skipped_review:
        print()
        print("REVIEW ROWS NOT GENERATED")
        print("-------------------------")
        for row in skipped_review:
            print(
                row["stock_clip_id"],
                row["project_name"],
                "->",
                row["recommended_lut"],
                row["recommendation_confidence"],
            )
    print()
    print("output:", output)
    print(
        "VCLIP RE-GRADE FCPXML GENERATION:",
        "PASS" if not failures and len(manifest) == len(selected) else "FAILED",
    )
    return 0 if not failures and len(manifest) == len(selected) else 1


if __name__ == "__main__":
    raise SystemExit(main())
