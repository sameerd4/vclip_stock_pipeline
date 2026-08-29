#!/usr/bin/env python3
"""
VClip OpenAI production-QC runner.

Reads candidate rows from a CSV, samples frames from either:
  - an already-cut proxy MP4/MOV, or
  - the original media referenced by the candidate's reconstructed FCPXML,

then sends chronological sampled frames to the OpenAI Responses API with a
strict JSON schema.

This script is intentionally a visual-QC layer. It does not mutate FCPXML,
SQLite, source media, canonical masters, or the VClip library.

Requires:
  - ffmpeg / ffprobe on PATH
  - OPENAI_API_KEY in the environment
  - Python standard library only (certifi is used if installed)

Examples:

Calibration against already-cut proxies:
  python vclip_openai_production_qc.py \
    --input-csv /path/to/proxy-report.csv \
    --output-root /path/to/qc-output \
    --source-mode proxy \
    --media-column proxy_path \
    --frame-count 10 \
    --workers 4

Reconstructed FCPXML candidates:
  python vclip_openai_production_qc.py \
    --input-csv /path/to/unambiguous-source-unique.csv \
    --output-root /path/to/qc-output \
    --source-mode fcpxml \
    --frame-count 10 \
    --workers 6
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

API_URL = "https://api.openai.com/v1/responses"
PROMPT_VERSION = "vclip-production-qc-v1"

SYSTEM_PROMPT = """You are a production-QC classifier for chronological sampled frames from one drone stock-footage clip.

The sampled frames are ordered from the beginning to the end of the candidate clip and are approximately evenly spaced.

Do NOT identify, name, recognize, or guess the identity of any person. Only classify whether human figures are visibly present and how prominent they are.

Primary QC goals:
1. Detect a person who is clearly visible in the shot, especially a likely drone operator or companion near the launch/camera position.
2. Detect an obvious mid-shot camera reset, reframing, repositioning, or composition discontinuity that makes the clip feel like multiple takes rather than one coherent stock shot.
3. Detect obvious visual obstruction or unusable capture context.
4. Give a conservative stock-usability judgment.

Important:
- Do not penalize flat, log, D-Log, low-saturation, or ungraded color. These source frames may not contain the Final Cut color treatment even when the reconstructed project does.
- Smooth intentional pans, orbits, pushes, pulls, rises, and tracking moves are valid stock footage.
- A distant tiny pedestrian is not automatically a blocker.
- A clearly visible person close to the drone/camera, especially someone who appears to be operating or accompanying the drone, is a production blocker.
- If a clip is mostly smooth but appears to reframe/reset once in the middle, choose review rather than reject and mark the repositioning.
- If evidence is ambiguous, choose review rather than reject.
- person_sample_indices and reposition_sample_indices use 1-based indices into the chronological sampled frames.
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "person_presence": {
            "type": "string",
            "enum": ["none", "tiny_background", "visible", "prominent"],
        },
        "person_frame_hits": {"type": "integer", "minimum": 0, "maximum": 32},
        "person_sample_indices": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1, "maximum": 32},
        },
        "likely_operator_present": {"type": "boolean"},
        "camera_repositioning": {
            "type": "string",
            "enum": ["none", "minor", "significant"],
        },
        "reposition_sample_indices": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1, "maximum": 32},
        },
        "composition_discontinuity": {"type": "boolean"},
        "visual_obstruction": {"type": "boolean"},
        "takeoff_or_landing_context": {"type": "boolean"},
        "stock_usability": {
            "type": "string",
            "enum": ["pass", "review", "reject"],
        },
        "confidence": {
            "type": "string",
            "enum": ["low", "medium", "high"],
        },
        "reasons": {
            "type": "array",
            "items": {"type": "string"},
        },
        "notes": {"type": "string"},
    },
    "required": [
        "person_presence",
        "person_frame_hits",
        "person_sample_indices",
        "likely_operator_present",
        "camera_repositioning",
        "reposition_sample_indices",
        "composition_discontinuity",
        "visual_obstruction",
        "takeoff_or_landing_context",
        "stock_usability",
        "confidence",
        "reasons",
        "notes",
    ],
    "additionalProperties": False,
}


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def metadata_dict(clip: ET.Element) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in clip.iter():
        if local_name(node.tag) != "md":
            continue
        key = node.get("key")
        if key:
            out[key] = node.get("value") or ""
    return out


def project_stock_id(project: ET.Element) -> str:
    for node in project.iter():
        if local_name(node.tag) != "asset-clip":
            continue
        sid = metadata_dict(node).get("com.vclip.stock_clip_id")
        if sid:
            return sid
    blob = ET.tostring(project, encoding="unicode")
    m = re.search(r"VCLIP_[0-9A-F]{12,64}", blob)
    return m.group(0) if m else ""


def first_asset_clip(project: ET.Element) -> ET.Element | None:
    return next(
        (node for node in project.iter() if local_name(node.tag) == "asset-clip"),
        None,
    )


def file_url_to_path(value: str) -> Path | None:
    value = (value or "").strip()
    if not value:
        return None
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme == "file":
        path = urllib.parse.unquote(parsed.path)
        return Path(path)
    if parsed.scheme:
        return None
    return Path(urllib.parse.unquote(value)).expanduser()


def resource_media_candidates(resource: ET.Element) -> list[Path]:
    ranked: list[tuple[int, Path]] = []

    def add(value: str | None, rank: int) -> None:
        path = file_url_to_path(value or "")
        if path is not None:
            ranked.append((rank, path))

    add(resource.get("src"), 10)

    for node in resource.iter():
        if node is resource:
            continue
        src = node.get("src")
        if not src:
            continue
        kind = (node.get("kind") or "").casefold()
        rank = 0 if "original" in kind else 5
        add(src, rank)

    ranked.sort(key=lambda pair: pair[0])
    unique: list[Path] = []
    seen: set[str] = set()
    for _rank, path in ranked:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


class FCPXMLResolver:
    def __init__(self) -> None:
        self._cache: dict[str, dict[str, Path]] = {}
        self._lock = threading.Lock()

    def _parse(self, xml_path: Path) -> dict[str, Path]:
        tree = ET.parse(xml_path)
        root = tree.getroot()

        resources_node = next(
            (node for node in list(root) if local_name(node.tag) == "resources"),
            None,
        )
        if resources_node is None:
            raise RuntimeError(f"No <resources> in {xml_path}")

        index = {
            node.get("id"): node
            for node in list(resources_node)
            if node.get("id")
        }

        result: dict[str, Path] = {}
        for event in root.iter():
            if local_name(event.tag) != "event":
                continue
            for project in list(event):
                if local_name(project.tag) != "project":
                    continue
                sid = project_stock_id(project)
                if not sid:
                    continue
                clip = first_asset_clip(project)
                if clip is None:
                    continue
                ref = clip.get("ref") or ""
                resource = index.get(ref)
                if resource is None:
                    continue
                candidates = resource_media_candidates(resource)
                if not candidates:
                    continue

                # Prefer an actually mounted path. If none exists, retain the
                # first FCPXML path so the caller gets a useful error.
                chosen = next((p for p in candidates if p.is_file()), candidates[0])
                result.setdefault(sid, chosen)
        return result

    def resolve(self, xml_path: Path, stock_clip_id: str) -> Path:
        key = str(xml_path)
        with self._lock:
            mapping = self._cache.get(key)
        if mapping is None:
            mapping = self._parse(xml_path)
            with self._lock:
                self._cache[key] = mapping
        path = mapping.get(stock_clip_id)
        if path is None:
            raise RuntimeError(
                f"Could not resolve {stock_clip_id} from reconstructed FCPXML {xml_path}"
            )
        return path


@dataclass(frozen=True)
class Config:
    input_csv: Path
    output_root: Path
    source_mode: str
    media_column: str
    model: str
    workers: int
    frame_count: int
    max_dim: int
    detail: str
    limit: int | None
    start_index: int
    include_relations: set[str] | None
    min_duration: float
    require_graded: bool
    prepare_only: bool
    retries: int


def parse_args() -> Config:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input-csv", type=Path, required=True)
    p.add_argument("--output-root", type=Path, required=True)
    p.add_argument("--source-mode", choices=["proxy", "fcpxml"], required=True)
    p.add_argument("--media-column", default="proxy_path")
    p.add_argument("--model", default="gpt-5-mini")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--frame-count", type=int, default=10)
    p.add_argument("--max-dim", type=int, default=768)
    p.add_argument("--detail", choices=["low", "high", "auto"], default="low")
    p.add_argument("--limit", type=int)
    p.add_argument("--start-index", type=int, default=0)
    p.add_argument(
        "--include-relations",
        help="Comma-separated deterministic source relations to keep, e.g. DISJOINT,NO_READY_ON_SOURCE",
    )
    p.add_argument("--min-duration", type=float, default=0.0)
    p.add_argument("--require-graded", action="store_true")
    p.add_argument("--prepare-only", action="store_true")
    p.add_argument("--retries", type=int, default=5)
    a = p.parse_args()

    if a.frame_count < 3 or a.frame_count > 32:
        p.error("--frame-count must be between 3 and 32")
    if a.workers < 1:
        p.error("--workers must be >= 1")

    relations = None
    if a.include_relations:
        relations = {
            x.strip().upper() for x in a.include_relations.split(",") if x.strip()
        }

    return Config(
        input_csv=a.input_csv.expanduser().resolve(),
        output_root=a.output_root.expanduser().resolve(),
        source_mode=a.source_mode,
        media_column=a.media_column,
        model=a.model,
        workers=a.workers,
        frame_count=a.frame_count,
        max_dim=a.max_dim,
        detail=a.detail,
        limit=a.limit,
        start_index=a.start_index,
        include_relations=relations,
        min_duration=a.min_duration,
        require_graded=a.require_graded,
        prepare_only=a.prepare_only,
        retries=a.retries,
    )


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def relation_for(row: dict[str, str]) -> str:
    return (
        row.get("best_existing_relation")
        or row.get("best_ready_relation")
        or row.get("ready_relation")
        or ""
    ).upper()


def is_graded(row: dict[str, str]) -> bool:
    value = (row.get("graded") or "").strip().upper()
    if value:
        return value in {"YES", "TRUE", "1"}
    return safe_float(row.get("custom_lut_count"), 0.0) > 0


def load_input(cfg: Config) -> list[dict[str, str]]:
    with cfg.input_csv.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    filtered: list[dict[str, str]] = []
    for row in rows:
        duration = safe_float(row.get("duration_s"), 0.0)
        if duration < cfg.min_duration:
            continue
        if cfg.require_graded and not is_graded(row):
            continue
        if cfg.include_relations is not None:
            if relation_for(row) not in cfg.include_relations:
                continue
        filtered.append(row)

    filtered = filtered[cfg.start_index :]
    if cfg.limit is not None:
        filtered = filtered[: cfg.limit]
    return filtered


def ffprobe_duration(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for {path}: {(proc.stderr or proc.stdout)[-1200:]}"
        )
    return float(proc.stdout.strip())


def frame_cache_key(
    media: Path,
    start_s: float,
    duration_s: float,
    frame_count: int,
    max_dim: int,
) -> str:
    try:
        st = media.stat()
        identity = f"{media.resolve()}|{st.st_size}|{st.st_mtime_ns}"
    except OSError:
        identity = str(media)
    payload = f"{identity}|{start_s:.6f}|{duration_s:.6f}|{frame_count}|{max_dim}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]


def ensure_frames(
    *,
    clip_id: str,
    media: Path,
    start_s: float,
    duration_s: float,
    cfg: Config,
    frames_root: Path,
) -> list[Path]:
    if not media.is_file():
        raise FileNotFoundError(f"Missing media for {clip_id}: {media}")

    duration_s = max(0.05, duration_s)
    cache_key = frame_cache_key(
        media, start_s, duration_s, cfg.frame_count, cfg.max_dim
    )
    clip_dir = frames_root / clip_id
    manifest = clip_dir / "manifest.json"

    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if data.get("cache_key") == cache_key:
                frames = sorted(clip_dir.glob("frame-*.jpg"))
                if len(frames) >= 3:
                    return frames
        except Exception:
            pass

    clip_dir.mkdir(parents=True, exist_ok=True)
    for old in clip_dir.glob("frame-*.jpg"):
        old.unlink(missing_ok=True)

    # One ffmpeg invocation per clip. fps=N/duration gives approximately N
    # chronological samples across the exact candidate interval.
    fps = cfg.frame_count / duration_s
    pattern = str(clip_dir / "frame-%02d.jpg")
    vf = (
        f"fps={fps:.10f},"
        f"scale={cfg.max_dim}:{cfg.max_dim}:force_original_aspect_ratio=decrease"
    )
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start_s:.6f}",
        "-i",
        str(media),
        "-t",
        f"{duration_s:.6f}",
        "-an",
        "-vf",
        vf,
        "-frames:v",
        str(cfg.frame_count),
        "-q:v",
        "3",
        "-y",
        pattern,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg frame extraction failed for {clip_id}: "
            f"{(proc.stderr or proc.stdout)[-1800:]}"
        )

    frames = sorted(clip_dir.glob("frame-*.jpg"))
    if len(frames) < 3:
        raise RuntimeError(f"Only {len(frames)} frames extracted for {clip_id}")

    manifest.write_text(
        json.dumps(
            {
                "cache_key": cache_key,
                "stock_clip_id": clip_id,
                "media": str(media),
                "start_s": start_s,
                "duration_s": duration_s,
                "frame_count": len(frames),
                "requested_frame_count": cfg.frame_count,
                "max_dim": cfg.max_dim,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return frames


def encode_data_url(path: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode(
        "ascii"
    )


def ssl_context() -> ssl.SSLContext:
    cafile = os.environ.get("SSL_CERT_FILE")
    if cafile and Path(cafile).is_file():
        return ssl.create_default_context(cafile=cafile)
    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CONTEXT = ssl_context()


def response_output_text(payload: dict[str, Any]) -> str:
    chunks: list[str] = []
    for item in payload.get("output") or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if content.get("type") == "output_text":
                chunks.append(content.get("text") or "")
    return "".join(chunks).strip()


def call_openai(
    *,
    api_key: str,
    row: dict[str, str],
    frames: list[Path],
    cfg: Config,
) -> tuple[dict[str, Any], dict[str, Any]]:
    clip_id = row["stock_clip_id"]
    scope = row.get("scope") or row.get("scope_prefix") or ""
    user_text = f"""Review this drone-stock candidate.

stock_clip_id: {clip_id}
scope: {scope}
project_name: {row.get('project_name', '')}
source_name: {row.get('source_name', '')}
duration_s: {row.get('duration_s', '')}
sample_count: {len(frames)}

The images that follow are chronological and approximately evenly spaced across the candidate clip.
Return only the requested structured production-QC result.
"""

    content: list[dict[str, Any]] = [{"type": "input_text", "text": user_text}]
    for frame in frames:
        content.append(
            {
                "type": "input_image",
                "image_url": encode_data_url(frame),
                "detail": cfg.detail,
            }
        )

    body = {
        "model": cfg.model,
        "store": False,
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": SYSTEM_PROMPT}],
            },
            {"role": "user", "content": content},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "vclip_production_qc",
                "strict": True,
                "schema": SCHEMA,
            }
        },
    }

    raw = json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "vclip-production-qc/1",
    }

    last_error: Exception | None = None
    for attempt in range(cfg.retries + 1):
        request = urllib.request.Request(API_URL, data=raw, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(
                request, context=SSL_CONTEXT, timeout=180
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
            text = response_output_text(payload)
            if not text:
                raise RuntimeError(
                    f"OpenAI response had no output_text for {clip_id}: "
                    f"{json.dumps(payload)[:1600]}"
                )
            parsed = json.loads(text)
            return parsed, payload
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            retryable = exc.code == 429 or 500 <= exc.code <= 599
            last_error = RuntimeError(f"OpenAI HTTP {exc.code}: {body_text[-2200:]}")
            if not retryable or attempt >= cfg.retries:
                raise last_error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt >= cfg.retries:
                raise
        delay = min(30.0, 1.5 * (2**attempt))
        time.sleep(delay)

    raise RuntimeError(f"OpenAI request failed: {last_error}")


def normalize_qc(parsed: dict[str, Any]) -> dict[str, Any]:
    # Structured Outputs should already guarantee the shape. Normalize only for
    # convenient CSV serialization.
    return {
        "person_presence": str(parsed["person_presence"]),
        "person_frame_hits": int(parsed["person_frame_hits"]),
        "person_sample_indices": [int(x) for x in parsed["person_sample_indices"]],
        "likely_operator_present": bool(parsed["likely_operator_present"]),
        "camera_repositioning": str(parsed["camera_repositioning"]),
        "reposition_sample_indices": [
            int(x) for x in parsed["reposition_sample_indices"]
        ],
        "composition_discontinuity": bool(parsed["composition_discontinuity"]),
        "visual_obstruction": bool(parsed["visual_obstruction"]),
        "takeoff_or_landing_context": bool(parsed["takeoff_or_landing_context"]),
        "stock_usability": str(parsed["stock_usability"]),
        "confidence": str(parsed["confidence"]),
        "reasons": [str(x) for x in parsed["reasons"]],
        "notes": str(parsed["notes"]),
    }


ADDITIVE_RELATIONS = {
    "DISJOINT",
    "NO_READY_ON_SOURCE",
    "NO_EXISTING_ON_SOURCE",
}


def local_decision(row: dict[str, str], qc: dict[str, Any]) -> str:
    relation = relation_for(row)
    telemetry = (
        row.get("qc_status") or row.get("telemetry_status") or ""
    ).upper()
    operator = (row.get("operator_status") or "").upper()

    if relation in {"EXACT", "NEAR_DUPLICATE"}:
        return "REJECT_SOURCE_DUPLICATE"

    if qc["likely_operator_present"] and qc["person_presence"] in {
        "visible",
        "prominent",
    }:
        return "REJECT_PERSON"

    if qc["person_presence"] == "prominent":
        return "REJECT_PERSON"

    if (
        qc["camera_repositioning"] == "significant"
        or qc["composition_discontinuity"]
    ):
        return "REVIEW_REPOSITIONING"

    if qc["visual_obstruction"]:
        return "REVIEW_VISUAL_OBSTRUCTION"

    if qc["person_presence"] == "visible":
        return "REVIEW_PERSON"

    if relation == "LARGELY_OVERLAPPING":
        return "REVIEW_SOURCE_OVERLAP"

    if qc["stock_usability"] == "reject":
        return "REVIEW_OTHER_VISUAL"

    if (
        qc["stock_usability"] == "pass"
        and telemetry == "PASS"
        and operator == "CLEAN"
        and relation in ADDITIVE_RELATIONS
    ):
        return "OPENAI_VISUAL_CLEAR"

    if (
        qc["stock_usability"] == "pass"
        and telemetry == "PASS"
        and operator == "MOVEMENT_ADVISORY"
        and relation in ADDITIVE_RELATIONS
    ):
        return "REVIEW_MOVEMENT"

    if qc["stock_usability"] == "pass":
        return "PROMOTE_AFTER_SPOTCHECK"

    return "REVIEW"


def usage_fields(payload: dict[str, Any]) -> dict[str, Any]:
    usage = payload.get("usage") or {}
    input_details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    return {
        "input_tokens": usage.get("input_tokens", ""),
        "cached_input_tokens": input_details.get("cached_tokens", ""),
        "output_tokens": usage.get("output_tokens", ""),
        "reasoning_tokens": output_details.get("reasoning_tokens", ""),
        "total_tokens": usage.get("total_tokens", ""),
    }


def result_row(
    row: dict[str, str],
    qc: dict[str, Any],
    *,
    media: Path,
    media_start_s: float,
    frame_count: int,
    payload: dict[str, Any],
    cfg: Config,
) -> dict[str, Any]:
    out = {
        "stock_clip_id": row["stock_clip_id"],
        "scope": row.get("scope") or row.get("scope_prefix") or "",
        "project_name": row.get("project_name", ""),
        "source_name": row.get("source_name", ""),
        "source_mode": cfg.source_mode,
        "resolved_media_path": str(media),
        "media_start_s": round(media_start_s, 6),
        "duration_s": safe_float(row.get("duration_s"), 0.0),
        "qc_status": row.get("qc_status") or row.get("telemetry_status") or "",
        "operator_status": row.get("operator_status", ""),
        "source_relation": relation_for(row),
        "graded": "YES" if is_graded(row) else "NO",
        "prompt_version": PROMPT_VERSION,
        "model": cfg.model,
        "frame_count": frame_count,
        **qc,
        "final_decision": local_decision(row, qc),
        **usage_fields(payload),
    }
    return out


CSV_FIELDS = [
    "stock_clip_id",
    "scope",
    "project_name",
    "source_name",
    "source_mode",
    "resolved_media_path",
    "media_start_s",
    "duration_s",
    "qc_status",
    "operator_status",
    "source_relation",
    "graded",
    "prompt_version",
    "model",
    "frame_count",
    "person_presence",
    "person_frame_hits",
    "person_sample_indices",
    "likely_operator_present",
    "camera_repositioning",
    "reposition_sample_indices",
    "composition_discontinuity",
    "visual_obstruction",
    "takeoff_or_landing_context",
    "stock_usability",
    "confidence",
    "reasons",
    "notes",
    "final_decision",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
]


def csv_safe(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in ("person_sample_indices", "reposition_sample_indices"):
        out[key] = "|".join(str(x) for x in out.get(key, []))
    out["reasons"] = "|".join(out.get("reasons", []))
    for key in (
        "likely_operator_present",
        "composition_discontinuity",
        "visual_obstruction",
        "takeoff_or_landing_context",
    ):
        out[key] = "YES" if out.get(key) else "NO"
    return {field: out.get(field, "") for field in CSV_FIELDS}


def write_outputs(
    output_root: Path,
    results: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    prepared: list[dict[str, Any]],
    cfg: Config,
) -> None:
    output_root.mkdir(parents=True, exist_ok=True)

    with (output_root / "results.jsonl").open("w", encoding="utf-8") as f:
        for row in sorted(results, key=lambda r: r["stock_clip_id"]):
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    with (output_root / "results.csv").open(
        "w", newline="", encoding="utf-8"
    ) as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in sorted(results, key=lambda r: r["stock_clip_id"]):
            writer.writerow(csv_safe(row))

    if prepared:
        fields = sorted({k for row in prepared for k in row})
        with (output_root / "resolved-input.csv").open(
            "w", newline="", encoding="utf-8"
        ) as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(prepared)

    decision_counts = Counter(row["final_decision"] for row in results)
    person_counts = Counter(row["person_presence"] for row in results)
    reposition_counts = Counter(row["camera_repositioning"] for row in results)
    usability_counts = Counter(row["stock_usability"] for row in results)

    total_usage = Counter()
    for row in results:
        for key in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
        ):
            try:
                total_usage[key] += int(row.get(key) or 0)
            except Exception:
                pass

    summary = {
        "prompt_version": PROMPT_VERSION,
        "model": cfg.model,
        "source_mode": cfg.source_mode,
        "completed": len(results),
        "failed": len(failures),
        "by_decision": dict(decision_counts),
        "by_person_presence": dict(person_counts),
        "by_camera_repositioning": dict(reposition_counts),
        "by_stock_usability": dict(usability_counts),
        "usage": dict(total_usage),
        "failures": failures,
    }
    (output_root / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    cfg = parse_args()

    for exe in ("ffmpeg", "ffprobe"):
        if shutil.which(exe) is None:
            raise SystemExit(f"{exe} is required on PATH")

    if not cfg.prepare_only and not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set in this shell")

    rows = load_input(cfg)
    if not rows:
        raise SystemExit("No input rows remain after filtering")

    if "stock_clip_id" not in rows[0]:
        raise SystemExit("Input CSV must contain stock_clip_id")
    if cfg.source_mode == "fcpxml" and "xml_path" not in rows[0]:
        raise SystemExit("FCPXML mode requires an xml_path column")
    if cfg.source_mode == "proxy" and cfg.media_column not in rows[0]:
        raise SystemExit(
            f"Proxy mode requires media column {cfg.media_column!r}"
        )

    cfg.output_root.mkdir(parents=True, exist_ok=True)
    frames_root = cfg.output_root / "frames"
    analysis_root = cfg.output_root / "analysis"
    analysis_root.mkdir(parents=True, exist_ok=True)

    resolver = FCPXMLResolver()
    prepared: list[dict[str, Any]] = []
    prepared_work: list[tuple[dict[str, str], Path, float, float]] = []
    prep_failures: list[dict[str, Any]] = []

    print("VCLIP OPENAI PRODUCTION QC")
    print("==========================")
    print("input       :", cfg.input_csv)
    print("output      :", cfg.output_root)
    print("source mode :", cfg.source_mode)
    print("rows        :", len(rows))
    print("model       :", cfg.model)
    print("frames/clip :", cfg.frame_count)
    print("workers     :", cfg.workers)
    print()

    print("RESOLVING MEDIA")
    print("---------------")
    for idx, row in enumerate(rows, 1):
        sid = row["stock_clip_id"]
        try:
            if cfg.source_mode == "proxy":
                media = Path(row[cfg.media_column]).expanduser()
                media_start = 0.0
                actual_duration = ffprobe_duration(media)
                requested = safe_float(row.get("duration_s"), actual_duration)
                duration = min(requested if requested > 0 else actual_duration, actual_duration)
            else:
                xml_path = Path(row["xml_path"]).expanduser()
                media = resolver.resolve(xml_path, sid)
                media_start = safe_float(row.get("start_s"), 0.0)
                duration = safe_float(row.get("duration_s"), 0.0)
                if duration <= 0:
                    raise RuntimeError(f"Invalid duration_s for {sid}: {row.get('duration_s')}")

            if not media.is_file():
                raise FileNotFoundError(str(media))

            prepared_work.append((row, media, media_start, duration))
            prepared.append(
                {
                    "stock_clip_id": sid,
                    "resolved_media_path": str(media),
                    "media_start_s": round(media_start, 6),
                    "duration_s": round(duration, 6),
                    "source_relation": relation_for(row),
                    "graded": "YES" if is_graded(row) else "NO",
                    "xml_path": row.get("xml_path", ""),
                }
            )
        except Exception as exc:
            prep_failures.append(
                {"stock_clip_id": sid, "stage": "resolve", "error": repr(exc)}
            )
        if idx == 1 or idx % 100 == 0 or idx == len(rows):
            print(f"resolved {idx:4d}/{len(rows)}")

    print()
    print("resolved ok :", len(prepared_work))
    print("resolve fail:", len(prep_failures))

    if cfg.prepare_only:
        write_outputs(cfg.output_root, [], prep_failures, prepared, cfg)
        print()
        print("PREPARE ONLY: PASS" if not prep_failures else "PREPARE ONLY: PARTIAL")
        return 0 if not prep_failures else 2

    api_key = os.environ["OPENAI_API_KEY"]
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = list(prep_failures)
    lock = threading.Lock()

    def process(item: tuple[dict[str, str], Path, float, float]) -> dict[str, Any]:
        row, media, media_start, duration = item
        sid = row["stock_clip_id"]
        result_cache = analysis_root / f"{sid}.json"

        if result_cache.is_file():
            cached = json.loads(result_cache.read_text(encoding="utf-8"))
            # Cache is tied to prompt/model and exact source interval.
            if (
                cached.get("prompt_version") == PROMPT_VERSION
                and cached.get("model") == cfg.model
                and cached.get("resolved_media_path") == str(media)
                and abs(float(cached.get("media_start_s", -1)) - media_start) < 1e-6
                and abs(float(cached.get("duration_s", -1)) - duration) < 1e-6
                and int(cached.get("frame_count", 0)) >= 3
            ):
                return cached

        frames = ensure_frames(
            clip_id=sid,
            media=media,
            start_s=media_start,
            duration_s=duration,
            cfg=cfg,
            frames_root=frames_root,
        )
        parsed, payload = call_openai(
            api_key=api_key,
            row=row,
            frames=frames,
            cfg=cfg,
        )
        qc = normalize_qc(parsed)
        result = result_row(
            row,
            qc,
            media=media,
            media_start_s=media_start,
            frame_count=len(frames),
            payload=payload,
            cfg=cfg,
        )
        # Keep the full API response beside the normalized cache for debugging.
        raw_path = analysis_root / f"{sid}--openai-response.json"
        raw_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        result_cache.write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return result

    print()
    print("OPENAI QC")
    print("---------")
    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        futures = {pool.submit(process, item): item for item in prepared_work}
        total = len(futures)
        for index, future in enumerate(as_completed(futures), 1):
            row, _media, _start, _duration = futures[future]
            sid = row["stock_clip_id"]
            try:
                result = future.result()
                with lock:
                    results.append(result)
                print(
                    f"[{index:04d}/{total:04d}] {sid}  "
                    f"{result['final_decision']:24s} "
                    f"person={result['person_presence']:15s} "
                    f"reposition={result['camera_repositioning']}"
                )
            except Exception as exc:
                with lock:
                    failures.append(
                        {"stock_clip_id": sid, "stage": "openai", "error": repr(exc)}
                    )
                print(f"[{index:04d}/{total:04d}] {sid}  FAILED  {exc}")

    write_outputs(cfg.output_root, results, failures, prepared, cfg)

    print()
    print("RESULT")
    print("======")
    print("completed:", len(results))
    print("failed   :", len(failures))
    print("results  :", cfg.output_root / "results.csv")
    print("summary  :", cfg.output_root / "summary.json")
    print()
    if failures:
        print("VCLIP OPENAI PRODUCTION QC: PARTIAL")
        return 2
    print("VCLIP OPENAI PRODUCTION QC: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
