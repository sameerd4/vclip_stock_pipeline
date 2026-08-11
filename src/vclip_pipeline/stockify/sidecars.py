"""Locate DJI SRT sidecars and parse their telemetry into structured samples."""

from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path
from urllib.parse import unquote

from .constants import (
    SIDECAR_EXTENSIONS,
    SKIPPED_SCAN_DIR_NAMES,
    SRT_BRACKET_FIELD_RE,
    SRT_DATETIME_RE,
    SRT_NUMERIC_FIELD_RE,
    SRT_TIME_RE,
)
from .fcpxml import asset_ingest_datetime, asset_media_paths
from .metadata import is_usable_gps
from .models import SidecarIndex, SidecarMatchResult, SidecarSummary, SrtInfo, SrtSample

logger = logging.getLogger(__name__)


# Archive duplicate stems are disambiguated with Final Cut ingestDate as a heuristic.
# Ingest is not capture time (libraries may re-ingest years later), so rank by
# absolute delta and require only a clear margin over the runner-up.
_CLEAR_INGEST_MARGIN = timedelta(days=14)


# Sidecar matching

# Normalize a filename stem for case-insensitive sidecar matching.
def normalized_stem(value: str | None) -> str:
    if not value:
        return ""
    stem = Path(unquote(value)).stem
    return stem.lower()


# Accept common Finder copy suffixes as equivalent names.
def sidecar_stem_variants(stem: str) -> set[str]:
    variants = {stem}
    copy_stripped = re.sub(r"(?: copy|\s+\(\d+\))+$", "", stem).strip()
    if copy_stripped:
        variants.add(copy_stripped)
    return variants


# Build all useful SRT lookup names for an asset.
def asset_sidecar_stems(asset: ET.Element) -> set[str]:
    stems = {normalized_stem(asset.get("name"))}
    stems.update(normalized_stem(path.name) for path in asset_media_paths(asset))
    variants: set[str] = set()
    for stem in stems:
        if stem:
            variants.update(sidecar_stem_variants(stem))
    return variants


# Yield upper- and lower-case SRT neighbors for a media file.
def same_stem_srt_paths(media_path: Path) -> Iterator[Path]:
    for suffix in (".SRT", ".srt"):
        yield media_path.with_suffix(suffix)


# Read the first embedded capture datetime from an SRT without full telemetry parse.
def read_srt_capture_datetime(path: Path) -> datetime | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = SRT_DATETIME_RE.search(text)
    if not match:
        return None
    return parse_srt_payload_datetime(match.group(0))


# Prefer an SRT sitting beside the asset's media-rep path.
def sibling_srt_for_asset(asset: ET.Element) -> Path | None:
    seen: set[Path] = set()
    for media_path in asset_media_paths(asset):
        for srt_path in same_stem_srt_paths(media_path):
            try:
                resolved = srt_path.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            if srt_path.is_file():
                return resolved
    return None


# Collect archive SRT paths whose stems match any asset stem variant.
def archive_candidates_for_asset(
    asset: ET.Element,
    archive_by_stem: dict[str, tuple[Path, ...]],
) -> list[Path]:
    paths: set[Path] = set()
    for stem in asset_sidecar_stems(asset):
        paths.update(archive_by_stem.get(stem, ()))
    return sorted(paths, key=str)


# Rank duplicate archive SRTs by closeness of capture time to FCP ingestDate.
def choose_archive_srt_by_ingest_date(
    candidates: list[Path],
    ingest_at: datetime,
    *,
    asset_id: str | None = None,
) -> Path | None:
    ingest_naive = ingest_at.replace(tzinfo=None) if ingest_at.tzinfo else ingest_at
    scored: list[tuple[timedelta, datetime, Path]] = []
    for path in candidates:
        captured_at = read_srt_capture_datetime(path)
        if captured_at is None:
            logger.debug(
                "ingest-date SRT match asset=%s rejected: missing capture datetime in %s",
                asset_id,
                path,
            )
            # Refuse to guess when any duplicate lacks a capture timestamp.
            return None
        capture_naive = (
            captured_at.replace(tzinfo=None) if captured_at.tzinfo else captured_at
        )
        delta = abs(capture_naive - ingest_naive)
        scored.append((delta, capture_naive, path))
        logger.debug(
            "ingest-date SRT candidate asset=%s path=%s capture=%s delta=%s",
            asset_id,
            path,
            capture_naive.isoformat(sep=" "),
            delta,
        )

    scored.sort(key=lambda item: (item[0], str(item[2])))
    best_delta, best_capture, best_path = scored[0]
    if len(scored) > 1:
        second_delta = scored[1][0]
        margin = second_delta - best_delta
        if margin < _CLEAR_INGEST_MARGIN:
            logger.debug(
                "ingest-date SRT match asset=%s ambiguous: best_delta=%s "
                "second_delta=%s margin=%s (< %s)",
                asset_id,
                best_delta,
                second_delta,
                margin,
                _CLEAR_INGEST_MARGIN,
            )
            return None
    logger.debug(
        "ingest-date SRT match asset=%s chose %s capture=%s delta=%s",
        asset_id,
        best_path,
        best_capture.isoformat(sep=" "),
        best_delta,
    )
    return best_path


# Resolve one asset's SRT using sibling → unique archive → ingestDate precedence.
def resolve_sidecar_for_asset(
    asset: ET.Element,
    archive_by_stem: dict[str, tuple[Path, ...]],
) -> SidecarMatchResult:
    archive_candidates = archive_candidates_for_asset(asset, archive_by_stem)
    archive_count = len(archive_candidates)

    sibling = sibling_srt_for_asset(asset)
    if sibling is not None:
        return SidecarMatchResult(
            path=sibling,
            method="exact_sibling",
            confidence="high",
            ambiguous=False,
            archive_candidate_count=archive_count,
        )

    if archive_count == 0:
        return SidecarMatchResult(
            path=None,
            method="missing",
            confidence=None,
            ambiguous=False,
            archive_candidate_count=0,
        )

    if archive_count == 1:
        return SidecarMatchResult(
            path=archive_candidates[0],
            method="unique_archive_stem",
            confidence="high",
            ambiguous=False,
            archive_candidate_count=1,
        )

    asset_id = asset.get("id")
    ingest_at = asset_ingest_datetime(asset)
    if ingest_at is None:
        logger.debug(
            "ingest-date SRT match asset=%s ambiguous: %s archive candidates, "
            "no com.apple.proapps.mio.ingestDate",
            asset_id,
            archive_count,
        )
        return SidecarMatchResult(
            path=None,
            method="ambiguous",
            confidence=None,
            ambiguous=True,
            archive_candidate_count=archive_count,
        )

    logger.debug(
        "ingest-date SRT match asset=%s ingestDate=%s archive_candidates=%s",
        asset_id,
        ingest_at.isoformat(sep=" "),
        archive_count,
    )
    chosen = choose_archive_srt_by_ingest_date(
        archive_candidates,
        ingest_at,
        asset_id=asset_id,
    )
    if chosen is None:
        return SidecarMatchResult(
            path=None,
            method="ambiguous",
            confidence=None,
            ambiguous=True,
            archive_candidate_count=archive_count,
        )

    return SidecarMatchResult(
        path=chosen,
        method="archive_stem_ingest_date",
        confidence="medium",
        ambiguous=False,
        archive_candidate_count=archive_count,
    )


# Find matching DJI SRT files beside media and under scan roots.
def build_sidecar_index(
    assets: Iterable[ET.Element],
    roots: Iterable[Path],
) -> SidecarIndex:
    candidate_stems: set[str] = set()
    scanned_matches: dict[str, list[Path]] = {}
    summary = SidecarSummary()

    asset_list = list(assets)
    for asset in asset_list:
        candidate_stems.update(asset_sidecar_stems(asset))

    normalized_roots: list[Path] = []
    for root in roots:
        expanded = root.expanduser().resolve()
        summary.roots.append(str(expanded))
        if expanded.exists():
            normalized_roots.append(expanded)
        else:
            summary.scan_errors.append(f"Sidecar root does not exist: {expanded}")

    for root in normalized_roots:
        for dirpath, dirnames, filenames in os.walk(root, onerror=None):
            dirnames[:] = [
                dirname
                for dirname in dirnames
                if dirname not in SKIPPED_SCAN_DIR_NAMES
                and not dirname.endswith(".fcpbundle")
            ]
            for filename in filenames:
                path = Path(dirpath) / filename
                if path.suffix.lower() not in SIDECAR_EXTENSIONS:
                    continue
                summary.srt_files_scanned += 1
                stem = normalized_stem(filename)
                if stem in candidate_stems:
                    scanned_matches.setdefault(stem, []).append(path.resolve())

    archive_by_stem: dict[str, tuple[Path, ...]] = {
        stem: tuple(sorted(set(paths), key=str))
        for stem, paths in scanned_matches.items()
    }

    by_asset_id: dict[str, SidecarMatchResult] = {}
    matched_assets = 0
    ambiguous_assets = 0
    for asset in asset_list:
        match = resolve_sidecar_for_asset(asset, archive_by_stem)
        asset_id = asset.get("id")
        if asset_id:
            by_asset_id[asset_id] = match
        if match.path is not None:
            matched_assets += 1
        if match.ambiguous or match.method == "ambiguous":
            ambiguous_assets += 1

    summary.candidate_asset_stems = len(candidate_stems)
    summary.matched_asset_stems = matched_assets
    summary.ambiguous_asset_stems = ambiguous_assets
    return SidecarIndex(
        archive_by_stem=archive_by_stem,
        summary=summary,
        by_asset_id=by_asset_id,
    )


# Return the matching SRT and a small explanation of how it was found.
def sidecar_match_for_asset(
    asset: ET.Element | None,
    sidecar_index: SidecarIndex | None,
) -> SidecarMatchResult:
    if asset is None or sidecar_index is None:
        return SidecarMatchResult()
    asset_id = asset.get("id")
    if asset_id and asset_id in sidecar_index.by_asset_id:
        return sidecar_index.by_asset_id[asset_id]
    return resolve_sidecar_for_asset(asset, sidecar_index.archive_by_stem)


# Keep the original simple lookup for clip-recovery code.
def sidecar_for_asset(asset: ET.Element | None, sidecar_index: SidecarIndex | None) -> Path | None:
    return sidecar_match_for_asset(asset, sidecar_index).path


# SRT parsing

# Convert an SRT clock group into an exact fraction of a second.
def parse_srt_clock(match: re.Match[str], start_index: int) -> Fraction:
    hours = int(match.group(start_index))
    minutes = int(match.group(start_index + 1))
    seconds = int(match.group(start_index + 2))
    millis = int(match.group(start_index + 3))
    return Fraction(
        ((hours * 3600 + minutes * 60 + seconds) * 1000) + millis,
        1000,
    )


# Read a DJI capture timestamp from cue text.
def parse_srt_payload_datetime(payload: str) -> datetime | None:
    match = SRT_DATETIME_RE.search(payload)
    if not match:
        return None
    value = f"{match.group(1)} {match.group(2)}"
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def extract_srt_color_md(path: Path, *, max_cues: int = 40) -> str | None:
    """Return the dominant literal color_md value from a DJI SRT, if present.

    Preserves source literals such as ``dlog_m`` and ``default``; does not infer
    Normal/HLG/etc. from ``default``.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    counts: dict[str, int] = {}
    cues_seen = 0
    for match in SRT_TIME_RE.finditer(text):
        cues_seen += 1
        if cues_seen > max_cues:
            break
        # Inspect the payload window after the cue clock line.
        window = text[match.end() : match.end() + 500]
        for key, value in SRT_BRACKET_FIELD_RE.findall(window):
            if key.lower() != "color_md":
                continue
            literal = value.strip()
            if not literal:
                continue
            counts[literal] = counts.get(literal, 0) + 1
    if not counts:
        # Fallback: scan whole file when cue windows missed the field.
        for key, value in SRT_BRACKET_FIELD_RE.findall(text):
            if key.lower() != "color_md":
                continue
            literal = value.strip()
            if literal:
                counts[literal] = counts.get(literal, 0) + 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


# Parse timing, GPS, altitude, and orientation availability from an SRT.
def parse_srt_info(path: Path) -> SrtInfo:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    samples: list[SrtSample] = []
    start_time: Fraction | None = None
    end_time: Fraction | None = None
    has_orientation = bool(
        re.search(r"\b(yaw|pitch|roll|gimbal|gb_yaw|gb_pitch|gb_roll)\b", text, re.I)
    )

    index = 0
    while index < len(lines):
        line = lines[index]
        match = SRT_TIME_RE.search(line)
        if not match:
            index += 1
            continue

        cue_start = parse_srt_clock(match, 1)
        cue_end = parse_srt_clock(match, 5)
        if start_time is None:
            start_time = cue_start
        end_time = cue_end

        index += 1
        payload_lines: list[str] = []
        while index < len(lines) and lines[index].strip():
            payload_lines.append(lines[index])
            index += 1

        payload = " ".join(payload_lines)
        fields = {
            key.lower(): float(value)
            for key, value in SRT_NUMERIC_FIELD_RE.findall(payload)
        }
        captured_at = parse_srt_payload_datetime(payload)
        samples.append(
            SrtSample(
                time=cue_start,
                latitude=fields.get("latitude"),
                longitude=fields.get("longitude"),
                rel_alt=fields.get("rel_alt"),
                captured_at=captured_at.isoformat(timespec="milliseconds") if captured_at else None,
            )
        )

    valid_position = any(
        is_usable_gps(sample.latitude, sample.longitude) for sample in samples
    )
    valid_altitude = any(sample.rel_alt is not None for sample in samples)
    return SrtInfo(
        path=path,
        start=start_time or Fraction(0),
        end=end_time or Fraction(0),
        sample_count=len(samples),
        samples=tuple(samples),
        has_position=valid_position,
        has_altitude=valid_altitude,
        has_orientation=has_orientation,
    )
