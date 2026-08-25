#!/usr/bin/env python3
"""Audit a VClip reconstructed-corpus dedupe dry-run before destructive write.

Checks:
- before/after counts by product role
- unique active stock IDs vs active project rows
- duplicate active stock IDs
- canonical clusters / largest clusters
- near-duplicate removals that do NOT directly satisfy configured thresholds
  (detects transitive Union-Find collapse)
- suspicious self-canonical removals
- lowest-margin near-duplicate pairs
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def run(args: argparse.Namespace) -> int:
    d = json.loads(args.report.read_text(encoding="utf-8"))
    active = d.get("active_candidates", [])
    removals = d.get("removals", [])

    containment_threshold = float(d.get("containment_threshold", 0.95))
    iou_threshold = float(d.get("iou_threshold", 0.90))

    after_by_role = Counter(x.get("role", "?") for x in active)
    removed_by_role = Counter(x.get("role", "?") for x in removals)
    before_by_role = after_by_role + removed_by_role

    active_ids = [x.get("stock_clip_id") for x in active if x.get("stock_clip_id")]
    active_id_counts = Counter(active_ids)
    duplicate_active_ids = {
        k: v for k, v in active_id_counts.items() if v > 1
    }

    by_canonical = defaultdict(list)
    for r in removals:
        by_canonical[r.get("canonical_stock_clip_id")].append(r)

    near = [r for r in removals if r.get("reason") == "obvious_near_duplicate"]
    direct_threshold_violations = [
        r for r in near
        if float(r.get("containment") or 0.0) < containment_threshold
        or float(r.get("iou") or 0.0) < iou_threshold
    ]

    self_canonical = [
        r for r in removals
        if r.get("removed_stock_clip_id") == r.get("canonical_stock_clip_id")
    ]

    print("VClip dedupe dry-run audit")
    print("==========================")
    print(f"Projects before: {d.get('candidate_projects_before', 0):,}")
    print(f"Projects after:  {d.get('candidate_projects_after', 0):,}")
    print(f"Removed:         {d.get('projects_removed', 0):,}")
    print(f"Clusters:        {d.get('clusters', 0):,}")
    print()

    print("By role")
    print("-------")
    roles = sorted(set(before_by_role) | set(after_by_role) | set(removed_by_role))
    for role in roles:
        print(
            f"{role:18s} "
            f"before={before_by_role[role]:5d} "
            f"removed={removed_by_role[role]:4d} "
            f"after={after_by_role[role]:5d}"
        )
    print()

    print("Identity")
    print("--------")
    print(f"Active project rows:           {len(active):,}")
    print(f"Unique active stock_clip_ids:  {len(active_id_counts):,}")
    print(f"Duplicate active stock IDs:    {len(duplicate_active_ids):,}")
    print(f"Self-canonical removals:       {len(self_canonical):,}")
    if duplicate_active_ids:
        print("First duplicate active IDs:")
        for stock_id, n in list(sorted(duplicate_active_ids.items()))[:20]:
            print(f"  {stock_id}: {n}")
    print()

    print("Near-duplicate safety")
    print("---------------------")
    print(f"Configured containment >= {containment_threshold:.3f}")
    print(f"Configured IoU         >= {iou_threshold:.3f}")
    print(f"Near-duplicate removals:       {len(near):,}")
    print(f"Direct threshold violations:   {len(direct_threshold_violations):,}")

    if near:
        ranked = sorted(
            near,
            key=lambda r: (
                float(r.get("iou") or 0.0),
                float(r.get("containment") or 0.0),
            ),
        )
        print("\nLowest-margin near-duplicate removals:")
        for r in ranked[:20]:
            print(
                f"  IoU={float(r.get('iou') or 0):.4f} "
                f"contain={float(r.get('containment') or 0):.4f} "
                f"{r.get('role')}  {r.get('source_name')}"
            )
            print(f"    REMOVE {r.get('removed_project_name')}")
            print(f"    KEEP   {r.get('kept_project_name')}")
    print()

    print("Largest canonical groups")
    print("------------------------")
    groups = sorted(
        by_canonical.items(),
        key=lambda kv: (-len(kv[1]), str(kv[0])),
    )
    for canonical, rows in groups[:20]:
        sample = rows[0]
        print(
            f"  canonical={canonical}  removed={len(rows)}  "
            f"role={sample.get('role')}  source={sample.get('source_name')}"
        )
        for r in rows[:5]:
            print(
                f"    {r.get('reason'):23s} "
                f"IoU={float(r.get('iou') or 0):.4f} "
                f"contain={float(r.get('containment') or 0):.4f} "
                f"{r.get('removed_project_name')}"
            )
    print()

    if direct_threshold_violations:
        print("RESULT: BLOCK")
        print(
            "At least one candidate was removed as a near duplicate even though "
            "the winner/loser pair does not directly satisfy the configured "
            "thresholds. This is consistent with transitive Union-Find clustering."
        )
        return 2

    if duplicate_active_ids:
        print("RESULT: REVIEW")
        print(
            "No transitive-threshold violation found, but duplicate active stock "
            "IDs remain and should be resolved before DB import."
        )
        return 1

    print("RESULT: PASS")
    print("No direct safety violation detected in the dry-run report.")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--report", type=Path, required=True)
    return p


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
