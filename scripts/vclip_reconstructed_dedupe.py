#!/usr/bin/env python3
"""Remove obvious duplicate Ready Cuts and Extended Masters across a corpus.

Dedupe is deliberately conservative:
- only within the same product role (Ready Cut vs Extended Master)
- only the same source media stem
- only the same visual-treatment signature
- exact ranges, or near-identical ranges with high containment + IoU

Ready Cut and Extended Master versions of one shot are intentionally both kept.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

try:
    from vclip_pipeline.stockify.core import format_time, local_name, parse_time
    from vclip_pipeline.stockify.fcpxml import (
        build_resource_index,
        first_direct_child,
        read_vclip_metadata,
        validate_fcpxml,
        video_treatment_signature,
        write_fcpxml,
    )
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "Could not import VClip. Run with PYTHONPATH=<repo>/src. "
        f"Import error: {exc}"
    )

VCLIP_RE = re.compile(r"VCLIP_[0-9A-F]{12,64}")


@dataclass
class Candidate:
    key: str
    xml_path: str
    relative_path: str
    event_name: str
    project_name: str
    role: str
    stock_clip_id: str
    parent_ids: list[str]
    source_name: str
    source_stem: str
    start_s: float
    duration_s: float
    end_s: float
    frame_s: float
    effect_signature: str
    action: str
    qc_status: str
    operator_status: str
    visual_status: str
    readiness_basis: str
    metadata: dict[str, str]
    kept: bool = True
    duplicate_of: str | None = None
    duplicate_reason: str | None = None
    project: ET.Element = field(repr=False, default=None)
    event: ET.Element = field(repr=False, default=None)


@dataclass
class Removal:
    removed_stock_clip_id: str
    canonical_stock_clip_id: str
    removed_project_name: str
    kept_project_name: str
    role: str
    source_name: str
    removed_start_s: float
    removed_duration_s: float
    kept_start_s: float
    kept_duration_s: float
    containment: float
    iou: float
    reason: str
    removed_xml: str
    kept_xml: str


def source_role(event_name: str, metadata: dict[str, str]) -> str | None:
    # Product role is defined by the physical reconstruction event bucket.
    # Internal telemetry variants describe how a candidate was generated and
    # must not promote QC Review inventory into the customer-facing pool.
    _ = metadata
    event = event_name.casefold()
    if "ready cuts" in event:
        return "ready_cut"
    if "extended masters" in event:
        return "extended_master"
    return None


def float_meta(metadata: dict[str, str], key: str) -> float | None:
    try:
        return float(metadata[key])
    except Exception:
        return None


def event_projects(root: ET.Element):
    for event in root.iter():
        if local_name(event.tag) != "event":
            continue
        for project in list(event):
            if local_name(project.tag) == "project":
                yield event, project


def parse_file(path: Path, relative: Path) -> tuple[ET.ElementTree, list[Candidate]]:
    tree = ET.parse(path)
    root = tree.getroot()
    resources = first_direct_child(root, "resources")
    if resources is None:
        raise RuntimeError(f"No <resources> in {path}")
    index = build_resource_index(resources)
    candidates: list[Candidate] = []

    for event, project in event_projects(root):
        event_name = event.get("name") or ""
        project_name = project.get("name") or ""
        clip = next(
            (node for node in project.iter() if local_name(node.tag) == "asset-clip"),
            None,
        )
        if clip is None:
            continue
        metadata = read_vclip_metadata(clip)
        role = source_role(event_name, metadata)
        if role is None:
            continue
        stock_id = metadata.get("com.vclip.stock_clip_id")
        if not stock_id:
            ids = VCLIP_RE.findall(ET.tostring(project, encoding="unicode"))
            stock_id = ids[0] if ids else None
        if not stock_id:
            continue
        parent_ids = [
            value.strip()
            for value in metadata.get("com.vclip.telemetry.parent_ids", stock_id).split(",")
            if value.strip()
        ]
        source_name = clip.get("name") or ""
        source_stem = Path(source_name).stem.casefold()
        start_s = float_meta(metadata, "com.vclip.telemetry.source_start_s")
        duration_s = float_meta(metadata, "com.vclip.telemetry.duration_s")
        if start_s is None:
            start_s = float(parse_time(clip.get("start")))
        if duration_s is None:
            duration_s = float(parse_time(clip.get("duration")))

        sequence = first_direct_child(project, "sequence")
        frame_s = 1 / 30
        if sequence is not None:
            fmt = index.get(sequence.get("format") or "")
            if fmt is not None and fmt.get("frameDuration"):
                frame_s = float(parse_time(fmt.get("frameDuration")))

        key = f"{relative.as_posix()}::{event_name}::{project_name}::{stock_id}"
        candidates.append(
            Candidate(
                key=key,
                xml_path=str(path),
                relative_path=relative.as_posix(),
                event_name=event_name,
                project_name=project_name,
                role=role,
                stock_clip_id=stock_id,
                parent_ids=sorted(set(parent_ids)),
                source_name=source_name,
                source_stem=source_stem,
                start_s=start_s,
                duration_s=duration_s,
                end_s=start_s + duration_s,
                frame_s=frame_s,
                effect_signature=video_treatment_signature(clip),
                action=metadata.get("com.vclip.telemetry.variant", ""),
                qc_status=metadata.get("com.vclip.telemetry.qc_status", ""),
                operator_status=metadata.get("com.vclip.telemetry.operator_status", ""),
                visual_status=metadata.get("com.vclip.visual.status", ""),
                readiness_basis=metadata.get("com.vclip.readiness_basis", ""),
                metadata=metadata,
                project=project,
                event=event,
            )
        )
    return tree, candidates


def overlap(a: Candidate, b: Candidate) -> tuple[float, float, float]:
    inter = max(0.0, min(a.end_s, b.end_s) - max(a.start_s, b.start_s))
    union = max(a.end_s, b.end_s) - min(a.start_s, b.start_s)
    shorter = max(1e-9, min(a.duration_s, b.duration_s))
    return inter, inter / shorter, inter / union if union > 0 else 0.0


def exact_duplicate(a: Candidate, b: Candidate) -> bool:
    tolerance = max(a.frame_s, b.frame_s) * 1.1 + 1e-5
    return abs(a.start_s - b.start_s) <= tolerance and abs(a.end_s - b.end_s) <= tolerance


def quality_rank(row: Candidate) -> tuple[Any, ...]:
    visual = {"COHERENT": 4, "ADVISORY": 2, "NO_VISUAL": 1, "TRANSITION": 0}.get(
        row.visual_status,
        1,
    )
    readiness = {
        "telemetry_and_visual_ready": 5,
        "generated_visual_ready": 5,
        "visual_rescue": 4,
        "visual_advisory": 2,
        "visual_unavailable_fallback": 1,
    }.get(row.readiness_basis, 1)
    qc = {"PASS": 3, "NO_TELEMETRY": 2, "SOFT_REVIEW": 1, "REVIEW": 0}.get(
        row.qc_status,
        1,
    )
    operator = {"CLEAN": 3, "MOVEMENT_ADVISORY": 2, "NO_TELEMETRY": 1, "TRANSITION": 0}.get(
        row.operator_status,
        1,
    )
    human = 1 if row.stock_clip_id in row.parent_ids else 0
    tier = 3 if row.duration_s >= 10 else 2 if row.duration_s >= 5 else 1
    return (
        visual,
        readiness,
        qc,
        operator,
        human,
        tier,
        round(row.duration_s, 6),
        row.stock_clip_id,
    )


class UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def clean_empty_events(tree: ET.ElementTree) -> None:
    root = tree.getroot()
    library = next((node for node in root.iter() if local_name(node.tag) == "library"), None)
    if library is None:
        return
    for event in list(library):
        if local_name(event.tag) != "event":
            continue
        if not any(local_name(child.tag) == "project" for child in list(event)):
            library.remove(event)


def run(args: argparse.Namespace) -> int:
    input_root = args.input_root.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    xmls = sorted(input_root.rglob("*.fcpxml"))
    if not xmls:
        raise SystemExit(f"No reconstructed FCPXML files under {input_root}")

    trees: dict[Path, ET.ElementTree] = {}
    all_rows: list[Candidate] = []
    failures: list[dict[str, str]] = []
    print(f"Reading {len(xmls):,} reconstructed shard(s)...")
    for path in xmls:
        relative = path.relative_to(input_root)
        try:
            tree, rows = parse_file(path, relative)
            trees[relative] = tree
            all_rows.extend(rows)
        except Exception as exc:
            failures.append({"xml": str(path), "error": f"{type(exc).__name__}: {exc}"})

    groups: dict[tuple[str, str, str], list[Candidate]] = {}
    for row in all_rows:
        groups.setdefault((row.role, row.source_stem, row.effect_signature), []).append(row)

    removals: list[Removal] = []
    conflicts: list[dict[str, Any]] = []
    clusters_seen = 0

    for group_key, rows in groups.items():
        if len(rows) < 2:
            continue
        uf = UnionFind(len(rows))
        relation: dict[tuple[int, int], tuple[str, float, float]] = {}
        for i, a in enumerate(rows):
            for j in range(i + 1, len(rows)):
                b = rows[j]
                inter, containment, iou = overlap(a, b)
                reason: str | None = None
                if exact_duplicate(a, b):
                    reason = "exact_source_range"
                elif containment >= args.containment and iou >= args.iou:
                    reason = "obvious_near_duplicate"
                if reason:
                    uf.union(i, j)
                    relation[(i, j)] = (reason, containment, iou)

        clusters: dict[int, list[int]] = {}
        for index in range(len(rows)):
            clusters.setdefault(uf.find(index), []).append(index)

        for members in clusters.values():
            if len(members) < 2:
                continue
            clusters_seen += 1
            cluster_rows = [rows[index] for index in members]
            winner = max(cluster_rows, key=quality_rank)
            for loser in cluster_rows:
                if loser is winner:
                    continue
                inter, containment, iou = overlap(winner, loser)
                reason = "exact_source_range" if exact_duplicate(winner, loser) else "obvious_near_duplicate"
                loser.kept = False
                loser.duplicate_of = winner.stock_clip_id
                loser.duplicate_reason = reason
                removals.append(
                    Removal(
                        removed_stock_clip_id=loser.stock_clip_id,
                        canonical_stock_clip_id=winner.stock_clip_id,
                        removed_project_name=loser.project_name,
                        kept_project_name=winner.project_name,
                        role=loser.role,
                        source_name=loser.source_name,
                        removed_start_s=loser.start_s,
                        removed_duration_s=loser.duration_s,
                        kept_start_s=winner.start_s,
                        kept_duration_s=winner.duration_s,
                        containment=containment,
                        iou=iou,
                        reason=reason,
                        removed_xml=loser.relative_path,
                        kept_xml=winner.relative_path,
                    )
                )

    # Semantic safety gate: a separate location-consistency audit may identify
    # duplicate-looking candidates whose known parent location provenance
    # disagrees. Preserve those candidates rather than silently choosing one
    # location lineage for customer-facing metadata.
    preserved_location_conflicts: list[str] = []
    if args.location_audit:
        audit_path = args.location_audit.expanduser().resolve()
        audit = json.loads(audit_path.read_text(encoding="utf-8"))

        missing_ids = audit.get("missing_candidate_ids") or []
        if missing_ids:
            raise RuntimeError(
                "Location audit is incomplete; missing candidate IDs: "
                + ", ".join(str(value) for value in missing_ids[:20])
            )

        if int(audit.get("removal_pairs", -1)) != len(removals):
            raise RuntimeError(
                "Location audit does not match this dedupe decision set: "
                f"audit removal_pairs={audit.get('removal_pairs')}, "
                f"current removals={len(removals)}"
            )

        preserve_ids = {
            str(item["removed_stock_clip_id"])
            for item in audit.get("conflicts", [])
            if item.get("removed_stock_clip_id")
        }
        removal_ids = {item.removed_stock_clip_id for item in removals}
        unexpected = sorted(preserve_ids - removal_ids)
        if unexpected:
            raise RuntimeError(
                "Location audit asks to preserve IDs not removed by this run: "
                + ", ".join(unexpected[:20])
            )

        if preserve_ids:
            by_id = {row.stock_clip_id: row for row in all_rows}
            missing_rows = sorted(preserve_ids - set(by_id))
            if missing_rows:
                raise RuntimeError(
                    "Could not find location-conflict candidates in parsed corpus: "
                    + ", ".join(missing_rows[:20])
                )

            for stock_id in preserve_ids:
                row = by_id[stock_id]
                row.kept = True
                row.duplicate_of = None
                row.duplicate_reason = None

            removals = [
                item
                for item in removals
                if item.removed_stock_clip_id not in preserve_ids
            ]
            preserved_location_conflicts = sorted(preserve_ids)

    # Mutate copies only after the global decision is complete.
    for row in all_rows:
        if row.kept:
            continue
        try:
            row.event.remove(row.project)
        except ValueError:
            conflicts.append({"candidate": row.stock_clip_id, "error": "project_not_in_event"})

    output_root.mkdir(parents=True, exist_ok=True)
    written = 0
    validation_failures: list[dict[str, Any]] = []
    for relative, tree in trees.items():
        clean_empty_events(tree)
        validation = validate_fcpxml(tree.getroot())
        if not validation.passed:
            validation_failures.append(
                {"xml": relative.as_posix(), "errors": validation.errors[:20]}
            )
            continue
        destination = output_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not args.dry_run:
            write_fcpxml(tree.getroot(), destination)
        written += 1

    active = [
        {
            "stock_clip_id": row.stock_clip_id,
            "parent_ids": row.parent_ids,
            "role": row.role,
            "xml_path": row.relative_path,
            "event_name": row.event_name,
            "project_name": row.project_name,
            "source_name": row.source_name,
            "source_stem": row.source_stem,
            "start_s": row.start_s,
            "duration_s": row.duration_s,
            "end_s": row.end_s,
            "effect_signature": row.effect_signature,
            "action": row.action,
            "qc_status": row.qc_status,
            "operator_status": row.operator_status,
            "visual_status": row.visual_status,
            "readiness_basis": row.readiness_basis,
            "metadata": row.metadata,
        }
        for row in all_rows
        if row.kept
    ]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_root": str(input_root),
        "output_root": str(output_root),
        "xml_files": len(xmls),
        "candidate_projects_before": len(all_rows),
        "clusters": clusters_seen,
        "projects_removed": len(removals),
        "candidate_projects_after": len(active),
        "files_written": written,
        "dry_run": args.dry_run,
        "containment_threshold": args.containment,
        "iou_threshold": args.iou,
        "location_audit": (
            str(args.location_audit.expanduser().resolve())
            if args.location_audit
            else None
        ),
        "location_conflicts_preserved": preserved_location_conflicts,
        "parse_failures": failures,
        "validation_failures": validation_failures,
        "conflicts": conflicts,
        "removals": [asdict(row) for row in removals],
        "active_candidates": active,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": report["generated_at"],
                    "input_root": str(input_root),
                    "output_root": str(output_root),
                    "active_candidates": active,
                    "dedupe_removals": [asdict(row) for row in removals],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    print()
    print("RECONSTRUCTED CORPUS DEDUPE")
    print("===========================")
    print(f"Candidate projects before: {len(all_rows):,}")
    print(f"Duplicate clusters:        {clusters_seen:,}")
    print(f"Projects removed:          {len(removals):,}")
    print(
        f"Location conflicts kept:   "
        f"{len(preserved_location_conflicts):,}"
    )
    print(f"Candidate projects after:  {len(active):,}")
    print(f"FCPXML files written:      {written:,}")
    print(f"Report:                    {args.report}")
    if args.manifest:
        print(f"Manifest:                  {args.manifest}")

    failed = bool(failures or validation_failures or conflicts)
    return 1 if failed else 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input-root", type=Path, required=True)
    p.add_argument("--output-root", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--manifest", type=Path)
    p.add_argument("--containment", type=float, default=0.95)
    p.add_argument("--iou", type=float, default=0.90)
    p.add_argument(
        "--location-audit",
        type=Path,
        help=(
            "Optional JSON from vclip_dedupe_location_audit.py. "
            "Known parent-location conflicts are preserved instead of deduped."
        ),
    )
    p.add_argument("--dry-run", action="store_true")
    return p


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
