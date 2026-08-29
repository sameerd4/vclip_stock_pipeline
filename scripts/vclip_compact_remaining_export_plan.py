#!/usr/bin/env python3
"""
Build a compact export plan for only the still-unrendered active VClip candidates.

Unlike scripts/vclip_export_plan.py, this planner is allowed to combine projects
from different reconstructed FCPXML shards into the same export batch. It remaps
every resource ID into one collision-free resource table, so batch count is
driven by format signature + --max-projects rather than source-shard count.

It can also read a prior export manifest and exclude candidates whose receipts
are already complete and whose rendered files still exist.

The source FCPXML corpus, source DB, prior export plan, and rendered files are
never modified.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sqlite3
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
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
except Exception as exc:
    raise SystemExit(
        "Could not import VClip. Run with PYTHONPATH=<repo>/src:<repo>/scripts. "
        f"Import error: {exc}"
    )


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def receipt_complete_ids(manifest: dict[str, Any]) -> set[str]:
    complete: set[str] = set()
    for batch in manifest.get("batches", []):
        receipt = read_json(Path(batch["receipt_path"]))
        if not receipt or receipt.get("status") != "complete":
            continue
        files = receipt.get("files") or []
        if len(files) != int(batch["expected_count"]):
            continue
        if not all(item.get("path") and Path(item["path"]).is_file() for item in files):
            continue
        complete.update(
            str(item["stock_clip_id"])
            for item in files
            if item.get("stock_clip_id")
        )
    return complete


def candidate_map(con: sqlite3.Connection) -> tuple[str, dict[str, dict[str, Any]]]:
    rows = con.execute(
        """
        SELECT *
        FROM reconstructed_candidates
        WHERE active=1
          AND product_role IN ('ready_cut','extended_master')
        ORDER BY stock_clip_id
        """
    ).fetchall()
    if not rows:
        raise RuntimeError("No active reconstructed candidates in DB.")
    run_ids = {str(row["reconstruction_run_id"]) for row in rows}
    if len(run_ids) != 1:
        raise RuntimeError(
            f"Expected one active reconstruction run, found {sorted(run_ids)}"
        )
    return next(iter(run_ids)), {
        str(row["stock_clip_id"]): dict(row)
        for row in rows
    }


def project_stock_id(project: ET.Element) -> str | None:
    clip = next(
        (node for node in project.iter() if local_name(node.tag) == "asset-clip"),
        None,
    )
    if clip is None:
        return None
    return read_vclip_metadata(clip).get("com.vclip.stock_clip_id")


def role_from_event(name: str) -> str | None:
    lower = name.casefold()
    if "ready cuts" in lower:
        return "ready_cut"
    if "extended masters" in lower:
        return "extended_master"
    return None


def format_signature(
    project: ET.Element,
    resources: ET.Element,
) -> tuple[str, str, str, str]:
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


@dataclass
class SelectedProject:
    source_path: Path
    source_root: ET.Element
    resources: ET.Element
    project: ET.Element
    candidate: dict[str, Any]
    signature: tuple[str, str, str, str]


def collect_projects(
    xml_root: Path,
    candidates: dict[str, dict[str, Any]],
) -> list[SelectedProject]:
    wanted = set(candidates)
    found: dict[str, SelectedProject] = {}

    for path in sorted(xml_root.rglob("*.fcpxml")):
        tree = ET.parse(path)
        root = tree.getroot()
        resources = first_direct_child(root, "resources")
        library = next(
            (node for node in root.iter() if local_name(node.tag) == "library"),
            None,
        )
        if resources is None or library is None:
            continue

        for event in list(library):
            if local_name(event.tag) != "event":
                continue
            role = role_from_event(event.get("name") or "")
            if role is None:
                continue

            for project in list(event):
                if local_name(project.tag) != "project":
                    continue
                sid = project_stock_id(project)
                if not sid or sid not in wanted:
                    continue
                candidate = candidates[sid]
                if candidate["product_role"] != role:
                    continue
                if sid in found:
                    raise RuntimeError(
                        f"Candidate {sid} appears in multiple selected XML projects: "
                        f"{found[sid].source_path} and {path}"
                    )
                found[sid] = SelectedProject(
                    source_path=path,
                    source_root=root,
                    resources=resources,
                    project=project,
                    candidate=candidate,
                    signature=format_signature(project, resources),
                )

    missing = sorted(wanted - set(found))
    if missing:
        raise RuntimeError(
            f"DB/XML mismatch: wanted={len(wanted)}, found={len(found)}, "
            f"missing={len(missing)}. First missing: {missing[:20]}"
        )
    return [found[sid] for sid in sorted(found)]


def rewrite_resource_refs(element: ET.Element, mapping: dict[str, str]) -> None:
    for node in element.iter():
        for key, value in list(node.attrib.items()):
            replacement = mapping.get(value)
            if replacement is not None:
                node.set(key, replacement)


def new_fcpxml_root(template: ET.Element) -> tuple[ET.Element, ET.Element, ET.Element]:
    root = ET.Element(template.tag, dict(template.attrib))
    resources = ET.SubElement(root, "resources")
    library = ET.SubElement(root, "library")
    return root, resources, library


def append_source_resources(
    destination: ET.Element,
    source: ET.Element,
    next_id: list[int],
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    children = list(source)

    for child in children:
        old_id = child.get("id")
        if old_id:
            mapping[old_id] = f"r{next_id[0]}"
            next_id[0] += 1

    for child in children:
        copied = copy.deepcopy(child)
        rewrite_resource_refs(copied, mapping)
        destination.append(copied)

    return mapping


def chunks(items: list[Any], size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def build_batch_root(
    members: list[SelectedProject],
    *,
    plan_id: str,
    batch_id: str,
    event_name: str,
) -> ET.Element:
    template = members[0].source_root
    root, resources_out, library_out = new_fcpxml_root(template)

    # One resource mapping per source document in this compact batch.
    source_maps: dict[Path, dict[str, str]] = {}
    next_id = [1]

    for source_path in sorted({member.source_path for member in members}):
        member = next(m for m in members if m.source_path == source_path)
        source_maps[source_path] = append_source_resources(
            resources_out,
            member.resources,
            next_id,
        )

    event = ET.SubElement(
        library_out,
        "event",
        {
            "name": event_name,
            "uid": stable_uid("vclip-compact-export-event", batch_id),
        },
    )

    for member in members:
        candidate = member.candidate
        project = copy.deepcopy(member.project)
        rewrite_resource_refs(project, source_maps[member.source_path])

        original_name = project.get("name") or candidate["project_name"]
        export_name = candidate["expected_export_basename"]

        project.set("name", export_name)
        project.set(
            "uid",
            stable_uid(
                "vclip-compact-export-project",
                plan_id,
                candidate["stock_clip_id"],
            ),
        )

        clip = next(
            (node for node in project.iter() if local_name(node.tag) == "asset-clip"),
            None,
        )
        if clip is None:
            raise RuntimeError(
                f"Compact export project has no asset-clip: {original_name}"
            )

        add_vclip_metadata(
            clip,
            {
                "com.vclip.export.plan_id": plan_id,
                "com.vclip.export.batch_id": batch_id,
                "com.vclip.export.basename": export_name,
                "com.vclip.export.original_project_name": original_name,
                "com.vclip.export.product_role": candidate["product_role"],
                "com.vclip.export.compacted": "1",
            },
        )
        event.append(project)

    return root


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--xml-root", type=Path, required=True)
    p.add_argument("--prior-manifest", type=Path, required=True)
    p.add_argument("--output-root", type=Path, required=True)
    p.add_argument("--render-root", type=Path, required=True)
    p.add_argument("--max-projects", type=int, default=20)
    p.add_argument("--share-destination", default="Export File (default)…")
    p.add_argument("--library-root", type=Path, required=True)
    p.add_argument("--library-name", required=True)
    return p


def main() -> int:
    args = parser().parse_args()
    if args.max_projects < 1:
        raise SystemExit("--max-projects must be >= 1")

    db_path = args.db.expanduser().resolve()
    xml_root = args.xml_root.expanduser().resolve()
    prior_manifest_path = args.prior_manifest.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    render_root = args.render_root.expanduser().resolve()
    library_root = args.library_root.expanduser().resolve()

    prior_manifest = json.loads(prior_manifest_path.read_text(encoding="utf-8"))
    completed_ids = receipt_complete_ids(prior_manifest)

    con = connect(db_path)
    reconstruction_run_id, active = candidate_map(con)
    con.close()

    remaining = {
        sid: row
        for sid, row in active.items()
        if sid not in completed_ids
    }

    if not remaining:
        print("All active candidates already have complete prior receipts.")
        return 0

    selected = collect_projects(xml_root, remaining)

    grouped: dict[
        tuple[str, tuple[str, str, str, str]],
        list[SelectedProject],
    ] = defaultdict(list)

    for member in selected:
        grouped[
            (
                str(member.candidate["product_role"]),
                member.signature,
            )
        ].append(member)

    signature_payload = {
        "planner": "vclip-compact-remaining-v1",
        "reconstruction_run_id": reconstruction_run_id,
        "candidate_ids": sorted(remaining),
        "prior_plan_id": prior_manifest.get("plan_id"),
        "completed_ids": sorted(completed_ids & set(active)),
        "max_projects": args.max_projects,
        "share_destination": args.share_destination,
        "render_root": str(render_root),
        "library_root": str(library_root),
        "library_name": args.library_name,
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

    for (role, signature), members in sorted(
        grouped.items(),
        key=lambda item: str(item[0]),
    ):
        members.sort(
            key=lambda member: member.candidate["expected_export_basename"]
        )

        for part in chunks(members, args.max_projects):
            batch_index += 1
            member_ids = [
                str(member.candidate["stock_clip_id"])
                for member in part
            ]
            seed = (
                f"{role}|{signature}|"
                + "|".join(member_ids)
            )
            batch_id = (
                "BATCH_"
                + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16].upper()
            )
            event_name = (
                f"VClip Compact Export — {batch_id} — "
                f"{role} — {len(part)} Projects"
            )
            xml_path = (
                batch_root
                / f"{batch_index:04d}--{batch_id}.fcpxml"
            )
            output_directory = (
                render_root
                / plan_id
                / f"{batch_index:04d}--{batch_id}"
            )

            root = build_batch_root(
                part,
                plan_id=plan_id,
                batch_id=batch_id,
                event_name=event_name,
            )
            validation = validate_fcpxml(root)
            if not validation.passed:
                raise RuntimeError(
                    f"Compact batch {batch_id} failed validation: "
                    f"{validation.errors[:20]}"
                )

            write_fcpxml(root, xml_path)
            output_directory.mkdir(parents=True, exist_ok=True)

            source_shards = sorted(
                {
                    str(member.source_path.relative_to(xml_root))
                    for member in part
                }
            )

            batches.append(
                {
                    "batch_id": batch_id,
                    "batch_index": batch_index,
                    "source_shard": "COMPACT",
                    "source_shards": source_shards,
                    "product_role": role,
                    "format_signature": list(signature),
                    "xml_path": str(xml_path),
                    "event_name": event_name,
                    "output_directory": str(output_directory),
                    "expected_count": len(part),
                    "receipt_path": str(receipt_root / f"{batch_id}.json"),
                }
            )

            for member in part:
                candidate = member.candidate
                items.append(
                    {
                        "batch_id": batch_id,
                        "stock_clip_id": candidate["stock_clip_id"],
                        "expected_basename": candidate[
                            "expected_export_basename"
                        ],
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
        "planner": "vclip-compact-remaining-v1",
        "generated_at": now(),
        "plan_id": plan_id,
        "reconstruction_run_id": reconstruction_run_id,
        "xml_root": str(xml_root),
        "plan_root": str(plan_root),
        "render_root": str(render_root),
        "receipt_root": str(receipt_root),
        "share_destination": args.share_destination,
        "library_root": str(library_root),
        "library_name": args.library_name,
        "max_projects_per_batch": args.max_projects,
        "prior_manifest": str(prior_manifest_path),
        "prior_plan_id": prior_manifest.get("plan_id"),
        "prior_completed_active_ids": sorted(completed_ids & set(active)),
        "batches": batches,
        "items": items,
    }
    manifest_path = plan_root / "export-plan.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )

    print("VCLIP COMPACT REMAINING EXPORT PLAN")
    print("===================================")
    print(f"Active candidates:       {len(active)}")
    print(f"Prior completed active:  {len(completed_ids & set(active))}")
    print(f"Remaining candidates:    {len(remaining)}")
    print(f"Format groups:           {len(grouped)}")
    print(f"Compact batches:         {len(batches)}")
    print(f"Max per batch:           {args.max_projects}")
    print(f"Plan ID:                 {plan_id}")
    print(f"Manifest:                {manifest_path}")
    print()
    for batch in batches:
        sig = batch["format_signature"]
        print(
            f"{batch['batch_index']:3d}  "
            f"{batch['expected_count']:2d} projects  "
            f"{sig[0]}x{sig[1]}  "
            f"{batch['batch_id']}  "
            f"sources={len(batch['source_shards'])}"
        )
    print()
    print("VCLIP COMPACT REMAINING EXPORT PLAN: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
