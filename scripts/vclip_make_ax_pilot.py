#!/usr/bin/env python3
"""Create a tiny Final Cut AX pilot FCPXML from a reconstructed VClip shard.

The source FCPXML is never modified. The output keeps the original resources,
keeps only the first N projects from the Ready Cuts event, gives the event a
unique test name, and renames the projects so pilot renders are obvious.
"""

from __future__ import annotations

import argparse
import copy
import xml.etree.ElementTree as ET
from pathlib import Path


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def direct_child(parent: ET.Element, name: str) -> ET.Element | None:
    return next((x for x in list(parent) if local_name(x.tag) == name), None)


def run(args: argparse.Namespace) -> int:
    src = args.input.expanduser().resolve()
    dst = args.output.expanduser().resolve()

    if not src.is_file():
        raise SystemExit(f"Input FCPXML not found: {src}")

    tree = ET.parse(src)
    root = tree.getroot()

    library = next(
        (x for x in root.iter() if local_name(x.tag) == "library"),
        None,
    )
    if library is None:
        raise SystemExit("No <library> found in FCPXML")

    events = [x for x in list(library) if local_name(x.tag) == "event"]
    ready_events = [
        x for x in events
        if "ready cuts" in (x.get("name") or "").casefold()
    ]
    if not ready_events:
        print("Could not find a Ready Cuts event. Available events:")
        for event in events:
            projects = sum(
                1 for x in list(event) if local_name(x.tag) == "project"
            )
            print(f"  {event.get('name')!r}  projects={projects}")
        return 2

    ready = max(
        ready_events,
        key=lambda e: sum(
            1 for x in list(e) if local_name(x.tag) == "project"
        ),
    )
    projects = [
        x for x in list(ready) if local_name(x.tag) == "project"
    ]
    if len(projects) < args.count:
        raise SystemExit(
            f"Ready Cuts event has only {len(projects)} project(s); "
            f"requested {args.count}"
        )

    # Work on a copy only.
    new_root = copy.deepcopy(root)
    new_library = next(
        (x for x in new_root.iter() if local_name(x.tag) == "library"),
        None,
    )
    assert new_library is not None

    for child in list(new_library):
        if local_name(child.tag) == "event":
            new_library.remove(child)

    pilot_event = ET.Element("event", {"name": args.event_name})

    for index, source_project in enumerate(projects[: args.count], 1):
        project = copy.deepcopy(source_project)
        project.attrib.pop("uid", None)
        original_name = project.get("name") or f"Project {index}"
        project.set("name", f"VCLIP_AX_PILOT_{index:02d}")
        # Preserve the original label in a note attribute only for easy manual
        # inspection; Final Cut ignores unknown attributes poorly, so use note
        # only when already supported would be risky. Print it instead.
        print(
            f"  {index:02d}: {original_name} -> "
            f"{project.get('name')}"
        )
        pilot_event.append(project)

    new_library.append(pilot_event)

    dst.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(new_root).write(
        dst,
        encoding="utf-8",
        xml_declaration=True,
    )

    print()
    print("VClip AX pilot created")
    print("======================")
    print(f"Input:          {src}")
    print(f"Output:         {dst}")
    print(f"Event:          {args.event_name}")
    print(f"Project count:  {args.count}")
    print("Expected Share menu:")
    print(f"  Share {args.count} Projects")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--count", type=int, default=3)
    p.add_argument(
        "--event-name",
        default="VClip AX Pilot — 3 Projects",
    )
    return p


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
