#!/usr/bin/env python3
"""Create a resolvable-only staging corpus from physical review shards.

This is the operational bridge between strict archival media closure and useful
work today. It NEVER mutates the physical review roots.

For each VClip project:
- resolved source      -> keep it and point the staging asset at chosen_path
- missing source       -> defer it
- ambiguous source     -> defer it
- known non-drone      -> exclude it
- no closure evidence  -> defer it fail-closed

A shard containing 40 resolvable clips + 2 missing clips becomes a staging shard
with 40 clips. This lets reconstruction proceed candidate-by-candidate instead
of blocking an entire shard or corpus.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

try:
    from vclip_pipeline.stockify.core import local_name
    from vclip_pipeline.stockify.fcpxml import (
        build_resource_index,
        first_direct_child,
        read_vclip_metadata,
    )
    from vclip_pipeline.workflow.camera_scope import (
        SCOPE_OUT_OF_SCOPE_NON_DRONE,
        classify_vclip_camera_scope,
    )
except Exception as exc:
    raise SystemExit(
        "Could not import VClip. Run with PYTHONPATH=<repo>/src. "
        f"Import error: {exc}"
    )

VCLIP_RE = re.compile(r"VCLIP_[0-9A-F]{12,64}")


def file_url_to_path(value: str | None) -> Path | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme and parsed.scheme != "file":
        return None
    raw = parsed.path if parsed.scheme == "file" else value
    return Path(unquote(raw)) if raw else None


def discover(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() == ".fcpxml" else []
    return sorted(root.rglob("*.fcpxml")) if root.is_dir() else []


def project_identity(
    project: ET.Element,
    index: dict[str, ET.Element],
    event_name: str,
) -> dict[str, Any] | None:
    clip = next(
        (node for node in project.iter() if local_name(node.tag) == "asset-clip"),
        None,
    )
    if clip is None:
        return None

    metadata = read_vclip_metadata(clip)
    stock_id = metadata.get("com.vclip.stock_clip_id")
    if not stock_id:
        ids = VCLIP_RE.findall(ET.tostring(project, encoding="unicode"))
        stock_id = ids[0] if ids else None
    if not stock_id:
        return None

    ref = clip.get("ref") or ""
    asset = index.get(ref)
    source_name = (
        clip.get("name")
        or (asset.get("name") if asset is not None else None)
        or ""
    )
    stem = Path(source_name).stem.casefold()

    media_path: str | None = None
    if asset is not None:
        for child in list(asset):
            if local_name(child.tag) != "media-rep":
                continue
            path = file_url_to_path(child.get("src"))
            if path is not None:
                media_path = str(path)
                break

    scope = classify_vclip_camera_scope(
        source_basename=source_name,
        media_path=media_path,
        source_event_name=event_name,
        source_project_name=project.get("name") or "",
    )
    return {
        "stock_clip_id": stock_id,
        "source_name": source_name,
        "source_stem": stem,
        "source_ref": ref,
        "camera_scope": str(scope.get("camera_scope") or "unknown"),
        "camera_family": str(scope.get("camera_family") or "unknown"),
    }


def patch_asset_path(asset: ET.Element, chosen: Path) -> None:
    reps = [
        child
        for child in list(asset)
        if local_name(child.tag) == "media-rep"
    ]
    if not reps:
        raise RuntimeError(
            f"Asset {asset.get('id') or asset.get('name')} has no media-rep"
        )

    target = next(
        (rep for rep in reps if rep.get("kind") == "original-media"),
        reps[0],
    )
    target.set("src", chosen.resolve().as_uri())


def remove_empty_events(root: ET.Element) -> None:
    for library in root.iter():
        if local_name(library.tag) != "library":
            continue
        for event in list(library):
            if local_name(event.tag) != "event":
                continue
            if not any(local_name(child.tag) == "project" for child in list(event)):
                library.remove(event)


def run(args: argparse.Namespace) -> int:
    closure = json.loads(args.closure_report.read_text(encoding="utf-8"))
    source_rows = {
        str(row["source_stem"]).casefold(): row
        for row in closure.get("sources", [])
    }

    if closure.get("parse_errors"):
        raise SystemExit(
            "Closure report contains XML parse errors. Refusing to stage."
        )

    if args.output_root.exists() and args.clean:
        shutil.rmtree(args.output_root)
    args.output_root.mkdir(parents=True, exist_ok=True)

    kept: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    shard_rows: list[dict[str, Any]] = []
    parse_errors: list[dict[str, str]] = []

    roots = [path.expanduser().resolve() for path in args.input_root]
    total_xml = sum(len(discover(root)) for root in roots)
    seen_xml = 0

    print("Staging currently available VClip corpus")
    print("=======================================")
    print(f"Physical review XMLs: {total_xml:,}")

    for root in roots:
        label = root.name
        for xml_path in discover(root):
            seen_xml += 1
            relative = (
                xml_path.relative_to(root)
                if root.is_dir()
                else Path(xml_path.name)
            )
            destination = args.output_root / label / relative
            try:
                tree = ET.parse(xml_path)
                xml_root = tree.getroot()
                resources = first_direct_child(xml_root, "resources")
                if resources is None:
                    raise RuntimeError("missing <resources>")
                index = build_resource_index(resources)

                shard_kept = 0
                shard_deferred = 0
                shard_excluded = 0
                patched_assets: dict[str, str] = {}

                for event in list(xml_root.iter()):
                    if local_name(event.tag) != "event":
                        continue
                    event_name = event.get("name") or ""
                    for project in list(event):
                        if local_name(project.tag) != "project":
                            continue
                        ident = project_identity(project, index, event_name)
                        if ident is None:
                            # Review shards occasionally contain helpers. They
                            # are not part of the physical VClip product corpus.
                            event.remove(project)
                            continue

                        base = {
                            "stock_clip_id": ident["stock_clip_id"],
                            "project_name": project.get("name") or "",
                            "event_name": event_name,
                            "source_name": ident["source_name"],
                            "source_stem": ident["source_stem"],
                            "input_xml": str(xml_path),
                            "input_relative": f"{label}/{relative.as_posix()}",
                        }

                        if ident["camera_scope"] == SCOPE_OUT_OF_SCOPE_NON_DRONE:
                            event.remove(project)
                            excluded.append(
                                {
                                    **base,
                                    "reason": "known_non_drone",
                                    "camera_family": ident["camera_family"],
                                }
                            )
                            shard_excluded += 1
                            continue

                        closure_row = source_rows.get(ident["source_stem"])
                        status = (
                            str(closure_row.get("status"))
                            if closure_row
                            else "not_in_closure_report"
                        )

                        if status != "resolved":
                            event.remove(project)
                            deferred.append(
                                {
                                    **base,
                                    "reason": status,
                                    "candidate_media_paths": (
                                        closure_row.get("candidate_media_paths", [])
                                        if closure_row
                                        else []
                                    ),
                                }
                            )
                            shard_deferred += 1
                            continue

                        chosen_raw = closure_row.get("chosen_path")
                        chosen = Path(str(chosen_raw)).expanduser() if chosen_raw else None
                        if chosen is None or not chosen.is_file():
                            event.remove(project)
                            deferred.append(
                                {
                                    **base,
                                    "reason": "resolved_path_not_currently_mounted",
                                    "chosen_path": chosen_raw,
                                }
                            )
                            shard_deferred += 1
                            continue

                        asset = index.get(ident["source_ref"])
                        if asset is None:
                            event.remove(project)
                            deferred.append(
                                {
                                    **base,
                                    "reason": "referenced_asset_missing",
                                }
                            )
                            shard_deferred += 1
                            continue

                        ref = ident["source_ref"]
                        if patched_assets.get(ref) not in (None, str(chosen)):
                            event.remove(project)
                            deferred.append(
                                {
                                    **base,
                                    "reason": "asset_ref_resolves_to_multiple_paths",
                                    "paths": [patched_assets[ref], str(chosen)],
                                }
                            )
                            shard_deferred += 1
                            continue

                        patch_asset_path(asset, chosen)
                        patched_assets[ref] = str(chosen)
                        kept.append(
                            {
                                **base,
                                "chosen_path": str(chosen),
                                "resolution_method": closure_row.get("resolution_method"),
                            }
                        )
                        shard_kept += 1

                remove_empty_events(xml_root)
                if shard_kept:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    ET.indent(xml_root)
                    destination.write_bytes(
                        ET.tostring(
                            xml_root,
                            encoding="utf-8",
                            xml_declaration=True,
                        )
                    )

                shard_rows.append(
                    {
                        "input_xml": str(xml_path),
                        "staged_xml": str(destination) if shard_kept else None,
                        "kept": shard_kept,
                        "deferred": shard_deferred,
                        "excluded": shard_excluded,
                    }
                )
            except Exception as exc:
                parse_errors.append(
                    {
                        "xml": str(xml_path),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

            if seen_xml % 25 == 0:
                print(f"  staged {seen_xml}/{total_xml}")

    reason_counts = Counter(row["reason"] for row in deferred)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "closure_report": str(args.closure_report),
        "output_root": str(args.output_root),
        "physical_xml_files": total_xml,
        "staged_xml_files": sum(bool(row["staged_xml"]) for row in shard_rows),
        "kept_vclip_appearances": len(kept),
        "deferred_vclip_appearances": len(deferred),
        "known_non_drone_excluded": len(excluded),
        "deferred_reason_counts": dict(reason_counts),
        "parse_errors": parse_errors,
        "shards": shard_rows,
        "kept": kept,
        "deferred": deferred,
        "excluded": excluded,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print()
    print("AVAILABLE CORPUS")
    print("================")
    print(f"Staged review XMLs:       {report['staged_xml_files']:,}")
    print(f"Available VClip projects: {len(kept):,}")
    print(f"Deferred VClip projects:  {len(deferred):,}")
    print(f"Non-drone excluded:       {len(excluded):,}")
    for reason, count in sorted(reason_counts.items()):
        print(f"  deferred {reason:34s} {count:6,d}")
    print(f"Report: {args.report}")

    if parse_errors:
        print(f"ERROR: {len(parse_errors)} staging XML parse error(s)")
        return 2
    if not kept:
        print("ERROR: no currently resolvable VClip projects")
        return 3
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input-root", type=Path, action="append", required=True)
    p.add_argument("--closure-report", type=Path, required=True)
    p.add_argument("--output-root", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument(
        "--clean",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Rebuild the disposable staging root before writing.",
    )
    return p


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
