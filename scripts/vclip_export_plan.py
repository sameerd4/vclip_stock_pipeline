#!/usr/bin/env python3
"""Build deterministic, one-event Final Cut export batches from the VClip pool.

Each batch FCPXML contains only customer-eligible Ready Cuts or Extended Masters.
Project names are changed in the export staging XML to deterministic ASCII
basenames beginning with the stable VCLIP ID. The clean reconstructed review XML
is never modified.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from vclip_pipeline.stockify.core import local_name, stable_uid
    from vclip_pipeline.stockify.fcpxml import (
        add_vclip_metadata,
        first_direct_child,
        read_vclip_metadata,
        validate_fcpxml,
        write_fcpxml,
    )
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "Could not import VClip. Run with PYTHONPATH=<repo>/src. "
        f"Import error: {exc}"
    )


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def candidate_map(con: sqlite3.Connection) -> tuple[str, dict[str, dict[str, Any]]]:
    rows = con.execute(
        """
        SELECT * FROM reconstructed_candidates
        WHERE active=1 AND product_role IN ('ready_cut','extended_master')
        ORDER BY shard_path, product_role, orientation, frame_rate, project_name
        """
    ).fetchall()
    if not rows:
        raise RuntimeError("No active reconstructed candidates in DB.")
    run_ids = {row["reconstruction_run_id"] for row in rows}
    if len(run_ids) != 1:
        raise RuntimeError(f"Expected one active reconstruction run, found {sorted(run_ids)}")
    return next(iter(run_ids)), {row["stock_clip_id"]: dict(row) for row in rows}


def project_stock_id(project: ET.Element) -> str | None:
    clip = next(
        (node for node in project.iter() if local_name(node.tag) == "asset-clip"),
        None,
    )
    if clip is None:
        return None
    return read_vclip_metadata(clip).get("com.vclip.stock_clip_id")


def format_signature(project: ET.Element, resources: ET.Element) -> tuple[str, str, str, str]:
    index = {
        child.get("id"): child
        for child in list(resources)
        if child.get("id")
    }
    sequence = first_direct_child(project, "sequence")
    fmt_id = sequence.get("format") if sequence is not None else None
    fmt = index.get(fmt_id or "")
    width = fmt.get("width", "") if fmt is not None else ""
    height = fmt.get("height", "") if fmt is not None else ""
    frame = fmt.get("frameDuration", "") if fmt is not None else ""
    color = sequence.get("colorSpace", "") if sequence is not None else ""
    return width, height, frame, color


def role_from_event(name: str) -> str | None:
    lower = name.casefold()
    if "ready cuts" in lower:
        return "ready_cut"
    if "extended masters" in lower:
        return "extended_master"
    return None


def selected_projects(path: Path, active: dict[str, dict[str, Any]]):
    tree = ET.parse(path)
    root = tree.getroot()
    resources = first_direct_child(root, "resources")
    library = next((node for node in root.iter() if local_name(node.tag) == "library"), None)
    if resources is None or library is None:
        raise RuntimeError(f"Malformed FCPXML: {path}")
    rows: list[tuple[str, tuple[str, str, str, str], ET.Element, dict[str, Any]]] = []
    for event in list(library):
        if local_name(event.tag) != "event":
            continue
        role = role_from_event(event.get("name") or "")
        if role is None:
            continue
        for project in list(event):
            if local_name(project.tag) != "project":
                continue
            stock_id = project_stock_id(project)
            candidate = active.get(stock_id or "")
            if not candidate or candidate["product_role"] != role:
                continue
            rows.append((role, format_signature(project, resources), project, candidate))
    return tree, rows


def chunks(items: list[Any], size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def build_batch_root(
    source_tree: ET.ElementTree,
    projects: list[tuple[ET.Element, dict[str, Any]]],
    *,
    event_name: str,
    plan_id: str,
    batch_id: str,
) -> ET.Element:
    root = copy.deepcopy(source_tree.getroot())
    library = next((node for node in root.iter() if local_name(node.tag) == "library"), None)
    if library is None:
        raise RuntimeError("No <library> in source FCPXML")
    for child in list(library):
        if local_name(child.tag) == "event":
            library.remove(child)
    event = ET.SubElement(
        library,
        "event",
        {"name": event_name, "uid": stable_uid("vclip-export-event", batch_id)},
    )
    for original_project, candidate in projects:
        project = copy.deepcopy(original_project)
        original_name = project.get("name") or candidate["project_name"]
        export_name = candidate["expected_export_basename"]
        project.set("name", export_name)
        project.set("uid", stable_uid("vclip-export-project", plan_id, candidate["stock_clip_id"]))
        clip = next(
            (node for node in project.iter() if local_name(node.tag) == "asset-clip"),
            None,
        )
        if clip is None:
            raise RuntimeError(f"Export project has no asset-clip: {original_name}")
        add_vclip_metadata(
            clip,
            {
                "com.vclip.export.plan_id": plan_id,
                "com.vclip.export.batch_id": batch_id,
                "com.vclip.export.basename": export_name,
                "com.vclip.export.original_project_name": original_name,
                "com.vclip.export.product_role": candidate["product_role"],
            },
        )
        event.append(project)
    return root


def run(args: argparse.Namespace) -> int:
    xml_root = args.xml_root.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    render_root = args.render_root.expanduser().resolve()
    library_root = (
        args.library_root.expanduser().resolve()
        if args.library_root
        else None
    )
    library_name = (
        args.library_name.strip()
        if args.library_name and args.library_name.strip()
        else None
    )
    if bool(library_root) != bool(library_name):
        raise RuntimeError(
            "--library-root and --library-name must be provided together"
        )

    con = connect(args.db.expanduser().resolve())
    reconstruction_run_id, active = candidate_map(con)

    source_files = sorted(xml_root.rglob("*.fcpxml"))
    selected_count = 0
    source_data: list[tuple[Path, ET.ElementTree, list[Any]]] = []
    for path in source_files:
        tree, rows = selected_projects(path, active)
        if rows:
            source_data.append((path, tree, rows))
            selected_count += len(rows)
    if selected_count != len(active):
        missing = sorted(set(active) - {
            row[3]["stock_clip_id"]
            for _path, _tree, rows in source_data
            for row in rows
        })
        raise RuntimeError(
            f"DB/XML mismatch: active={len(active)}, selected={selected_count}, "
            f"missing={len(missing)}. First missing: {missing[:10]}"
        )

    signature_payload = {
        "reconstruction_run_id": reconstruction_run_id,
        "candidate_ids": sorted(active),
        "max_projects": args.max_projects,
        "share_destination": args.share_destination,
        "render_root": str(render_root),
        "library_root": str(library_root) if library_root else None,
        "library_name": library_name,
    }
    plan_id = "EXPORTPLAN_" + hashlib.sha256(
        json.dumps(signature_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16].upper()
    plan_root = output_root / plan_id
    batch_root = plan_root / "batches"
    receipt_root = plan_root / "receipts"
    batch_root.mkdir(parents=True, exist_ok=True)
    receipt_root.mkdir(parents=True, exist_ok=True)

    batches: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    batch_index = 0

    for source_path, source_tree, rows in source_data:
        grouped: dict[tuple[str, tuple[str, str, str, str]], list[tuple[ET.Element, dict[str, Any]]]] = {}
        for role, signature, project, candidate in rows:
            grouped.setdefault((role, signature), []).append((project, candidate))
        for (role, signature), members in sorted(grouped.items(), key=lambda item: str(item[0])):
            members.sort(key=lambda pair: pair[1]["expected_export_basename"])
            for part in chunks(members, args.max_projects):
                batch_index += 1
                seed = (
                    f"{source_path.relative_to(xml_root).as_posix()}|{role}|{signature}|"
                    + "|".join(candidate["stock_clip_id"] for _, candidate in part)
                )
                batch_id = "BATCH_" + hashlib.sha256(seed.encode()).hexdigest()[:16].upper()
                event_name = f"VClip Export — {batch_id} — {role} — {len(part)} Projects"
                xml_path = batch_root / f"{batch_index:04d}--{batch_id}.fcpxml"
                output_directory = render_root / plan_id / f"{batch_index:04d}--{batch_id}"
                root = build_batch_root(
                    source_tree,
                    part,
                    event_name=event_name,
                    plan_id=plan_id,
                    batch_id=batch_id,
                )
                validation = validate_fcpxml(root)
                if not validation.passed:
                    raise RuntimeError(
                        f"Batch {batch_id} failed validation: {validation.errors[:10]}"
                    )
                write_fcpxml(root, xml_path)
                output_directory.mkdir(parents=True, exist_ok=True)
                batch = {
                    "batch_id": batch_id,
                    "batch_index": batch_index,
                    "source_shard": str(source_path.relative_to(xml_root)),
                    "product_role": role,
                    "format_signature": list(signature),
                    "xml_path": str(xml_path),
                    "event_name": event_name,
                    "output_directory": str(output_directory),
                    "expected_count": len(part),
                    "receipt_path": str(receipt_root / f"{batch_id}.json"),
                }
                batches.append(batch)
                for project, candidate in part:
                    items.append(
                        {
                            "batch_id": batch_id,
                            "stock_clip_id": candidate["stock_clip_id"],
                            "expected_basename": candidate["expected_export_basename"],
                            "original_project_name": candidate["project_name"],
                            "product_role": candidate["product_role"],
                            "duration_seconds": candidate["duration_seconds"],
                            "width": candidate["width"],
                            "height": candidate["height"],
                            "frame_rate": candidate["frame_rate"],
                            "output_directory": str(output_directory),
                        }
                    )

    manifest = {
        "schema_version": 1,
        "generated_at": now(),
        "plan_id": plan_id,
        "reconstruction_run_id": reconstruction_run_id,
        "xml_root": str(xml_root),
        "plan_root": str(plan_root),
        "render_root": str(render_root),
        "receipt_root": str(receipt_root),
        "share_destination": args.share_destination,
        "library_root": str(library_root) if library_root else None,
        "library_name": library_name,
        "max_projects_per_batch": args.max_projects,
        "batches": batches,
        "items": items,
    }
    manifest_path = plan_root / "export-plan.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print("VCLIP EXPORT PLAN")
    print("=================")
    print(f"Plan ID:           {plan_id}")
    print(f"Candidates:        {len(items):,}")
    print(f"Batches:           {len(batches):,}")
    print(f"Max per batch:     {args.max_projects}")
    print(f"Share destination: {args.share_destination}")
    print(f"Library root:      {library_root or '(not configured)'}")
    print(f"Library name:      {library_name or '(not configured)'}")
    print(f"Manifest:          {manifest_path}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--xml-root", type=Path, required=True)
    p.add_argument("--output-root", type=Path, required=True)
    p.add_argument("--render-root", type=Path, required=True)
    p.add_argument("--max-projects", type=int, default=40)
    p.add_argument("--share-destination", default="Export File (default)…")
    p.add_argument("--library-root", type=Path)
    p.add_argument("--library-name")
    return p


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
