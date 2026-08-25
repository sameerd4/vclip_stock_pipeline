#!/usr/bin/env python3
"""Score physical VClip review candidates using DJI flight-record telemetry.

This command is intentionally report-only. It does not modify FCPXML or the
canonical VClip SQLite database. It:

1. Reads the *physical* review-shard corpus and extracts VCLIP IDs + source ranges.
2. Resolves source media on mounted volumes (preferring canonical /drone/ media).
3. Reads DJI flight-record headers and matches source videos to flights by time.
4. Decrypts only matched flight logs with pydjirecord (DJI_API_KEY for v13/v14).
5. Intersects each VClip source range with ~10 Hz gimbal/aircraft telemetry.
6. Emits objective movement features plus PASS / SOFT_REVIEW / REVIEW / NO_TELEMETRY.

Run this with the Python environment that contains pydjirecord, and set
PYTHONPATH to the VClip repo's src directory if you want the repository's
camera-scope classifier to be used.
"""

from __future__ import annotations

import argparse
import csv
import enum
import json
import math
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Sequence

# pydjirecord 1.3.0 creates dynamic UNKNOWN_<N> IntEnum pseudo-members for
# newer DJI models. Python deepcopy tries to reconstruct those by name and can
# fail (e.g. UNKNOWN_90 / UNKNOWN_120). Enum members are immutable, so returning
# the same object on deepcopy is safe and fixes frame construction.
def _enum_deepcopy(self: enum.Enum, memo: dict[int, Any]) -> enum.Enum:
    return self


enum.Enum.__deepcopy__ = _enum_deepcopy  # type: ignore[attr-defined]

try:
    from pydjirecord import DJILog
except Exception as exc:  # pragma: no cover - environment check
    DJILog = None  # type: ignore[assignment]
    PYDJIRECORD_IMPORT_ERROR = exc
else:
    PYDJIRECORD_IMPORT_ERROR = None

try:
    from vclip_pipeline.workflow.camera_scope import (
        SCOPE_OUT_OF_SCOPE_NON_DRONE,
        classify_vclip_camera_scope,
    )
except Exception:
    SCOPE_OUT_OF_SCOPE_NON_DRONE = "out_of_scope_non_drone"
    classify_vclip_camera_scope = None  # type: ignore[assignment]

VCLIP_RE = re.compile(r"VCLIP_[0-9A-F]{16,64}")
SOURCE_TIME_RE = re.compile(r"DJI_(\d{14})_", re.I)
FLIGHT_RE = re.compile(
    r"^(?:DJI)?FlightRecord_(\d{4}-\d{2}-\d{2})_\[(\d{2})-(\d{2})-(\d{2})\]\.txt$",
    re.I,
)
VIDEO_EXTS = {".mp4", ".mov", ".m4v"}
SKIP_MEDIA_DIRS = {
    ".spotlight-v100",
    ".trashes",
    "__trash",
    "render files",
    "analysis files",
    "proxy media",
    "high quality media",
    "optimized media",
    "transcoded media",
}


@dataclass
class AssetResource:
    ref: str
    name: str | None
    media_uri: str | None
    media_path: str | None
    media_start_s: float


@dataclass
class ClipAppearance:
    stock_clip_id: str
    review_root: str
    shard_path: str
    event_name: str
    project_name: str
    source_ref: str | None
    source_name: str | None
    source_start_s: float | None
    duration_s: float | None
    xml_media_path: str | None
    parse_note: str | None = None
    camera_lut: str | None = None
    db_source_ref: str | None = None
    camera_scope: str | None = None
    camera_family: str | None = None
    media_path: str | None = None
    media_resolution: str | None = None


@dataclass
class MediaProbe:
    path: str
    creation_time: str | None
    duration_s: float | None
    error: str | None = None

    @property
    def creation_dt(self) -> datetime | None:
        return parse_iso_datetime(self.creation_time)


@dataclass
class FlightHeader:
    path: str
    basename: str
    version: int | None
    start_time: str | None
    total_time_s: float | None
    aircraft_name: str | None
    product_type: str | None
    local_filename_time: str | None
    size: int
    mtime_ns: int
    duplicate_count: int = 1
    duplicate_conflict: bool = False
    error: str | None = None

    @property
    def start_dt(self) -> datetime | None:
        return parse_iso_datetime(self.start_time)

    @property
    def local_dt(self) -> datetime | None:
        return parse_iso_datetime(self.local_filename_time)


@dataclass
class FlightSamples:
    path: str
    version: int | None
    aircraft_name: str | None
    timestamps: list[datetime]
    pitch: list[float]
    roll: list[float]
    gimbal_yaw: list[float]
    aircraft_yaw: list[float]
    h_speed: list[float]
    z_speed: list[float]
    pitch_limit: list[bool]
    roll_limit: list[bool]
    yaw_limit: list[bool]
    stuck: list[bool]


@dataclass
class WindowMetric:
    magnitude: float | None = None
    signed_delta: float | None = None
    start_s: float | None = None
    end_s: float | None = None


@dataclass
class ClipScore:
    stock_clip_id: str
    status: str
    reasons: list[str]
    event_name: str
    project_name: str
    shard_path: str
    source_name: str | None
    source_start_s: float | None
    duration_s: float | None
    camera_scope: str | None
    camera_family: str | None
    media_path: str | None
    media_resolution: str | None
    media_creation_time: str | None
    flight_log: str | None
    flight_version: int | None
    aircraft_name: str | None
    alignment_method: str | None
    telemetry_samples: int = 0
    telemetry_coverage_pct: float | None = None
    telemetry_rate_hz: float | None = None
    pitch_start_deg: float | None = None
    pitch_end_deg: float | None = None
    net_pitch_deg: float | None = None
    pitch_span_deg: float | None = None
    max_pitch_velocity_deg_s: float | None = None
    p95_pitch_velocity_deg_s: float | None = None
    max_pitch_accel_deg_s2: float | None = None
    max_pitch_delta_1s: WindowMetric = field(default_factory=WindowMetric)
    max_pitch_delta_2s: WindowMetric = field(default_factory=WindowMetric)
    max_pitch_delta_3s: WindowMetric = field(default_factory=WindowMetric)
    relative_yaw_start_deg: float | None = None
    relative_yaw_end_deg: float | None = None
    net_relative_yaw_deg: float | None = None
    relative_yaw_span_deg: float | None = None
    max_relative_yaw_delta_1s: WindowMetric = field(default_factory=WindowMetric)
    max_relative_yaw_delta_2s: WindowMetric = field(default_factory=WindowMetric)
    max_relative_yaw_delta_3s: WindowMetric = field(default_factory=WindowMetric)
    aircraft_yaw_span_deg: float | None = None
    h_speed_min_m_s: float | None = None
    h_speed_max_m_s: float | None = None
    max_abs_z_speed_m_s: float | None = None
    gimbal_pitch_limit: bool = False
    gimbal_roll_limit: bool = False
    gimbal_yaw_limit: bool = False
    gimbal_stuck: bool = False
    issue_start_s: float | None = None
    issue_end_s: float | None = None
    suggested_action: str | None = None
    suggested_trim_start_s: float | None = None
    suggested_trim_end_s: float | None = None
    error: str | None = None


@dataclass
class Thresholds:
    hard_net_pitch_deg: float = 10.0
    hard_pitch_span_deg: float = 12.0
    hard_pitch_delta_1s_deg: float = 6.5
    hard_pitch_delta_2s_deg: float = 8.0
    hard_pitch_delta_3s_deg: float = 10.0
    soft_net_pitch_deg: float = 7.0
    soft_pitch_span_deg: float = 8.0
    soft_pitch_delta_2s_deg: float = 6.0
    soft_pitch_delta_3s_deg: float = 7.0
    hard_relative_yaw_delta_1s_deg: float = 25.0
    hard_relative_yaw_delta_2s_deg: float = 35.0
    soft_relative_yaw_delta_1s_deg: float = 15.0
    soft_relative_yaw_delta_2s_deg: float = 25.0
    min_coverage_pct: float = 70.0
    min_clean_seconds: float = 5.0


def parse_fraction_seconds(value: str | None, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    raw = value[:-1] if value.endswith("s") else value
    try:
        return float(Fraction(raw))
    except (ValueError, ZeroDivisionError):
        return default


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def file_uri_to_path(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith("path:"):
        return value[5:]
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme == "file":
        return urllib.parse.unquote(parsed.path)
    if parsed.scheme:
        return None
    return urllib.parse.unquote(value)


def find_media_uri(asset: ET.Element) -> str | None:
    direct = asset.get("src")
    if direct:
        return direct
    reps = list(asset.iter("media-rep"))
    for rep in reps:
        if rep.get("kind") == "original-media" and rep.get("src"):
            return rep.get("src")
    for rep in reps:
        if rep.get("src"):
            return rep.get("src")
    return None


def parse_review_root(root: Path) -> tuple[list[ClipAppearance], list[dict[str, Any]]]:
    appearances: list[ClipAppearance] = []
    issues: list[dict[str, Any]] = []

    for shard in sorted(root.rglob("*.fcpxml")):
        try:
            tree = ET.parse(shard)
        except Exception as exc:
            issues.append({"type": "xml_parse_error", "shard": str(shard), "error": str(exc)})
            continue

        doc = tree.getroot()
        assets: dict[str, AssetResource] = {}
        for asset in doc.iter("asset"):
            ref = asset.get("id")
            if not ref:
                continue
            uri = find_media_uri(asset)
            assets[ref] = AssetResource(
                ref=ref,
                name=asset.get("name"),
                media_uri=uri,
                media_path=file_uri_to_path(uri),
                media_start_s=parse_fraction_seconds(asset.get("start"), 0.0) or 0.0,
            )

        for event in doc.iter("event"):
            event_name = event.get("name", "")
            for project in event.iter("project"):
                project_name = project.get("name", "")
                blob = ET.tostring(project, encoding="unicode")
                ids = sorted(set(VCLIP_RE.findall(blob)))
                if not ids:
                    continue

                media_elems: list[ET.Element] = []
                for elem in project.iter():
                    if elem.tag in {"asset-clip", "clip", "video", "ref-clip", "sync-clip"} and elem.get("ref"):
                        media_elems.append(elem)

                # Generated review projects should have one source-bearing clip. Prefer an
                # asset-clip, then any element whose ref is an asset resource.
                candidates = [e for e in media_elems if e.tag == "asset-clip"]
                if not candidates:
                    candidates = [e for e in media_elems if e.get("ref") in assets]

                if not candidates:
                    for stock_id in ids:
                        appearances.append(
                            ClipAppearance(
                                stock_clip_id=stock_id,
                                review_root=str(root),
                                shard_path=str(shard),
                                event_name=event_name,
                                project_name=project_name,
                                source_ref=None,
                                source_name=None,
                                source_start_s=None,
                                duration_s=parse_fraction_seconds(project.get("duration")),
                                xml_media_path=None,
                                parse_note="no_source_clip_element",
                            )
                        )
                    continue

                source_elem = candidates[0]
                ref = source_elem.get("ref")
                asset = assets.get(ref or "")
                absolute_start = parse_fraction_seconds(source_elem.get("start"), 0.0)
                asset_start = asset.media_start_s if asset else 0.0
                relative_start = None
                if absolute_start is not None:
                    relative_start = absolute_start - asset_start
                    if relative_start < -0.001:
                        relative_start = absolute_start

                note = None
                if len(candidates) > 1:
                    note = f"multiple_source_clip_elements:{len(candidates)}"
                if len(ids) > 1:
                    note = (note + ";" if note else "") + f"multiple_vclip_ids:{len(ids)}"

                for stock_id in ids:
                    appearances.append(
                        ClipAppearance(
                            stock_clip_id=stock_id,
                            review_root=str(root),
                            shard_path=str(shard),
                            event_name=event_name,
                            project_name=project_name,
                            source_ref=ref,
                            source_name=source_elem.get("name") or (asset.name if asset else None),
                            source_start_s=relative_start,
                            duration_s=parse_fraction_seconds(source_elem.get("duration")),
                            xml_media_path=asset.media_path if asset else None,
                            parse_note=note,
                        )
                    )

    return appearances, issues


def dedupe_physical_appearances(
    appearances: Sequence[ClipAppearance],
) -> tuple[list[ClipAppearance], list[dict[str, Any]]]:
    by_id: dict[str, list[ClipAppearance]] = defaultdict(list)
    for appearance in appearances:
        by_id[appearance.stock_clip_id].append(appearance)

    selected: list[ClipAppearance] = []
    duplicates: list[dict[str, Any]] = []
    for stock_id, rows in sorted(by_id.items()):
        if len(rows) > 1:
            duplicates.append(
                {
                    "stock_clip_id": stock_id,
                    "count": len(rows),
                    "appearances": [
                        {
                            "shard": r.shard_path,
                            "event": r.event_name,
                            "project": r.project_name,
                            "source_name": r.source_name,
                            "source_start_s": r.source_start_s,
                            "duration_s": r.duration_s,
                        }
                        for r in rows
                    ],
                }
            )
        # Deterministic: prefer a fully parsed row, then shortest path.
        rows_sorted = sorted(
            rows,
            key=lambda r: (
                r.source_start_s is None,
                r.duration_s is None,
                r.source_name is None,
                len(r.shard_path),
                r.shard_path,
            ),
        )
        selected.append(rows_sorted[0])
    return selected, duplicates


def enrich_from_db(db_path: Path, clips: Sequence[ClipAppearance]) -> dict[str, dict[str, Any]]:
    ids = [clip.stock_clip_id for clip in clips]
    if not ids:
        return {}

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TEMP TABLE active_ids (stock_clip_id TEXT PRIMARY KEY)")
    con.executemany("INSERT OR IGNORE INTO active_ids VALUES (?)", [(x,) for x in ids])
    rows = con.execute(
        """
        WITH ranked AS (
            SELECT sc.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY sc.stock_clip_id
                       ORDER BY COALESCE(sc.updated_at, sc.created_at, '') DESC, sc.rowid DESC
                   ) AS rn
            FROM stock_candidates sc
            JOIN active_ids a USING (stock_clip_id)
            WHERE sc.eligibility_status = 'accepted'
        )
        SELECT stock_clip_id, source_name, source_ref, camera_lut,
               generated_event_name, generated_project_label
        FROM ranked
        WHERE rn = 1
        """
    ).fetchall()
    con.close()
    return {row["stock_clip_id"]: dict(row) for row in rows}


def classify_scope(clip: ClipAppearance, db_row: dict[str, Any] | None) -> tuple[str, str]:
    db_row = db_row or {}
    lut = db_row.get("camera_lut")
    clip.camera_lut = lut
    clip.db_source_ref = db_row.get("source_ref")

    if classify_vclip_camera_scope is not None:
        result = classify_vclip_camera_scope(
            source_basename=clip.source_name or db_row.get("source_name"),
            media_path=clip.xml_media_path or file_uri_to_path(db_row.get("source_ref")),
            camera_lut=lut,
            source_event_name=clip.event_name,
            source_project_name=clip.project_name,
            extra_texts=[db_row.get("generated_event_name"), db_row.get("generated_project_label")],
        )
        return str(result.get("camera_scope") or "unknown_camera_family"), str(
            result.get("camera_family") or "unknown"
        )

    blob = "\n".join(
        str(x)
        for x in [clip.source_name, clip.xml_media_path, lut, clip.event_name, clip.project_name]
        if x
    ).casefold()
    if "pocket" in blob or "osmo action" in blob or "iphone" in blob:
        return SCOPE_OUT_OF_SCOPE_NON_DRONE, "non_drone"
    if "dji" in blob or "/drone/" in blob:
        return "drone", "dji_drone"
    return "unknown_camera_family", "unknown"


def normalize_source_stem(name: str | None) -> str | None:
    if not name:
        return None
    base = Path(name).name
    suffix = Path(base).suffix.casefold()
    if suffix in VIDEO_EXTS:
        base = Path(base).stem
    return base.casefold()


def media_path_score(path: Path) -> int:
    text = str(path).casefold()
    score = 0
    if "/drone/" in text:
        score += 100
    if "/original media/" in text:
        score += 30
    if ".fcpbundle/" in text:
        score += 5
    if "/__trash/" in text or "/.trashes/" in text:
        score -= 200
    if "proxy" in text or "transcoded" in text or "optimized" in text:
        score -= 100
    srt = path.with_suffix(".SRT")
    if srt.exists():
        score += 20
    return score


def build_media_index(roots: Sequence[Path], wanted_stems: set[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    if not wanted_stems:
        return result

    print(f"Indexing mounted media for {len(wanted_stems)} unresolved source stem(s)...", flush=True)
    scanned = 0
    for root in roots:
        if not root.exists():
            print(f"  media root not mounted: {root}", flush=True)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.casefold() not in SKIP_MEDIA_DIRS]
            for filename in filenames:
                if Path(filename).suffix.casefold() not in VIDEO_EXTS:
                    continue
                scanned += 1
                stem = Path(filename).stem.casefold()
                if stem in wanted_stems:
                    candidate = Path(dirpath) / filename
                    try:
                        if candidate.is_file():
                            result[stem].append(str(candidate))
                    except OSError:
                        pass
    print(f"  scanned {scanned:,} video file(s); matched {len(result):,} stem(s)", flush=True)
    return result


def resolve_media_paths(clips: Sequence[ClipAppearance], media_roots: Sequence[Path]) -> None:
    unresolved: list[ClipAppearance] = []
    for clip in clips:
        direct_candidates: list[tuple[str, str]] = []
        for value, kind in [
            (clip.xml_media_path, "fcpxml_source"),
            (file_uri_to_path(clip.db_source_ref), "db_source_ref"),
        ]:
            if value and Path(value).is_file():
                direct_candidates.append((value, kind))
        if direct_candidates:
            path, kind = max(direct_candidates, key=lambda item: media_path_score(Path(item[0])))
            clip.media_path = path
            clip.media_resolution = kind
        else:
            unresolved.append(clip)

    wanted = {stem for clip in unresolved if (stem := normalize_source_stem(clip.source_name))}
    index = build_media_index(media_roots, wanted)
    for clip in unresolved:
        stem = normalize_source_stem(clip.source_name)
        matches = index.get(stem or "", [])
        ranked_matches = []
        for value in matches:
            path = Path(value)
            try:
                if not path.is_file():
                    continue
                ranked_matches.append(
                    (media_path_score(path), path.stat().st_size, str(path))
                )
            except OSError:
                # Ignore stale/broken FCP managed-media references and files
                # disappearing during a mounted-volume scan.
                continue
        if ranked_matches:
            clip.media_path = max(ranked_matches)[2]
            clip.media_resolution = "media_index"
        else:
            clip.media_resolution = "missing"


def probe_cache_key(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return str(path)
    return f"{path}|{st.st_size}|{st.st_mtime_ns}"


def load_json_file(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    tmp.replace(path)


def ffprobe_media(path: Path, ffprobe: str, cache: dict[str, Any]) -> MediaProbe:
    key = probe_cache_key(path)
    cached = cache.get(key)
    if isinstance(cached, dict):
        return MediaProbe(**cached)

    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration:format_tags=creation_time:stream=index,codec_type:stream_tags=creation_time",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=45)
    except Exception as exc:
        result = MediaProbe(str(path), None, None, f"ffprobe_error:{exc}")
        cache[key] = asdict(result)
        return result

    if proc.returncode != 0:
        result = MediaProbe(str(path), None, None, f"ffprobe_exit_{proc.returncode}:{proc.stderr[-500:]}")
        cache[key] = asdict(result)
        return result

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        result = MediaProbe(str(path), None, None, f"ffprobe_json:{exc}")
        cache[key] = asdict(result)
        return result

    format_obj = data.get("format") or {}
    creation = (format_obj.get("tags") or {}).get("creation_time")
    if not creation:
        for stream in data.get("streams") or []:
            creation = (stream.get("tags") or {}).get("creation_time")
            if creation:
                break
    try:
        duration = float(format_obj.get("duration"))
    except (TypeError, ValueError):
        duration = None
    result = MediaProbe(str(path), creation, duration, None)
    cache[key] = asdict(result)
    return result


def parse_flight_filename(path: Path) -> datetime | None:
    match = FLIGHT_RE.match(path.name)
    if not match:
        return None
    date, hh, mm, ss = match.groups()
    try:
        return datetime.strptime(f"{date} {hh}:{mm}:{ss}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def source_local_datetime(name: str | None) -> datetime | None:
    if not name:
        return None
    match = SOURCE_TIME_RE.search(name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def flight_header_cache_key(path: Path) -> str:
    st = path.stat()
    return f"{path}|{st.st_size}|{st.st_mtime_ns}"


def enumerate_flight_logs(root: Path) -> tuple[list[Path], dict[str, Any]]:
    by_local_key: dict[str, list[Path]] = defaultdict(list)
    unparsable = 0
    for path in root.rglob("*.txt"):
        local = parse_flight_filename(path)
        if local is None:
            unparsable += 1
            continue
        by_local_key[local.isoformat()].append(path)

    chosen: list[Path] = []
    duplicate_groups = 0
    conflict_groups = 0
    for paths in by_local_key.values():
        if len(paths) > 1:
            duplicate_groups += 1
            sizes = {p.stat().st_size for p in paths}
            if len(sizes) > 1:
                conflict_groups += 1
        chosen.append(max(paths, key=lambda p: (p.stat().st_size, p.stat().st_mtime_ns, str(p))))

    chosen.sort(key=lambda p: parse_flight_filename(p) or datetime.min)
    stats = {
        "txt_files_parseable": sum(len(v) for v in by_local_key.values()),
        "unique_flight_start_times": len(chosen),
        "duplicate_start_time_groups": duplicate_groups,
        "duplicate_size_conflict_groups": conflict_groups,
        "unparseable_txt_filenames": unparsable,
    }
    return chosen, stats


def read_flight_headers(
    paths: Sequence[Path], cache_path: Path
) -> tuple[list[FlightHeader], dict[str, Any]]:
    if DJILog is None:
        raise RuntimeError(f"pydjirecord import failed: {PYDJIRECORD_IMPORT_ERROR}")

    cache: dict[str, Any] = load_json_file(cache_path, {})
    headers: list[FlightHeader] = []
    reused = 0
    parsed = 0

    local_counts = Counter((parse_flight_filename(p) or datetime.min).isoformat() for p in paths)
    for idx, path in enumerate(paths, 1):
        key = flight_header_cache_key(path)
        cached = cache.get(key)
        if isinstance(cached, dict):
            try:
                headers.append(FlightHeader(**cached))
                reused += 1
                continue
            except TypeError:
                pass

        try:
            data = path.read_bytes()
            log = DJILog.from_bytes(data)
            details = log.details
            start = getattr(details, "start_time", None)
            total_time = safe_float(getattr(details, "total_time", None))
            aircraft = getattr(details, "aircraft_name", None)
            product = getattr(details, "product_type", None)
            header = FlightHeader(
                path=str(path),
                basename=path.name,
                version=int(log.version) if log.version is not None else None,
                start_time=iso(start if isinstance(start, datetime) else None),
                total_time_s=total_time,
                aircraft_name=str(aircraft) if aircraft is not None else None,
                product_type=str(product) if product is not None else None,
                local_filename_time=(parse_flight_filename(path) or datetime.min).isoformat(),
                size=path.stat().st_size,
                mtime_ns=path.stat().st_mtime_ns,
                duplicate_count=local_counts[(parse_flight_filename(path) or datetime.min).isoformat()],
                duplicate_conflict=False,
                error=None,
            )
        except Exception as exc:
            header = FlightHeader(
                path=str(path),
                basename=path.name,
                version=None,
                start_time=None,
                total_time_s=None,
                aircraft_name=None,
                product_type=None,
                local_filename_time=(parse_flight_filename(path) or datetime.min).isoformat(),
                size=path.stat().st_size,
                mtime_ns=path.stat().st_mtime_ns,
                error=f"{type(exc).__name__}:{exc}",
            )
        cache[key] = asdict(header)
        headers.append(header)
        parsed += 1
        if idx % 100 == 0:
            print(f"  flight headers {idx}/{len(paths)}", flush=True)
            atomic_write_json(cache_path, cache)

    atomic_write_json(cache_path, cache)
    return headers, {"header_cache_reused": reused, "headers_parsed_now": parsed}


def safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def candidate_flights(
    probe: MediaProbe,
    clip: ClipAppearance,
    headers: Sequence[FlightHeader],
    grace_before_s: float = 20.0,
    grace_after_s: float = 180.0,
) -> list[tuple[float, FlightHeader, str, datetime]]:
    creation = probe.creation_dt
    candidates: list[tuple[float, FlightHeader, str, datetime]] = []

    if creation is not None:
        for header in headers:
            start = header.start_dt
            duration = header.total_time_s
            if start is None or duration is None or duration <= 0:
                continue
            delta = (creation - start).total_seconds()
            if -grace_before_s <= delta <= duration + grace_after_s:
                # Prefer source start within the actual header duration, then closest prior start.
                penalty = abs(min(delta, 0.0)) * 1000 + abs(max(delta - duration, 0.0)) * 1000
                score = penalty + abs(delta)
                candidates.append((score, header, "media_creation_time", creation))

    if candidates:
        return sorted(candidates, key=lambda item: (item[0], item[1].path))

    source_local = source_local_datetime(clip.source_name or (Path(probe.path).name if probe.path else None))
    if source_local is not None:
        for header in headers:
            local_start = header.local_dt
            duration = header.total_time_s
            start_utc = header.start_dt
            if local_start is None or duration is None or start_utc is None:
                continue
            delta = (source_local - local_start.replace(tzinfo=None)).total_seconds()
            if -grace_before_s <= delta <= duration + grace_after_s:
                approx_creation = start_utc + timedelta(seconds=delta)
                penalty = abs(min(delta, 0.0)) * 1000 + abs(max(delta - duration, 0.0)) * 1000
                score = penalty + abs(delta)
                candidates.append((score, header, "filename_log_delta", approx_creation))

    return sorted(candidates, key=lambda item: (item[0], item[1].path))


def decode_flight(header: FlightHeader, api_key: str | None) -> FlightSamples:
    if DJILog is None:
        raise RuntimeError(f"pydjirecord import failed: {PYDJIRECORD_IMPORT_ERROR}")
    path = Path(header.path)
    log = DJILog.from_bytes(path.read_bytes())
    if int(log.version) >= 13:
        if not api_key:
            raise RuntimeError("DJI_API_KEY is required for v13/v14 flight logs")
        keychains = log.fetch_keychains(api_key)
    else:
        keychains = None
    frames = log.frames(keychains)

    timestamps: list[datetime] = []
    pitch: list[float] = []
    roll: list[float] = []
    gimbal_yaw: list[float] = []
    aircraft_yaw: list[float] = []
    h_speed: list[float] = []
    z_speed: list[float] = []
    pitch_limit: list[bool] = []
    roll_limit: list[bool] = []
    yaw_limit: list[bool] = []
    stuck: list[bool] = []

    for frame in frames:
        dt = getattr(frame.custom, "date_time", None)
        if not isinstance(dt, datetime) or dt.year < 2000:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        timestamps.append(dt)
        pitch.append(float(frame.gimbal.pitch))
        roll.append(float(frame.gimbal.roll))
        gimbal_yaw.append(float(frame.gimbal.yaw))
        aircraft_yaw.append(float(frame.osd.yaw))
        h_speed.append(float(frame.osd.h_speed))
        z_speed.append(float(frame.osd.z_speed))
        pitch_limit.append(bool(frame.gimbal.is_pitch_at_limit))
        roll_limit.append(bool(frame.gimbal.is_roll_at_limit))
        yaw_limit.append(bool(frame.gimbal.is_yaw_at_limit))
        stuck.append(bool(frame.gimbal.is_stuck))

    return FlightSamples(
        path=header.path,
        version=header.version,
        aircraft_name=header.aircraft_name,
        timestamps=timestamps,
        pitch=pitch,
        roll=roll,
        gimbal_yaw=gimbal_yaw,
        aircraft_yaw=aircraft_yaw,
        h_speed=h_speed,
        z_speed=z_speed,
        pitch_limit=pitch_limit,
        roll_limit=roll_limit,
        yaw_limit=yaw_limit,
        stuck=stuck,
    )


def wrap180(value: float) -> float:
    return ((value + 180.0) % 360.0) - 180.0


def unwrap_degrees(values: Sequence[float]) -> list[float]:
    if not values:
        return []
    out = [values[0]]
    for value in values[1:]:
        out.append(out[-1] + wrap180(value - out[-1]))
    return out


def percentile(values: Sequence[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * pct
    lo = math.floor(position)
    hi = math.ceil(position)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - position) + ordered[hi] * (position - lo)


def median_edge(times: Sequence[float], values: Sequence[float], beginning: bool, width_s: float = 0.4) -> float | None:
    if not times or not values:
        return None
    if beginning:
        edge = times[0] + width_s
        subset = [v for t, v in zip(times, values) if t <= edge]
    else:
        edge = times[-1] - width_s
        subset = [v for t, v in zip(times, values) if t >= edge]
    return statistics.median(subset) if subset else None


def max_window_change(times: Sequence[float], values: Sequence[float], window_s: float) -> WindowMetric:
    best = WindowMetric()
    for i, t0 in enumerate(times):
        target = t0 + window_s
        j = bisect_left(times, target)
        if j >= len(times):
            continue
        if abs(times[j] - target) > max(0.25, window_s * 0.12):
            continue
        delta = values[j] - values[i]
        magnitude = abs(delta)
        if best.magnitude is None or magnitude > best.magnitude:
            best = WindowMetric(
                magnitude=magnitude,
                signed_delta=delta,
                start_s=t0,
                end_s=times[j],
            )
    return best


def adjacent_velocity(times: Sequence[float], values: Sequence[float]) -> tuple[list[tuple[float, float]], float | None, float | None]:
    rows: list[tuple[float, float]] = []
    for i in range(1, len(times)):
        dt = times[i] - times[i - 1]
        if dt <= 0 or dt > 0.5:
            continue
        velocity = (values[i] - values[i - 1]) / dt
        rows.append((times[i], velocity))
    abs_values = [abs(v) for _, v in rows]
    return rows, (max(abs_values) if abs_values else None), percentile(abs_values, 0.95)


def max_acceleration(velocities: Sequence[tuple[float, float]]) -> float | None:
    result: list[float] = []
    for i in range(1, len(velocities)):
        t0, v0 = velocities[i - 1]
        t1, v1 = velocities[i]
        dt = t1 - t0
        if dt <= 0 or dt > 0.5:
            continue
        result.append(abs((v1 - v0) / dt))
    return max(result) if result else None


def score_from_samples(
    clip: ClipAppearance,
    probe: MediaProbe,
    header: FlightHeader,
    alignment_method: str,
    creation_dt: datetime,
    samples: FlightSamples,
    thresholds: Thresholds,
) -> ClipScore:
    assert clip.source_start_s is not None
    assert clip.duration_s is not None

    clip_abs_start = creation_dt + timedelta(seconds=clip.source_start_s)
    clip_abs_end = clip_abs_start + timedelta(seconds=clip.duration_s)
    left = bisect_left(samples.timestamps, clip_abs_start)
    right = bisect_left(samples.timestamps, clip_abs_end)
    indices = list(range(left, min(right + 1, len(samples.timestamps))))
    indices = [i for i in indices if clip_abs_start <= samples.timestamps[i] <= clip_abs_end]

    score = ClipScore(
        stock_clip_id=clip.stock_clip_id,
        status="NO_TELEMETRY",
        reasons=[],
        event_name=clip.event_name,
        project_name=clip.project_name,
        shard_path=clip.shard_path,
        source_name=clip.source_name,
        source_start_s=clip.source_start_s,
        duration_s=clip.duration_s,
        camera_scope=clip.camera_scope,
        camera_family=clip.camera_family,
        media_path=clip.media_path,
        media_resolution=clip.media_resolution,
        media_creation_time=probe.creation_time or iso(creation_dt),
        flight_log=header.path,
        flight_version=header.version,
        aircraft_name=header.aircraft_name,
        alignment_method=alignment_method,
    )

    if len(indices) < 2:
        score.reasons = ["no_frame_overlap"]
        return score

    rel_times = [(samples.timestamps[i] - clip_abs_start).total_seconds() for i in indices]
    pitch = [samples.pitch[i] for i in indices]
    gimbal_yaw = unwrap_degrees([samples.gimbal_yaw[i] for i in indices])
    aircraft_yaw = unwrap_degrees([samples.aircraft_yaw[i] for i in indices])
    relative_yaw = unwrap_degrees([wrap180(g - a) for g, a in zip(gimbal_yaw, aircraft_yaw)])
    h_speed = [samples.h_speed[i] for i in indices]
    z_speed = [samples.z_speed[i] for i in indices]

    score.telemetry_samples = len(indices)
    span = max(0.0, rel_times[-1] - rel_times[0])
    score.telemetry_coverage_pct = min(100.0, 100.0 * span / clip.duration_s) if clip.duration_s > 0 else None
    score.telemetry_rate_hz = (len(indices) - 1) / span if span > 0 else None

    p_start = median_edge(rel_times, pitch, True)
    p_end = median_edge(rel_times, pitch, False)
    score.pitch_start_deg = p_start
    score.pitch_end_deg = p_end
    score.net_pitch_deg = (p_end - p_start) if p_start is not None and p_end is not None else None
    score.pitch_span_deg = max(pitch) - min(pitch) if pitch else None
    score.max_pitch_delta_1s = max_window_change(rel_times, pitch, 1.0)
    score.max_pitch_delta_2s = max_window_change(rel_times, pitch, 2.0)
    score.max_pitch_delta_3s = max_window_change(rel_times, pitch, 3.0)
    pitch_velocities, score.max_pitch_velocity_deg_s, score.p95_pitch_velocity_deg_s = adjacent_velocity(
        rel_times, pitch
    )
    score.max_pitch_accel_deg_s2 = max_acceleration(pitch_velocities)

    ry_start = median_edge(rel_times, relative_yaw, True)
    ry_end = median_edge(rel_times, relative_yaw, False)
    score.relative_yaw_start_deg = ry_start
    score.relative_yaw_end_deg = ry_end
    score.net_relative_yaw_deg = (ry_end - ry_start) if ry_start is not None and ry_end is not None else None
    score.relative_yaw_span_deg = max(relative_yaw) - min(relative_yaw) if relative_yaw else None
    score.max_relative_yaw_delta_1s = max_window_change(rel_times, relative_yaw, 1.0)
    score.max_relative_yaw_delta_2s = max_window_change(rel_times, relative_yaw, 2.0)
    score.max_relative_yaw_delta_3s = max_window_change(rel_times, relative_yaw, 3.0)
    score.aircraft_yaw_span_deg = max(aircraft_yaw) - min(aircraft_yaw) if aircraft_yaw else None
    score.h_speed_min_m_s = min(h_speed) if h_speed else None
    score.h_speed_max_m_s = max(h_speed) if h_speed else None
    score.max_abs_z_speed_m_s = max(abs(v) for v in z_speed) if z_speed else None
    score.gimbal_pitch_limit = any(samples.pitch_limit[i] for i in indices)
    score.gimbal_roll_limit = any(samples.roll_limit[i] for i in indices)
    score.gimbal_yaw_limit = any(samples.yaw_limit[i] for i in indices)
    score.gimbal_stuck = any(samples.stuck[i] for i in indices)

    coverage = score.telemetry_coverage_pct or 0.0
    if coverage < thresholds.min_coverage_pct:
        score.status = "NO_TELEMETRY"
        score.reasons = [f"coverage_below_{thresholds.min_coverage_pct:g}_pct"]
        return score

    hard: list[str] = []
    soft: list[str] = []

    if score.gimbal_stuck:
        hard.append("gimbal_stuck")
    if score.net_pitch_deg is not None and abs(score.net_pitch_deg) >= thresholds.hard_net_pitch_deg:
        hard.append("large_net_pitch_change")
    elif score.net_pitch_deg is not None and abs(score.net_pitch_deg) >= thresholds.soft_net_pitch_deg:
        soft.append("moderate_net_pitch_change")

    if score.pitch_span_deg is not None and score.pitch_span_deg >= thresholds.hard_pitch_span_deg:
        hard.append("large_pitch_span")
    elif score.pitch_span_deg is not None and score.pitch_span_deg >= thresholds.soft_pitch_span_deg:
        soft.append("moderate_pitch_span")

    p1 = score.max_pitch_delta_1s.magnitude or 0.0
    p2 = score.max_pitch_delta_2s.magnitude or 0.0
    p3 = score.max_pitch_delta_3s.magnitude or 0.0
    if p1 >= thresholds.hard_pitch_delta_1s_deg:
        hard.append("large_pitch_change_1s")
    if p2 >= thresholds.hard_pitch_delta_2s_deg:
        hard.append("large_pitch_change_2s")
    elif p2 >= thresholds.soft_pitch_delta_2s_deg:
        soft.append("moderate_pitch_change_2s")
    if p3 >= thresholds.hard_pitch_delta_3s_deg:
        hard.append("large_pitch_change_3s")
    elif p3 >= thresholds.soft_pitch_delta_3s_deg:
        soft.append("moderate_pitch_change_3s")

    ry1 = score.max_relative_yaw_delta_1s.magnitude or 0.0
    ry2 = score.max_relative_yaw_delta_2s.magnitude or 0.0
    if ry1 >= thresholds.hard_relative_yaw_delta_1s_deg:
        hard.append("abrupt_relative_yaw_1s")
    elif ry1 >= thresholds.soft_relative_yaw_delta_1s_deg:
        soft.append("moderate_relative_yaw_1s")
    if ry2 >= thresholds.hard_relative_yaw_delta_2s_deg:
        hard.append("large_relative_yaw_2s")
    elif ry2 >= thresholds.soft_relative_yaw_delta_2s_deg:
        soft.append("moderate_relative_yaw_2s")

    if hard:
        score.status = "REVIEW"
        score.reasons = sorted(set(hard + soft))
    elif soft:
        score.status = "SOFT_REVIEW"
        score.reasons = sorted(set(soft))
    else:
        score.status = "PASS"
        score.reasons = []

    choose_edit_suggestion(score, thresholds)
    return score


def choose_edit_suggestion(score: ClipScore, thresholds: Thresholds) -> None:
    if score.status not in {"REVIEW", "SOFT_REVIEW"} or not score.duration_s:
        return

    candidates: list[WindowMetric] = []
    for metric, cutoff in [
        (score.max_pitch_delta_3s, thresholds.soft_pitch_delta_3s_deg),
        (score.max_pitch_delta_2s, thresholds.soft_pitch_delta_2s_deg),
        (score.max_pitch_delta_1s, thresholds.hard_pitch_delta_1s_deg),
    ]:
        if metric.magnitude is not None and metric.magnitude >= cutoff and metric.start_s is not None:
            candidates.append(metric)

    if not candidates and score.net_pitch_deg is not None and abs(score.net_pitch_deg) >= thresholds.soft_net_pitch_deg:
        # Use the strongest available pitch window as a localization hint.
        candidates = [
            m
            for m in [score.max_pitch_delta_3s, score.max_pitch_delta_2s, score.max_pitch_delta_1s]
            if m.magnitude is not None and m.start_s is not None
        ]

    if not candidates:
        score.suggested_action = "manual_review"
        return

    strongest = max(candidates, key=lambda m: (m.magnitude or 0.0, -(m.start_s or 0.0)))
    issue_start = strongest.start_s
    issue_end = strongest.end_s
    score.issue_start_s = issue_start
    score.issue_end_s = issue_end
    if issue_start is None or issue_end is None:
        score.suggested_action = "manual_review"
        return

    pad = 0.15
    clean_before = max(0.0, issue_start - pad)
    clean_after_start = min(score.duration_s, issue_end + pad)
    clean_after = max(0.0, score.duration_s - clean_after_start)

    before_ok = clean_before >= thresholds.min_clean_seconds
    after_ok = clean_after >= thresholds.min_clean_seconds
    if before_ok and after_ok:
        score.suggested_action = "split_or_review"
        score.suggested_trim_end_s = round(clean_before, 3)
        score.suggested_trim_start_s = round(clean_after_start, 3)
    elif before_ok:
        score.suggested_action = "trim_end"
        score.suggested_trim_end_s = round(clean_before, 3)
    elif after_ok:
        score.suggested_action = "trim_start"
        score.suggested_trim_start_s = round(clean_after_start, 3)
    else:
        score.suggested_action = "manual_review_or_reject"


def score_without_telemetry(clip: ClipAppearance, status: str, reason: str, probe: MediaProbe | None = None) -> ClipScore:
    return ClipScore(
        stock_clip_id=clip.stock_clip_id,
        status=status,
        reasons=[reason],
        event_name=clip.event_name,
        project_name=clip.project_name,
        shard_path=clip.shard_path,
        source_name=clip.source_name,
        source_start_s=clip.source_start_s,
        duration_s=clip.duration_s,
        camera_scope=clip.camera_scope,
        camera_family=clip.camera_family,
        media_path=clip.media_path,
        media_resolution=clip.media_resolution,
        media_creation_time=probe.creation_time if probe else None,
        flight_log=None,
        flight_version=None,
        aircraft_name=None,
        alignment_method=None,
        error=probe.error if probe else None,
    )


def flatten_score(score: ClipScore) -> dict[str, Any]:
    row = asdict(score)
    row["reasons"] = ";".join(score.reasons)
    for key in list(row):
        value = row[key]
        if isinstance(value, dict):
            prefix = key
            row.pop(key)
            for subkey, subvalue in value.items():
                row[f"{prefix}_{subkey}"] = subvalue
    return row


def write_scores_csv(path: Path, scores: Sequence[ClipScore]) -> None:
    rows = [flatten_score(score) for score in scores]
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                fields.append(key)
                seen.add(key)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_summary(path: Path, summary: dict[str, Any], scores: Sequence[ClipScore]) -> None:
    status_counts = Counter(score.status for score in scores)
    reason_counts = Counter(reason for score in scores for reason in score.reasons)
    lines = [
        "VClip DJI telemetry QC",
        "======================",
        "",
    ]
    for key, value in summary.items():
        if isinstance(value, (dict, list)):
            continue
        lines.append(f"{key}: {value}")
    lines.extend(["", "Score status", "------------"])
    for status, count in status_counts.most_common():
        lines.append(f"{status:18s} {count:6d}")
    lines.extend(["", "Top reasons", "-----------"])
    for reason, count in reason_counts.most_common(30):
        lines.append(f"{reason:40s} {count:6d}")

    review = [s for s in scores if s.status == "REVIEW"]
    review.sort(
        key=lambda s: (
            -(s.pitch_span_deg or 0.0),
            -(abs(s.net_pitch_deg or 0.0)),
            s.stock_clip_id,
        )
    )
    lines.extend(["", "Top REVIEW clips by pitch span", "------------------------------"])
    for score in review[:50]:
        lines.append(
            f"{score.stock_clip_id}  span={score.pitch_span_deg or 0:6.2f}  "
            f"net={score.net_pitch_deg or 0:+6.2f}  "
            f"p3={score.max_pitch_delta_3s.magnitude or 0:6.2f}  "
            f"action={score.suggested_action or '-'}  {score.project_name}"
        )
    path.write_text("\n".join(lines) + "\n")


def print_progress_summary(scores: Sequence[ClipScore]) -> None:
    counts = Counter(s.status for s in scores)
    rendered = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"  scored={len(scores):,} {rendered}", flush=True)


def run(args: argparse.Namespace) -> int:
    started = time.time()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    thresholds = Thresholds(min_clean_seconds=args.min_clean_seconds)

    if DJILog is None:
        print(
            "ERROR: pydjirecord is not importable in this Python environment.\n"
            "Run this script with ~/.venvs/pydjirecord/bin/python.",
            file=sys.stderr,
        )
        return 2

    review_roots = [Path(value).expanduser().resolve() for value in args.review_root]
    db_path = Path(args.db).expanduser().resolve()
    flight_root = Path(args.flight_record_root).expanduser().resolve()
    media_roots = [Path(value).expanduser().resolve() for value in args.media_root]

    print("Reading physical review shards...", flush=True)
    all_appearances: list[ClipAppearance] = []
    xml_issues: list[dict[str, Any]] = []
    for root in review_roots:
        rows, issues = parse_review_root(root)
        print(f"  {root}: {len(rows):,} VCLIP appearance(s)", flush=True)
        all_appearances.extend(rows)
        xml_issues.extend(issues)

    physical, duplicate_physical = dedupe_physical_appearances(all_appearances)
    print(f"Physical unique VCLIPs: {len(physical):,}", flush=True)
    if duplicate_physical:
        print(f"WARNING: {len(duplicate_physical):,} VCLIP ID(s) appear physically more than once", flush=True)

    db_rows = enrich_from_db(db_path, physical)
    for clip in physical:
        scope, family = classify_scope(clip, db_rows.get(clip.stock_clip_id))
        clip.camera_scope = scope
        clip.camera_family = family

    non_drone = [clip for clip in physical if clip.camera_scope == SCOPE_OUT_OF_SCOPE_NON_DRONE]
    active = [clip for clip in physical if clip.camera_scope != SCOPE_OUT_OF_SCOPE_NON_DRONE]
    if args.limit_clips:
        active = active[: args.limit_clips]
    print(f"Excluded known non-drone: {len(non_drone):,}", flush=True)
    print(f"Telemetry-QC scope: {len(active):,}", flush=True)

    print("Resolving source media...", flush=True)
    resolve_media_paths(active, media_roots)
    media_counts = Counter(clip.media_resolution or "unknown" for clip in active)
    print("  " + " ".join(f"{k}={v}" for k, v in sorted(media_counts.items())), flush=True)

    probe_cache_path = output_dir / "media-probe-cache.json"
    probe_cache: dict[str, Any] = load_json_file(probe_cache_path, {})
    probes: dict[str, MediaProbe] = {}
    unique_media = sorted({clip.media_path for clip in active if clip.media_path})
    print(f"Probing {len(unique_media):,} unique source video(s) with ffprobe...", flush=True)
    for idx, media in enumerate(unique_media, 1):
        probes[media] = ffprobe_media(Path(media), args.ffprobe, probe_cache)
        if idx % 100 == 0:
            print(f"  ffprobe {idx}/{len(unique_media)}", flush=True)
            atomic_write_json(probe_cache_path, probe_cache)
    atomic_write_json(probe_cache_path, probe_cache)

    print("Indexing DJI flight records...", flush=True)
    flight_paths, flight_inventory = enumerate_flight_logs(flight_root)
    print(
        f"  {flight_inventory['unique_flight_start_times']:,} unique flight start time(s) "
        f"from {flight_inventory['txt_files_parseable']:,} parseable TXT file(s)",
        flush=True,
    )
    headers, header_cache_stats = read_flight_headers(
        flight_paths, output_dir / "flight-header-cache.json"
    )
    usable_headers = [h for h in headers if h.start_dt is not None and h.total_time_s]
    print(f"  usable flight headers: {len(usable_headers):,}", flush=True)

    source_groups: dict[tuple[str, str], list[ClipAppearance]] = defaultdict(list)
    pre_scores: list[ClipScore] = []
    source_match_meta: dict[tuple[str, str], tuple[MediaProbe, FlightHeader, str, datetime]] = {}

    for clip in active:
        if clip.source_start_s is None or clip.duration_s is None:
            pre_scores.append(score_without_telemetry(clip, "NO_TELEMETRY", "missing_source_range"))
            continue
        if not clip.media_path:
            pre_scores.append(score_without_telemetry(clip, "NO_TELEMETRY", "missing_source_media"))
            continue
        probe = probes.get(clip.media_path)
        if probe is None:
            pre_scores.append(score_without_telemetry(clip, "NO_TELEMETRY", "missing_media_probe"))
            continue
        candidates = candidate_flights(probe, clip, usable_headers)
        if not candidates:
            pre_scores.append(score_without_telemetry(clip, "NO_TELEMETRY", "no_matching_flight_log", probe))
            continue
        _, header, method, creation = candidates[0]
        key = (header.path, clip.media_path)
        source_groups[key].append(clip)
        source_match_meta[key] = (probe, header, method, creation)

    # Group source videos by matched flight so each flight is decrypted once.
    by_flight: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for key in source_groups:
        by_flight[key[0]].append(key)

    api_key = os.environ.get(args.api_key_env)
    scores: list[ClipScore] = list(pre_scores)
    flight_decode_errors: list[dict[str, Any]] = []
    matched_headers = {header.path: header for _, header, _, _ in source_match_meta.values()}
    total_flights = len(by_flight)
    print(f"Decoding/scoring {total_flights:,} matched flight log(s)...", flush=True)

    for flight_idx, flight_path in enumerate(sorted(by_flight), 1):
        header = matched_headers[flight_path]
        print(
            f"[{flight_idx}/{total_flights}] {Path(flight_path).name} "
            f"({len(by_flight[flight_path])} source video(s))",
            flush=True,
        )
        try:
            flight_samples = decode_flight(header, api_key)
        except Exception as exc:
            error_text = f"{type(exc).__name__}:{exc}"
            flight_decode_errors.append({"flight_log": flight_path, "error": error_text})
            for key in by_flight[flight_path]:
                probe, _, _, _ = source_match_meta[key]
                for clip in source_groups[key]:
                    score = score_without_telemetry(clip, "NO_TELEMETRY", "flight_decode_failed", probe)
                    score.flight_log = flight_path
                    score.flight_version = header.version
                    score.aircraft_name = header.aircraft_name
                    score.error = error_text
                    scores.append(score)
            continue

        for key in by_flight[flight_path]:
            probe, _, method, creation = source_match_meta[key]
            for clip in source_groups[key]:
                scores.append(
                    score_from_samples(
                        clip,
                        probe,
                        header,
                        method,
                        creation,
                        flight_samples,
                        thresholds,
                    )
                )

        if flight_idx % max(1, args.checkpoint_every_flights) == 0:
            scores.sort(key=lambda s: s.stock_clip_id)
            write_scores_csv(output_dir / "telemetry-qc-scores.partial.csv", scores)
            atomic_write_json(
                output_dir / "telemetry-qc-scores.partial.json",
                [asdict(score) for score in scores],
            )
            print_progress_summary(scores)

    scores.sort(key=lambda s: s.stock_clip_id)
    status_counts = Counter(score.status for score in scores)
    reason_counts = Counter(reason for score in scores for reason in score.reasons)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runtime_seconds": round(time.time() - started, 2),
        "physical_unique_vclips": len(physical),
        "known_non_drone_excluded": len(non_drone),
        "telemetry_qc_scope": len(active),
        "db_latest_accepted_rows_found": len(db_rows),
        "physical_duplicate_id_count": len(duplicate_physical),
        "xml_issue_count": len(xml_issues),
        "media_resolved": sum(1 for clip in active if clip.media_path),
        "media_missing": sum(1 for clip in active if not clip.media_path),
        "unique_media_resolved": len(unique_media),
        "flight_inventory": flight_inventory,
        "flight_header_cache": header_cache_stats,
        "usable_flight_headers": len(usable_headers),
        "matched_source_videos": len(source_groups),
        "matched_flight_logs": total_flights,
        "flight_decode_error_count": len(flight_decode_errors),
        "scores_emitted": len(scores),
        "status_counts": dict(status_counts),
        "reason_counts": dict(reason_counts),
        "thresholds": asdict(thresholds),
    }

    write_scores_csv(output_dir / "telemetry-qc-scores.csv", scores)
    write_scores_csv(
        output_dir / "telemetry-qc-review.csv",
        [score for score in scores if score.status in {"REVIEW", "SOFT_REVIEW"}],
    )
    write_scores_csv(
        output_dir / "telemetry-qc-no-telemetry.csv",
        [score for score in scores if score.status == "NO_TELEMETRY"],
    )
    write_scores_csv(
        output_dir / "telemetry-qc-pass.csv",
        [score for score in scores if score.status == "PASS"],
    )
    atomic_write_json(output_dir / "telemetry-qc-scores.json", [asdict(score) for score in scores])
    atomic_write_json(output_dir / "telemetry-qc-summary.json", summary)
    atomic_write_json(output_dir / "telemetry-qc-xml-issues.json", xml_issues)
    atomic_write_json(output_dir / "telemetry-qc-physical-duplicates.json", duplicate_physical)
    atomic_write_json(output_dir / "telemetry-qc-flight-errors.json", flight_decode_errors)
    atomic_write_json(output_dir / "telemetry-qc-non-drone-excluded.json", [asdict(c) for c in non_drone])
    write_summary(output_dir / "telemetry-qc-summary.txt", summary, scores)

    print("", flush=True)
    print("FINAL", flush=True)
    print("=====", flush=True)
    print(f"Physical VCLIPs:        {len(physical):,}", flush=True)
    print(f"Non-drone excluded:     {len(non_drone):,}", flush=True)
    print(f"Telemetry-QC scope:     {len(active):,}", flush=True)
    print(f"Media resolved:         {summary['media_resolved']:,}", flush=True)
    print(f"Matched source videos:  {len(source_groups):,}", flush=True)
    print(f"Matched flight logs:    {total_flights:,}", flush=True)
    for status in ["PASS", "SOFT_REVIEW", "REVIEW", "NO_TELEMETRY"]:
        print(f"{status:20s} {status_counts.get(status, 0):6,d}", flush=True)
    print(f"Reports: {output_dir}", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Score physical VClip review candidates using DJI flight-record gimbal telemetry."
    )
    parser.add_argument(
        "--review-root",
        action="append",
        required=True,
        help="Physical review-shard root. Repeat for multiple active corpora.",
    )
    parser.add_argument("--db", required=True, help="Canonical vclip.sqlite3 (read-only).")
    parser.add_argument("--flight-record-root", required=True, help="Folder containing DJI *.txt flight logs.")
    parser.add_argument(
        "--media-root",
        action="append",
        default=[],
        help="Mounted media root used only as fallback when FCPXML/DB paths are unavailable. Repeatable.",
    )
    parser.add_argument("--output-dir", required=True, help="Report/cache output directory.")
    parser.add_argument("--ffprobe", default="ffprobe", help="ffprobe executable (default: ffprobe).")
    parser.add_argument(
        "--api-key-env",
        default="DJI_API_KEY",
        help="Environment variable containing DJI Open API key (default: DJI_API_KEY).",
    )
    parser.add_argument(
        "--min-clean-seconds",
        type=float,
        default=5.0,
        help="Minimum clean segment length for trim/split suggestions (default: 5).",
    )
    parser.add_argument(
        "--limit-clips",
        type=int,
        default=0,
        help="Debug/pilot limit after drone-scope filtering. 0 means all.",
    )
    parser.add_argument(
        "--checkpoint-every-flights",
        type=int,
        default=10,
        help="Write partial score files every N decoded flights (default: 10).",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not args.media_root:
        args.media_root = ["/Volumes"]
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
