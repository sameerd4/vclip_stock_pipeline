#!/usr/bin/env python3
"""Count reconstructed VClip projects by event bucket and telemetry variant."""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def read_metadata(clip: ET.Element) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in clip.iter():
        if local_name(node.tag) != "md":
            continue
        key = node.get("key")
        value = node.get("value")
        if key and value is not None:
            out[key] = value
    return out


def bucket(event_name: str) -> str:
    name = event_name.casefold()
    if "ready cuts" in name:
        return "ready_cuts"
    if "extended masters" in name:
        return "extended_masters"
    if "master review" in name:
        return "master_review"
    if "repair candidates" in name:
        return "repair_candidates"
    if "qc review" in name:
        return "qc_review"
    if "historical originals" in name:
        return "historical_originals"
    return "other"


def audit(root: Path) -> None:
    event_counts = Counter()
    variant_counts = Counter()
    cross = Counter()
    project_total = 0

    for path in sorted(root.rglob("*.fcpxml")):
        tree = ET.parse(path)
        for event in tree.getroot().iter():
            if local_name(event.tag) != "event":
                continue
            event_name = event.get("name") or ""
            event_bucket = bucket(event_name)
            for project in list(event):
                if local_name(project.tag) != "project":
                    continue
                project_total += 1
                clip = next(
                    (n for n in project.iter() if local_name(n.tag) == "asset-clip"),
                    None,
                )
                variant = ""
                if clip is not None:
                    variant = read_metadata(clip).get(
                        "com.vclip.telemetry.variant", ""
                    )
                event_counts[event_bucket] += 1
                variant_counts[variant or "(none)"] += 1
                cross[(event_bucket, variant or "(none)")] += 1

    print(f"Root: {root}")
    print(f"All project rows: {project_total:,}")
    print()
    print("Event buckets")
    print("-------------")
    for key, n in event_counts.most_common():
        print(f"{key:24s} {n:6,d}")

    print()
    print("Telemetry variants")
    print("------------------")
    for key, n in variant_counts.most_common():
        print(f"{key:24s} {n:6,d}")

    print()
    print("Extended-master variant by event bucket")
    print("---------------------------------------")
    for (event_bucket, variant), n in sorted(cross.items()):
        if variant == "extended_master":
            print(f"{event_bucket:24s} {n:6,d}")

    exportable = (
        event_counts["ready_cuts"]
        + event_counts["extended_masters"]
    )
    print()
    print(f"Exportable event-bucket projects: {exportable:,}")
    print(
        f"  Ready Cuts:       {event_counts['ready_cuts']:,}\n"
        f"  Extended Masters: {event_counts['extended_masters']:,}"
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    args = p.parse_args()
    audit(args.root.expanduser().resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
