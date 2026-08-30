#!/usr/bin/env python3
"""Generate VClip regrade variants without requiring the calibration export locally.

This is a thin production wrapper around vclip_apply_regrade_plan.py.

The first generator required the exported 10-LUT calibration FCPXML to still be
present on the local Mac. That is unnecessary because the reconstructed VClip
corpus already contains many default-strength applications of every calibrated
Production Palette v1 LUT.

V2 therefore uses the authoritative production-palette registry to identify the
10 opaque CLUT identities, scans the reconstruction corpus for one default-Mix
Final Cut Custom LUT filter carrying each identity, validates all 10, and then
feeds those exact serialized Final Cut payloads into the existing safe rewrite
engine.

No historical XML or media is modified.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import vclip_apply_regrade_plan as base


DEFAULT_PAYLOAD_CORPUS = (
    Path.home()
    / "Desktop"
    / "vclip-work"
    / "work"
    / "reconstructed-vt-v1"
    / "combined-main-plus-jan18-final-v1"
)


def lut_param(filter_video: ET.Element) -> ET.Element | None:
    return next(
        (
            node
            for node in filter_video.iter()
            if base.local_name(node.tag) == "param"
            and node.get("name") == "LUT"
            and node.get("key") == "3"
            and node.get("value")
        ),
        None,
    )


def lut_identity(filter_video: ET.Element) -> str | None:
    param = lut_param(filter_video)
    if param is None:
        return None
    value = param.get("value") or ""
    if not value:
        return None
    return (
        "CLUT_"
        + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16].upper()
    )


def explicit_mix(filter_video: ET.Element) -> str | None:
    for node in filter_video.iter():
        if base.local_name(node.tag) != "param":
            continue
        if (node.get("name") or "").casefold() == "mix":
            return node.get("value") or ""
    return None


def corpus_fcpxml_files(root: Path) -> list[Path]:
    if not root.is_dir():
        raise RuntimeError(f"Payload corpus not found: {root}")
    return sorted(
        {
            path.resolve()
            for path in root.rglob("*.fcpxml")
            if path.is_file()
        },
        key=lambda path: str(path).casefold(),
    )


def discover_filters_from_corpus(
    corpus_root: Path,
    registry: dict[str, str],
) -> tuple[dict[str, ET.Element], dict[str, str]]:
    identity_to_name = {identity: name for name, identity in registry.items()}
    if len(identity_to_name) != len(registry):
        raise RuntimeError("Production LUT registry contains duplicate identities")

    found: dict[str, ET.Element] = {}
    evidence: dict[str, str] = {}
    files = corpus_fcpxml_files(corpus_root)
    parse_errors = 0

    for xml_path in files:
        if len(found) == len(registry):
            break
        try:
            root = ET.parse(xml_path).getroot()
        except ET.ParseError:
            parse_errors += 1
            continue

        resources = base.first_direct_child(root, "resources")
        if resources is None:
            continue
        index = base.build_resource_index(resources)

        for node in root.iter():
            if base.local_name(node.tag) != "filter-video":
                continue
            effect = index.get(node.get("ref") or "")
            if (
                effect is None
                or base.local_name(effect.tag) != "effect"
                or effect.get("uid") != base.CUSTOM_LUT_UID
            ):
                continue

            identity = lut_identity(node)
            if identity is None or identity not in identity_to_name:
                continue
            name = identity_to_name[identity]
            if name in found:
                continue

            mix = explicit_mix(node)
            if mix is not None:
                continue

            clone = copy.deepcopy(node)
            found[name] = clone
            evidence[name] = str(xml_path)

            if len(found) == len(registry):
                break

    missing = sorted(set(registry) - set(found))
    if missing:
        raise RuntimeError(
            "Could not discover default-Mix payloads for: "
            + ", ".join(missing)
            + f". Scanned {len(files)} FCPXML files with {parse_errors} parse errors."
        )

    for name, filter_video in found.items():
        actual = lut_identity(filter_video)
        expected = registry[name]
        if actual != expected:
            raise RuntimeError(
                f"Discovered payload mismatch for {name}: {actual} != {expected}"
            )
        if explicit_mix(filter_video) is not None:
            raise RuntimeError(
                f"Discovered payload for {name} unexpectedly contains an explicit Mix"
            )

    return found, evidence


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--plan", type=Path, default=base.DEFAULT_PLAN)
    p.add_argument("--registry", type=Path, default=base.DEFAULT_REGISTRY)
    p.add_argument("--payload-corpus", type=Path, default=DEFAULT_PAYLOAD_CORPUS)
    p.add_argument("--calibration", type=Path)
    p.add_argument("--output-root", type=Path, default=base.DEFAULT_OUTPUT)
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
        registry = base.load_registry(registry_path)
        if args.calibration is not None:
            calibration = base.find_calibration(args.calibration)
            calibration_filters = base.extract_calibration_filters(
                calibration,
                registry,
            )
            payload_source = f"calibration:{calibration}"
            payload_evidence = {name: str(calibration) for name in registry}
        else:
            payload_corpus = args.payload_corpus.expanduser().resolve()
            calibration_filters, payload_evidence = discover_filters_from_corpus(
                payload_corpus,
                registry,
            )
            payload_source = f"reconstruction-corpus:{payload_corpus}"
    except RuntimeError as exc:
        raise SystemExit(f"LUT PAYLOAD ERROR: {exc}") from exc

    print("VCLIP PRODUCTION LUT PAYLOAD LIBRARY")
    print("====================================")
    print("source:", payload_source)
    print()
    for name in sorted(registry):
        print(f"{registry[name]}  ->  {name}")
        print("  evidence:", payload_evidence[name])
    print()

    plan = base.read_csv(plan_path)
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
    print("payload source   :", payload_source)
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
            path, rows_manifest = base.build_output_for_source(
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

    base.write_csv(output / "regrade-manifest.csv", manifest)
    base.write_csv(output / "regrade-generation-failures.csv", failures)
    base.write_csv(output / "review-skipped.csv", skipped_review)

    payload_rows = [
        {
            "lut_name": name,
            "lut_identity": registry[name],
            "payload_source_fcpxml": payload_evidence[name],
            "mix": "<default>",
        }
        for name in sorted(registry)
    ]
    base.write_csv(output / "production-lut-payload-evidence.csv", payload_rows)

    lut_counts = Counter(row["new_lut"] for row in manifest)
    summary = {
        "regrade_version": base.REgrade_VERSION,
        "policy_version": base.POLICY_VERSION,
        "payload_source": payload_source,
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
