"""Scan mounted volumes for currently available media and SRT sidecars."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .constants import SIDECAR_EXTENSIONS, SKIPPED_SCAN_DIR_NAMES
from .sidecars import normalized_stem

MEDIA_EXTENSIONS = {".mp4", ".mov", ".mxf", ".mts", ".m4v", ".avi"}


@dataclass
class VolumeFileIndex:
    media_by_stem: dict[str, list[str]] = field(default_factory=dict)
    srt_by_stem: dict[str, list[str]] = field(default_factory=dict)
    roots_scanned: list[str] = field(default_factory=list)
    scan_errors: list[str] = field(default_factory=list)


def build_volume_file_index(
    needed_stems: set[str],
    scan_roots: Iterable[Path],
    *,
    progress: Callable[[str], None] | None = None,
) -> VolumeFileIndex:
    """Index media/SRT paths under scan_roots whose stems are in needed_stems."""
    index = VolumeFileIndex()
    if not needed_stems:
        return index
    if progress:
        progress(f"Checking mounted volumes for {len(needed_stems)} source file stem(s).")
    for root in scan_roots:
        expanded = Path(root).expanduser()
        index.roots_scanned.append(str(expanded))
        if not expanded.exists():
            index.scan_errors.append(f"Scan root not mounted: {expanded}")
            continue
        try:
            for dirpath, dirnames, filenames in os.walk(expanded, onerror=None):
                dirnames[:] = [
                    dirname
                    for dirname in dirnames
                    if dirname not in SKIPPED_SCAN_DIR_NAMES
                    and not dirname.endswith(".fcpbundle")
                ]
                for filename in filenames:
                    stem = normalized_stem(filename)
                    if stem not in needed_stems:
                        continue
                    path = Path(dirpath) / filename
                    suffix = path.suffix.lower()
                    try:
                        resolved = str(path.resolve())
                    except OSError:
                        resolved = str(path)
                    if suffix in MEDIA_EXTENSIONS:
                        index.media_by_stem.setdefault(stem, []).append(resolved)
                    elif suffix in SIDECAR_EXTENSIONS:
                        index.srt_by_stem.setdefault(stem, []).append(resolved)
        except OSError as exc:
            index.scan_errors.append(f"Could not scan {expanded}: {exc}")
    return index


def first_existing_path(*candidates: str | Path | None) -> str | None:
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file():
            try:
                return str(path.resolve())
            except OSError:
                return str(path)
    return None


def sibling_srt_for_media(media_path: str | Path | None) -> str | None:
    if not media_path:
        return None
    path = Path(media_path)
    if not path.name:
        return None
    from .sidecars import same_stem_srt_paths

    for srt_path in same_stem_srt_paths(path):
        if srt_path.is_file():
            try:
                return str(srt_path.resolve())
            except OSError:
                return str(srt_path)
    return None
