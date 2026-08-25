#!/usr/bin/env python3
"""Media-closure audit for physical VClip review shards.

The review corpus is the authority for what must resolve. This tool never relinks
or mutates Final Cut XML. It finds each physical VCLIP project, resolves its source
video from the XML or explicit media roots, and emits a deterministic closure
report. Exit status is non-zero when any in-scope source is missing or ambiguous.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlparse

try:
    from vclip_pipeline.stockify.fcpxml import (
        build_resource_index,
        first_direct_child,
        local_name,
        read_vclip_metadata,
    )
    from vclip_pipeline.workflow.camera_scope import (
        SCOPE_OUT_OF_SCOPE_NON_DRONE,
        classify_vclip_camera_scope,
    )
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "Could not import VClip. Run with PYTHONPATH=<repo>/src. "
        f"Import error: {exc}"
    )

VCLIP_RE = re.compile(r"VCLIP_[0-9A-F]{12,64}")
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}
SKIP_DIRS = {
    ".Trash",
    ".Trashes",
    "__Trash",
    "Render Files",
    "Transcoded Media",
    "Analysis Files",
    "Backups",
    "Shared Items",
    ".Spotlight-V100",
    ".fseventsd",
}


@dataclass
class Appearance:
    stock_clip_id: str
    xml_path: str
    event_name: str
    project_name: str
    source_name: str
    source_stem: str
    xml_media_paths: list[str]
    camera_scope: str
    camera_family: str
    resolved_path: str | None = None
    resolution_method: str | None = None


@dataclass
class SourceResult:
    source_stem: str
    source_name: str
    appearances: int
    candidate_ids: list[str]
    xml_paths: list[str]
    candidate_media_paths: list[str]
    chosen_path: str | None
    status: str
    resolution_method: str | None


def file_url_to_path(value: str | None) -> Path | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme and parsed.scheme != "file":
        return None
    raw = parsed.path if parsed.scheme == "file" else value
    return Path(unquote(raw)) if raw else None


def asset_media_paths(asset: ET.Element | None) -> list[Path]:
    if asset is None:
        return []
    out: list[Path] = []
    for child in list(asset):
        if local_name(child.tag) != "media-rep":
            continue
        path = file_url_to_path(child.get("src"))
        if path is not None:
            out.append(path)
    return out


def iter_xmls(roots: Iterable[Path]) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for root in roots:
        root = root.expanduser().resolve()
        if root.is_file() and root.suffix.lower() == ".fcpxml":
            candidates = [root]
        elif root.is_dir():
            candidates = sorted(root.rglob("*.fcpxml"))
        else:
            candidates = []
        for path in candidates:
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved not in seen:
                seen.add(resolved)
                out.append(path)
    return sorted(out, key=lambda p: str(p).casefold())


def parse_appearances(xml_path: Path) -> list[Appearance]:
    root = ET.parse(xml_path).getroot()
    resources = first_direct_child(root, "resources")
    if resources is None:
        raise RuntimeError(f"No <resources> in {xml_path}")
    index = build_resource_index(resources)
    out: list[Appearance] = []

    for event in root.iter():
        if local_name(event.tag) != "event":
            continue
        event_name = event.get("name") or ""
        for project in list(event):
            if local_name(project.tag) != "project":
                continue
            project_name = project.get("name") or ""
            blob = ET.tostring(project, encoding="unicode")
            ids = sorted(set(VCLIP_RE.findall(blob)))
            if not ids:
                continue
            asset_clip = next(
                (node for node in project.iter() if local_name(node.tag) == "asset-clip"),
                None,
            )
            if asset_clip is None:
                continue
            metadata = read_vclip_metadata(asset_clip)
            metadata_id = metadata.get("com.vclip.stock_clip_id")
            if metadata_id:
                ids = [metadata_id]
            ref = asset_clip.get("ref") or ""
            asset = index.get(ref)
            source_name = (
                asset_clip.get("name")
                or (asset.get("name") if asset is not None else None)
                or ""
            )
            stem = Path(source_name).stem.casefold()
            paths = asset_media_paths(asset)
            scope = classify_vclip_camera_scope(
                source_basename=source_name,
                media_path=str(paths[0]) if paths else None,
                source_event_name=event_name,
                source_project_name=project_name,
            )
            for stock_id in ids:
                out.append(
                    Appearance(
                        stock_clip_id=stock_id,
                        xml_path=str(xml_path),
                        event_name=event_name,
                        project_name=project_name,
                        source_name=source_name,
                        source_stem=stem,
                        xml_media_paths=[str(path) for path in paths],
                        camera_scope=str(scope.get("camera_scope") or "unknown"),
                        camera_family=str(scope.get("camera_family") or "unknown"),
                    )
                )
    return out


def media_path_score(path: Path) -> tuple[int, int, int, str]:
    text = str(path).casefold()
    score = 0
    if "/drone/" in text:
        score += 100
    if "original media" in text:
        score += 25
    if ".fcpbundle/" in text:
        score -= 10
    if "/__trash/" in text or "/.trash" in text:
        score -= 100
    try:
        size = path.stat().st_size
    except OSError:
        size = -1
    return score, 1 if path.suffix == ".MP4" else 0, size, str(path)


def index_media(roots: list[Path], wanted: set[str]) -> dict[str, list[Path]]:
    found: dict[str, list[Path]] = defaultdict(list)
    if not wanted:
        return found
    scanned = 0
    for root in roots:
        root = root.expanduser().resolve()
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, onerror=None):
            dirnames[:] = [
                name
                for name in dirnames
                if name not in SKIP_DIRS
                and not name.startswith(".")
            ]
            for filename in filenames:
                suffix = Path(filename).suffix.lower()
                if suffix not in VIDEO_EXTENSIONS:
                    continue
                scanned += 1
                stem = Path(filename).stem.casefold()
                if stem not in wanted:
                    continue
                path = Path(dirpath) / filename
                try:
                    if path.is_file():
                        found[stem].append(path)
                except OSError:
                    continue
    print(f"  scanned {scanned:,} video file(s) under media roots")
    return found


def run(args: argparse.Namespace) -> int:
    xmls = iter_xmls(args.input_root)
    if not xmls:
        raise SystemExit("No FCPXML files found.")

    appearances: list[Appearance] = []
    parse_errors: list[dict[str, str]] = []
    print(f"Reading {len(xmls):,} review shard(s)...")
    for index, path in enumerate(xmls, 1):
        try:
            appearances.extend(parse_appearances(path))
        except Exception as exc:
            parse_errors.append({"xml": str(path), "error": f"{type(exc).__name__}: {exc}"})
        if index % 25 == 0:
            print(f"  parsed {index}/{len(xmls)}")

    in_scope = [
        appearance
        for appearance in appearances
        if appearance.camera_scope != SCOPE_OUT_OF_SCOPE_NON_DRONE
    ]
    excluded = len(appearances) - len(in_scope)

    by_stem: dict[str, list[Appearance]] = defaultdict(list)
    for appearance in in_scope:
        by_stem[appearance.source_stem].append(appearance)

    unresolved: set[str] = set()
    direct_paths: dict[str, list[Path]] = defaultdict(list)
    for stem, rows in by_stem.items():
        for row in rows:
            for raw in row.xml_media_paths:
                path = Path(raw)
                try:
                    if path.is_file():
                        direct_paths[stem].append(path)
                except OSError:
                    pass
        if not direct_paths.get(stem):
            unresolved.add(stem)

    print(f"Physical VCLIP appearances: {len(appearances):,}")
    print(f"Known non-drone excluded:  {excluded:,}")
    print(f"Drone/unknown appearances: {len(in_scope):,}")
    print(f"Unique source stems:       {len(by_stem):,}")
    print(f"Need media-root search:    {len(unresolved):,}")

    indexed = index_media(
        [path.expanduser().resolve() for path in args.media_root],
        unresolved,
    )

    results: list[SourceResult] = []
    missing: list[SourceResult] = []
    ambiguous: list[SourceResult] = []

    for stem, rows in sorted(by_stem.items()):
        candidates = list(direct_paths.get(stem, [])) + list(indexed.get(stem, []))
        unique: dict[str, Path] = {}
        for path in candidates:
            try:
                if path.is_file():
                    unique[str(path.resolve())] = path.resolve()
            except OSError:
                continue
        ranked = sorted(unique.values(), key=media_path_score, reverse=True)
        chosen = ranked[0] if ranked else None

        # Multiple physical copies are expected. Treat them as ambiguous only if
        # the top two have the same preference score but materially different size.
        status = "resolved" if chosen else "missing"
        method = "xml_media_path" if chosen and chosen in direct_paths.get(stem, []) else "media_index"
        if len(ranked) > 1:
            top_a = media_path_score(ranked[0])
            top_b = media_path_score(ranked[1])
            if top_a[:2] == top_b[:2] and top_a[2] != top_b[2]:
                status = "ambiguous"
                chosen = None
                method = None

        result = SourceResult(
            source_stem=stem,
            source_name=rows[0].source_name,
            appearances=len(rows),
            candidate_ids=sorted({row.stock_clip_id for row in rows}),
            xml_paths=sorted({row.xml_path for row in rows}),
            candidate_media_paths=[str(path) for path in ranked],
            chosen_path=str(chosen) if chosen else None,
            status=status,
            resolution_method=method,
        )
        results.append(result)
        if status == "missing":
            missing.append(result)
        elif status == "ambiguous":
            ambiguous.append(result)

    resolved_appearances = sum(
        row.appearances for row in results if row.status == "resolved"
    )
    missing_appearances = sum(row.appearances for row in missing)
    ambiguous_appearances = sum(row.appearances for row in ambiguous)

    strict_pass = not missing and not ambiguous and not parse_errors
    available_pass = bool(resolved_appearances) and not parse_errors
    closure_status = (
        "pass"
        if strict_pass
        else "partial"
        if args.allow_partial and available_pass
        else "fail"
    )

    report = {
        "input_roots": [str(path.expanduser().resolve()) for path in args.input_root],
        "media_roots": [str(path.expanduser().resolve()) for path in args.media_root],
        "xml_files": len(xmls),
        "physical_appearances": len(appearances),
        "known_non_drone_excluded": excluded,
        "in_scope_appearances": len(in_scope),
        "unique_source_stems": len(results),
        "resolved_sources": sum(row.status == "resolved" for row in results),
        "missing_sources": len(missing),
        "ambiguous_sources": len(ambiguous),
        "resolved_appearances": resolved_appearances,
        "missing_appearances": missing_appearances,
        "ambiguous_appearances": ambiguous_appearances,
        "strict_pass": strict_pass,
        "available_pass": available_pass,
        "closure_status": closure_status,
        "parse_errors": parse_errors,
        "sources": [asdict(row) for row in results],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print()
    print("MEDIA CLOSURE")
    print("=============")
    print(f"Resolved source stems:  {report['resolved_sources']:,}")
    print(f"Missing source stems:   {len(missing):,}")
    print(f"Ambiguous source stems: {len(ambiguous):,}")
    print(f"XML parse errors:       {len(parse_errors):,}")
    print(f"Report: {args.report}")

    if missing:
        print("\nMissing sources:")
        for row in missing[:100]:
            print(f"  {row.source_name}  ({row.appearances} VClip appearance(s))")
    if ambiguous:
        print("\nAmbiguous sources:")
        for row in ambiguous[:100]:
            print(f"  {row.source_name}")
            for path in row.candidate_media_paths[:5]:
                print(f"    {path}")

    print(f"Resolved VClip appearances:  {resolved_appearances:,}")
    print(f"Deferred missing appearances:{missing_appearances:8,d}")
    print(f"Deferred ambiguous appearances:{ambiguous_appearances:6,d}")
    print(f"\nMEDIA CLOSURE: {closure_status.upper()}")
    if parse_errors:
        return 3
    if strict_pass or (args.allow_partial and available_pass):
        return 0
    return 2


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input-root", type=Path, action="append", required=True)
    p.add_argument("--media-root", type=Path, action="append", required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "Return success when XML parsing is clean and at least one source "
            "resolves. Missing/ambiguous sources remain explicitly deferred."
        ),
    )
    return p


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
