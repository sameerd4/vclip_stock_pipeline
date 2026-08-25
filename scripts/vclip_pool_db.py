#!/usr/bin/env python3
"""Durable SQLite catalog for reconstructed VClip shots and rendered masters.

The existing Stockify catalog remains authoritative for historical source
candidates. These tables add a clean layer for reconstructed Ready Cuts,
Extended Masters, dedupe lineage, deterministic export plans, and physical
rendered masters.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

try:
    from vclip_pipeline.stockify.core import local_name, parse_time
    from vclip_pipeline.stockify.fcpxml import (
        build_resource_index,
        first_direct_child,
        read_vclip_metadata,
        video_treatment_signature,
    )
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "Could not import VClip. Run with PYTHONPATH=<repo>/src. "
        f"Import error: {exc}"
    )

VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v"}


SCHEMA = r"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS reconstruction_runs (
    id TEXT PRIMARY KEY,
    manifest_path TEXT NOT NULL,
    source_root TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('complete','failed','superseded')),
    settings_json TEXT NOT NULL,
    counts_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconstructed_candidates (
    stock_clip_id TEXT PRIMARY KEY,
    reconstruction_run_id TEXT NOT NULL REFERENCES reconstruction_runs(id),
    shard_path TEXT NOT NULL,
    event_name TEXT NOT NULL,
    project_name TEXT NOT NULL,
    product_role TEXT NOT NULL CHECK(product_role IN ('ready_cut','extended_master')),
    source_name TEXT NOT NULL,
    source_path TEXT,
    source_start_seconds REAL NOT NULL,
    duration_seconds REAL NOT NULL,
    width INTEGER,
    height INTEGER,
    frame_rate REAL,
    orientation TEXT,
    candidate_tier TEXT NOT NULL,
    readiness_basis TEXT,
    qc_status TEXT,
    operator_status TEXT,
    visual_status TEXT,
    camera_lut TEXT,
    effect_signature TEXT,
    location_json TEXT NOT NULL DEFAULT '{}',
    capture_time_json TEXT NOT NULL DEFAULT '{}',
    expected_export_basename TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1,
    duplicate_of TEXT REFERENCES reconstructed_candidates(stock_clip_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reconstructed_role
    ON reconstructed_candidates(product_role, active);
CREATE INDEX IF NOT EXISTS idx_reconstructed_source
    ON reconstructed_candidates(source_name, source_start_seconds);
CREATE INDEX IF NOT EXISTS idx_reconstructed_location
    ON reconstructed_candidates(active, orientation, candidate_tier);

CREATE TABLE IF NOT EXISTS reconstructed_candidate_parents (
    stock_clip_id TEXT NOT NULL REFERENCES reconstructed_candidates(stock_clip_id) ON DELETE CASCADE,
    parent_stock_clip_id TEXT NOT NULL,
    PRIMARY KEY(stock_clip_id, parent_stock_clip_id)
);

CREATE TABLE IF NOT EXISTS reconstruction_dedupe_removals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reconstruction_run_id TEXT NOT NULL REFERENCES reconstruction_runs(id),
    removed_stock_clip_id TEXT NOT NULL,
    canonical_stock_clip_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    containment REAL,
    iou REAL,
    source_name TEXT,
    removed_project_name TEXT,
    kept_project_name TEXT,
    provenance_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(reconstruction_run_id, removed_stock_clip_id)
);

CREATE TABLE IF NOT EXISTS master_export_plans (
    id TEXT PRIMARY KEY,
    reconstruction_run_id TEXT NOT NULL REFERENCES reconstruction_runs(id),
    manifest_path TEXT NOT NULL,
    render_root TEXT NOT NULL,
    share_destination TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('planned','running','rendered','ingested','failed')),
    batch_count INTEGER NOT NULL,
    item_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS master_export_batches (
    id TEXT PRIMARY KEY,
    export_plan_id TEXT NOT NULL REFERENCES master_export_plans(id) ON DELETE CASCADE,
    batch_index INTEGER NOT NULL,
    xml_path TEXT NOT NULL,
    event_name TEXT NOT NULL,
    output_directory TEXT NOT NULL,
    expected_count INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('planned','running','rendered','failed')),
    receipt_path TEXT,
    error_text TEXT,
    started_at TEXT,
    completed_at TEXT,
    UNIQUE(export_plan_id, batch_index)
);

CREATE TABLE IF NOT EXISTS master_export_items (
    export_plan_id TEXT NOT NULL REFERENCES master_export_plans(id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL REFERENCES master_export_batches(id) ON DELETE CASCADE,
    stock_clip_id TEXT NOT NULL REFERENCES reconstructed_candidates(stock_clip_id),
    expected_basename TEXT NOT NULL,
    expected_duration_seconds REAL NOT NULL,
    expected_width INTEGER,
    expected_height INTEGER,
    expected_frame_rate REAL,
    output_directory TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','rendered','missing','failed','ingested')),
    PRIMARY KEY(export_plan_id, stock_clip_id),
    UNIQUE(batch_id, expected_basename)
);

CREATE TABLE IF NOT EXISTS rendered_masters (
    id TEXT PRIMARY KEY,
    export_plan_id TEXT NOT NULL REFERENCES master_export_plans(id),
    batch_id TEXT NOT NULL REFERENCES master_export_batches(id),
    stock_clip_id TEXT NOT NULL UNIQUE REFERENCES reconstructed_candidates(stock_clip_id),
    exported_filename TEXT NOT NULL,
    exported_path TEXT NOT NULL UNIQUE,
    file_size_bytes INTEGER NOT NULL,
    duration_seconds REAL,
    width INTEGER,
    height INTEGER,
    frame_rate REAL,
    codec_name TEXT,
    sha256 TEXT NOT NULL,
    receipt_path TEXT,
    ingested_at TEXT NOT NULL
);

CREATE VIEW IF NOT EXISTS vclip_pool_catalog AS
SELECT
    c.stock_clip_id,
    c.project_name,
    c.event_name,
    c.product_role,
    c.candidate_tier,
    c.source_name,
    c.source_path,
    c.source_start_seconds,
    c.duration_seconds,
    c.width,
    c.height,
    c.frame_rate,
    c.orientation,
    c.readiness_basis,
    c.qc_status,
    c.operator_status,
    c.visual_status,
    c.camera_lut,
    c.effect_signature,
    c.location_json,
    c.capture_time_json,
    c.expected_export_basename,
    c.active,
    c.duplicate_of,
    CASE WHEN r.stock_clip_id IS NULL THEN 0 ELSE 1 END AS exported,
    r.exported_path,
    r.sha256,
    r.ingested_at
FROM reconstructed_candidates c
LEFT JOIN rendered_masters r USING(stock_clip_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    ensure_fts(con)
    return con


def ensure_fts(con: sqlite3.Connection) -> None:
    try:
        con.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS reconstructed_candidates_fts
            USING fts5(
                stock_clip_id UNINDEXED,
                project_name,
                event_name,
                source_name,
                location_text,
                product_role,
                candidate_tier,
                orientation,
                tokenize='unicode61 remove_diacritics 2'
            )
            """
        )
    except sqlite3.OperationalError:
        pass


def rebuild_fts(con: sqlite3.Connection) -> None:
    try:
        con.execute("DELETE FROM reconstructed_candidates_fts")
        rows = con.execute(
            """
            SELECT stock_clip_id, project_name, event_name, source_name,
                   location_json, product_role, candidate_tier, orientation
            FROM reconstructed_candidates
            WHERE active=1
            """
        ).fetchall()
        con.executemany(
            """
            INSERT INTO reconstructed_candidates_fts(
                stock_clip_id, project_name, event_name, source_name,
                location_text, product_role, candidate_tier, orientation
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            [
                (
                    row["stock_clip_id"],
                    row["project_name"],
                    row["event_name"],
                    row["source_name"],
                    location_label(row["location_json"]),
                    row["product_role"],
                    row["candidate_tier"],
                    row["orientation"],
                )
                for row in rows
            ],
        )
    except sqlite3.OperationalError:
        pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def slug(value: str, limit: int = 110) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    text = re.sub(r"_+", "_", text)
    return (text or "CLIP")[:limit]


def tier(duration: float) -> str:
    if duration >= 10.0:
        return "A Clean 10s+"
    if duration >= 5.0:
        return "B Clean 5–9.99s"
    if duration >= 3.0:
        return "C Clean 3–4.99s"
    return "Reject"


def file_url_to_path(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme and parsed.scheme != "file":
        return None
    raw = parsed.path if parsed.scheme == "file" else value
    return str(Path(unquote(raw))) if raw else None


def source_path(asset: ET.Element | None) -> str | None:
    if asset is None:
        return None
    for child in list(asset):
        if local_name(child.tag) == "media-rep":
            path = file_url_to_path(child.get("src"))
            if path:
                return path
    return None


def fps_from_format(format_element: ET.Element | None) -> float | None:
    if format_element is None or not format_element.get("frameDuration"):
        return None
    duration = float(parse_time(format_element.get("frameDuration")))
    return 1.0 / duration if duration > 0 else None


def parse_active_xmls(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.fcpxml")):
        relative = path.relative_to(root).as_posix()
        tree = ET.parse(path)
        xml_root = tree.getroot()
        resources = first_direct_child(xml_root, "resources")
        if resources is None:
            continue
        index = build_resource_index(resources)
        for event in xml_root.iter():
            if local_name(event.tag) != "event":
                continue
            event_name = event.get("name") or ""
            event_lower = event_name.casefold()
            if "ready cuts" in event_lower:
                role = "ready_cut"
            elif "extended masters" in event_lower:
                role = "extended_master"
            else:
                continue
            for project in list(event):
                if local_name(project.tag) != "project":
                    continue
                clip = next(
                    (node for node in project.iter() if local_name(node.tag) == "asset-clip"),
                    None,
                )
                if clip is None:
                    continue
                meta = read_vclip_metadata(clip)
                stock_id = meta.get("com.vclip.stock_clip_id")
                if not stock_id:
                    continue
                parents = [
                    value.strip()
                    for value in meta.get("com.vclip.telemetry.parent_ids", stock_id).split(",")
                    if value.strip()
                ]
                ref = clip.get("ref") or ""
                asset = index.get(ref)
                sequence = first_direct_child(project, "sequence")
                fmt = index.get(sequence.get("format") or "") if sequence is not None else None
                width = int(fmt.get("width")) if fmt is not None and fmt.get("width") else None
                height = int(fmt.get("height")) if fmt is not None and fmt.get("height") else None
                fps = fps_from_format(fmt)
                orientation = (
                    "vertical"
                    if width and height and height > width
                    else "landscape"
                    if width and height
                    else "unknown"
                )
                start = float(meta.get("com.vclip.telemetry.source_start_s") or float(parse_time(clip.get("start"))))
                duration = float(meta.get("com.vclip.telemetry.duration_s") or float(parse_time(clip.get("duration"))))
                project_name = project.get("name") or stock_id
                expected = f"{stock_id}__{slug(project_name)}"
                rows.append(
                    {
                        "stock_clip_id": stock_id,
                        "parent_ids": sorted(set(parents)),
                        "shard_path": relative,
                        "event_name": event_name,
                        "project_name": project_name,
                        "product_role": role,
                        "source_name": clip.get("name") or (asset.get("name") if asset is not None else ""),
                        "source_path": source_path(asset),
                        "source_start_seconds": start,
                        "duration_seconds": duration,
                        "width": width,
                        "height": height,
                        "frame_rate": fps,
                        "orientation": orientation,
                        "candidate_tier": tier(duration),
                        "readiness_basis": meta.get("com.vclip.readiness_basis"),
                        "qc_status": meta.get("com.vclip.telemetry.qc_status"),
                        "operator_status": meta.get("com.vclip.telemetry.operator_status"),
                        "visual_status": meta.get("com.vclip.visual.status"),
                        "camera_lut": asset.get("customLUTOverride") if asset is not None else None,
                        "effect_signature": video_treatment_signature(clip),
                        "expected_export_basename": expected,
                    }
                )
    return rows


def parent_metadata(con: sqlite3.Connection, parent_ids: list[str]) -> dict[str, Any]:
    if not parent_ids or not table_exists(con, "stock_candidates"):
        return {}
    placeholders = ",".join("?" for _ in parent_ids)
    query = f"""
        WITH ranked AS (
            SELECT sc.*,
                   ROW_NUMBER() OVER (
                     PARTITION BY stock_clip_id
                     ORDER BY COALESCE(updated_at, created_at, '') DESC, rowid DESC
                   ) rn
            FROM stock_candidates sc
            WHERE stock_clip_id IN ({placeholders})
              AND eligibility_status='accepted'
        )
        SELECT * FROM ranked WHERE rn=1
    """
    rows = con.execute(query, parent_ids).fetchall()
    if not rows:
        return {}
    # Prefer the first row with real location data; parent IDs are already stable.
    chosen = next(
        (row for row in rows if row["location_json"] not in (None, "", "{}")),
        rows[0],
    )
    return {
        "location_json": chosen["location_json"] or "{}",
        "capture_time_json": chosen["capture_time_json"] or "{}",
        "camera_lut": chosen["camera_lut"],
    }


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    )


def location_label(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
    except Exception:
        return ""

    preferred = (
        "public_label",
        "structured_location_label",
        "location_label",
        "place_label",
        "neighborhood",
        "city",
        "state",
        "country",
    )

    def walk(value: Any) -> str | None:
        if isinstance(value, dict):
            for key in preferred:
                item = value.get(key)
                if isinstance(item, str) and item.strip():
                    return item.strip()
            for child in value.values():
                found = walk(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found:
                    return found
        return None

    return walk(obj) or ""


def import_corpus(args: argparse.Namespace) -> int:
    con = connect(args.db)
    manifest_bytes = args.manifest.read_bytes()
    run_id = f"RECON_{hashlib.sha256(manifest_bytes).hexdigest()[:16].upper()}"
    manifest = json.loads(manifest_bytes)
    source_root = Path(manifest["output_root"]).expanduser().resolve()
    rows = parse_active_xmls(source_root)
    by_id: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_id.setdefault(row["stock_clip_id"], []).append(row)
    duplicate_ids = {stock_id: items for stock_id, items in by_id.items() if len(items) > 1}
    if duplicate_ids:
        details = []
        for stock_id, items in list(sorted(duplicate_ids.items()))[:20]:
            details.append(
                stock_id + ": " + "; ".join(
                    f"{item['shard_path']}::{item['project_name']}::{item['effect_signature'][:12]}"
                    for item in items
                )
            )
        raise RuntimeError(
            "Active reconstructed corpus contains duplicate stable VClip IDs. "
            "Refusing to overwrite variants in SQLite:\n" + "\n".join(details)
        )
    active_ids = set(by_id)
    created = now()

    counts = {
        "active_candidates": len(rows),
        "ready_cuts": sum(row["product_role"] == "ready_cut" for row in rows),
        "extended_masters": sum(row["product_role"] == "extended_master" for row in rows),
        "dedupe_removals": len(manifest.get("dedupe_removals", [])),
    }
    con.execute(
        """
        INSERT INTO reconstruction_runs(
            id, manifest_path, source_root, pipeline_version, status,
            settings_json, counts_json, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            manifest_path=excluded.manifest_path,
            source_root=excluded.source_root,
            status='complete',
            counts_json=excluded.counts_json,
            updated_at=excluded.updated_at
        """,
        (
            run_id,
            str(args.manifest.resolve()),
            str(source_root),
            "reconstruction-v3-source-vision",
            "complete",
            json.dumps({"manifest_schema_version": manifest.get("schema_version")}),
            json.dumps(counts),
            created,
            created,
        ),
    )

    # Mark prior imported candidates inactive; this manifest is the current pool.
    con.execute("UPDATE reconstructed_candidates SET active=0, updated_at=?", (created,))

    for row in rows:
        inherited = parent_metadata(con, row["parent_ids"])
        location_json = inherited.get("location_json", "{}")
        capture_json = inherited.get("capture_time_json", "{}")
        camera_lut = row.get("camera_lut") or inherited.get("camera_lut")
        con.execute(
            """
            INSERT INTO reconstructed_candidates(
                stock_clip_id, reconstruction_run_id, shard_path, event_name,
                project_name, product_role, source_name, source_path,
                source_start_seconds, duration_seconds, width, height, frame_rate,
                orientation, candidate_tier, readiness_basis, qc_status,
                operator_status, visual_status, camera_lut, effect_signature,
                location_json, capture_time_json, expected_export_basename,
                active, duplicate_of, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(stock_clip_id) DO UPDATE SET
                reconstruction_run_id=excluded.reconstruction_run_id,
                shard_path=excluded.shard_path,
                event_name=excluded.event_name,
                project_name=excluded.project_name,
                product_role=excluded.product_role,
                source_name=excluded.source_name,
                source_path=excluded.source_path,
                source_start_seconds=excluded.source_start_seconds,
                duration_seconds=excluded.duration_seconds,
                width=excluded.width,
                height=excluded.height,
                frame_rate=excluded.frame_rate,
                orientation=excluded.orientation,
                candidate_tier=excluded.candidate_tier,
                readiness_basis=excluded.readiness_basis,
                qc_status=excluded.qc_status,
                operator_status=excluded.operator_status,
                visual_status=excluded.visual_status,
                camera_lut=excluded.camera_lut,
                effect_signature=excluded.effect_signature,
                location_json=excluded.location_json,
                capture_time_json=excluded.capture_time_json,
                expected_export_basename=excluded.expected_export_basename,
                active=1,
                duplicate_of=NULL,
                updated_at=excluded.updated_at
            """,
            (
                row["stock_clip_id"],
                run_id,
                row["shard_path"],
                row["event_name"],
                row["project_name"],
                row["product_role"],
                row["source_name"],
                row["source_path"],
                row["source_start_seconds"],
                row["duration_seconds"],
                row["width"],
                row["height"],
                row["frame_rate"],
                row["orientation"],
                row["candidate_tier"],
                row["readiness_basis"],
                row["qc_status"],
                row["operator_status"],
                row["visual_status"],
                camera_lut,
                row["effect_signature"],
                location_json,
                capture_json,
                row["expected_export_basename"],
                1,
                None,
                created,
                created,
            ),
        )
        con.execute(
            "DELETE FROM reconstructed_candidate_parents WHERE stock_clip_id=?",
            (row["stock_clip_id"],),
        )
        con.executemany(
            "INSERT OR IGNORE INTO reconstructed_candidate_parents VALUES(?,?)",
            [(row["stock_clip_id"], parent) for parent in row["parent_ids"]],
        )

    con.execute(
        "DELETE FROM reconstruction_dedupe_removals WHERE reconstruction_run_id=?",
        (run_id,),
    )
    for removal in manifest.get("dedupe_removals", []):
        con.execute(
            """
            INSERT OR REPLACE INTO reconstruction_dedupe_removals(
                reconstruction_run_id, removed_stock_clip_id,
                canonical_stock_clip_id, reason, containment, iou, source_name,
                removed_project_name, kept_project_name, provenance_json, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                removal.get("removed_stock_clip_id"),
                removal.get("canonical_stock_clip_id"),
                removal.get("reason"),
                removal.get("containment"),
                removal.get("iou"),
                removal.get("source_name"),
                removal.get("removed_project_name"),
                removal.get("kept_project_name"),
                json.dumps(removal, sort_keys=True),
                created,
            ),
        )

    rebuild_fts(con)
    con.commit()
    print("Reconstructed corpus imported")
    print("=============================")
    print(f"Run ID:           {run_id}")
    print(f"Active candidates:{len(rows):8d}")
    print(f"Ready Cuts:       {counts['ready_cuts']:8d}")
    print(f"Extended Masters: {counts['extended_masters']:8d}")
    print(f"Dedupe removals:  {counts['dedupe_removals']:8d}")
    print(f"DB:               {args.db}")
    return 0


def register_plan(args: argparse.Namespace) -> int:
    con = connect(args.db)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    plan_id = manifest["plan_id"]
    created = now()
    con.execute(
        """
        INSERT INTO master_export_plans(
            id, reconstruction_run_id, manifest_path, render_root,
            share_destination, status, batch_count, item_count,
            created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            manifest_path=excluded.manifest_path,
            render_root=excluded.render_root,
            share_destination=excluded.share_destination,
            batch_count=excluded.batch_count,
            item_count=excluded.item_count,
            updated_at=excluded.updated_at
        """,
        (
            plan_id,
            manifest["reconstruction_run_id"],
            str(args.manifest.resolve()),
            manifest["render_root"],
            manifest["share_destination"],
            "planned",
            len(manifest["batches"]),
            len(manifest["items"]),
            created,
            created,
        ),
    )
    con.execute("DELETE FROM master_export_batches WHERE export_plan_id=?", (plan_id,))
    for batch in manifest["batches"]:
        con.execute(
            """
            INSERT INTO master_export_batches(
                id, export_plan_id, batch_index, xml_path, event_name,
                output_directory, expected_count, status
            ) VALUES(?,?,?,?,?,?,?,'planned')
            """,
            (
                batch["batch_id"],
                plan_id,
                batch["batch_index"],
                batch["xml_path"],
                batch["event_name"],
                batch["output_directory"],
                batch["expected_count"],
            ),
        )
    con.execute("DELETE FROM master_export_items WHERE export_plan_id=?", (plan_id,))
    for item in manifest["items"]:
        con.execute(
            """
            INSERT INTO master_export_items(
                export_plan_id, batch_id, stock_clip_id, expected_basename,
                expected_duration_seconds, expected_width, expected_height,
                expected_frame_rate, output_directory, status
            ) VALUES(?,?,?,?,?,?,?,?,?,'planned')
            """,
            (
                plan_id,
                item["batch_id"],
                item["stock_clip_id"],
                item["expected_basename"],
                item["duration_seconds"],
                item.get("width"),
                item.get("height"),
                item.get("frame_rate"),
                item["output_directory"],
            ),
        )
    con.commit()
    print(f"Registered export plan {plan_id}: {len(manifest['batches'])} batches, {len(manifest['items'])} items")
    return 0


def probe(path: Path, ffprobe: str) -> dict[str, Any]:
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,codec_name,r_frame_rate:format=duration",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed")
    data = json.loads(result.stdout)
    stream = (data.get("streams") or [{}])[0]
    duration = float((data.get("format") or {}).get("duration") or 0)
    rate = stream.get("r_frame_rate") or "0/1"
    num, den = rate.split("/", 1)
    fps = float(num) / float(den) if float(den) else 0.0
    return {
        "duration_seconds": duration,
        "width": stream.get("width"),
        "height": stream.get("height"),
        "frame_rate": fps,
        "codec_name": stream.get("codec_name"),
    }


def ingest_exports(args: argparse.Namespace) -> int:
    con = connect(args.db)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    plan_id = manifest["plan_id"]
    mismatches: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    ingested = 0
    created = now()

    for item in manifest["items"]:
        output_dir = Path(item["output_directory"])
        candidates = [
            path
            for path in output_dir.glob(item["expected_basename"] + ".*")
            if path.suffix.lower() in VIDEO_EXTENSIONS and path.is_file()
        ]
        if not candidates:
            missing.append(item)
            continue
        if len(candidates) > 1:
            ambiguous.append({"item": item, "files": [str(path) for path in candidates]})
            continue
        path = candidates[0]
        media = probe(path, args.ffprobe)
        expected_duration = float(item["duration_seconds"])
        fps = float(item.get("frame_rate") or media.get("frame_rate") or 30.0)
        tolerance = max(args.duration_tolerance, 2.0 / max(1.0, fps))
        delta = abs(float(media["duration_seconds"]) - expected_duration)
        if delta > tolerance:
            mismatches.append(
                {
                    "stock_clip_id": item["stock_clip_id"],
                    "file": str(path),
                    "expected_duration": expected_duration,
                    "actual_duration": media["duration_seconds"],
                    "difference": delta,
                    "tolerance": tolerance,
                }
            )
            continue
        checksum = sha256_file(path)
        render_id = f"MASTER_{hashlib.sha256((plan_id + '|' + item['stock_clip_id']).encode()).hexdigest()[:20].upper()}"
        receipt = Path(manifest["receipt_root"]) / f"{item['batch_id']}.json"
        con.execute(
            """
            INSERT INTO rendered_masters(
                id, export_plan_id, batch_id, stock_clip_id,
                exported_filename, exported_path, file_size_bytes,
                duration_seconds, width, height, frame_rate, codec_name,
                sha256, receipt_path, ingested_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(stock_clip_id) DO UPDATE SET
                export_plan_id=excluded.export_plan_id,
                batch_id=excluded.batch_id,
                exported_filename=excluded.exported_filename,
                exported_path=excluded.exported_path,
                file_size_bytes=excluded.file_size_bytes,
                duration_seconds=excluded.duration_seconds,
                width=excluded.width,
                height=excluded.height,
                frame_rate=excluded.frame_rate,
                codec_name=excluded.codec_name,
                sha256=excluded.sha256,
                receipt_path=excluded.receipt_path,
                ingested_at=excluded.ingested_at
            """,
            (
                render_id,
                plan_id,
                item["batch_id"],
                item["stock_clip_id"],
                path.name,
                str(path.resolve()),
                path.stat().st_size,
                media["duration_seconds"],
                media["width"],
                media["height"],
                media["frame_rate"],
                media["codec_name"],
                checksum,
                str(receipt) if receipt.is_file() else None,
                created,
            ),
        )
        con.execute(
            "UPDATE master_export_items SET status='ingested' WHERE export_plan_id=? AND stock_clip_id=?",
            (plan_id, item["stock_clip_id"]),
        )
        ingested += 1

    report = {
        "plan_id": plan_id,
        "items": len(manifest["items"]),
        "ingested": ingested,
        "missing": missing,
        "ambiguous": ambiguous,
        "duration_mismatches": mismatches,
        "generated_at": created,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if missing or ambiguous or mismatches:
        con.rollback()
        print(f"Ingest blocked: missing={len(missing)} ambiguous={len(ambiguous)} duration_mismatches={len(mismatches)}")
        print(f"Report: {args.report}")
        return 2

    con.execute(
        "UPDATE master_export_plans SET status='ingested', updated_at=? WHERE id=?",
        (created, plan_id),
    )
    con.commit()
    print(f"Ingested {ingested} rendered masters into {args.db}")
    print(f"Report: {args.report}")
    return 0


def stats(args: argparse.Namespace) -> int:
    con = connect(args.db)
    rows = con.execute(
        """
        SELECT product_role, candidate_tier, orientation, exported, COUNT(*) n,
               ROUND(SUM(duration_seconds)/60.0, 2) minutes
        FROM vclip_pool_catalog
        WHERE active=1
        GROUP BY product_role, candidate_tier, orientation, exported
        ORDER BY product_role, candidate_tier, orientation, exported
        """
    ).fetchall()
    total = con.execute(
        "SELECT COUNT(*) n, ROUND(SUM(duration_seconds)/60.0,2) minutes FROM vclip_pool_catalog WHERE active=1"
    ).fetchone()
    print("VCLIP POOL STATS")
    print("================")
    print(f"Active candidates: {total['n']:,}")
    print(f"Total duration:    {total['minutes'] or 0:.2f} min")
    print()
    for row in rows:
        print(
            f"{row['product_role']:16s} {row['candidate_tier']:18s} "
            f"{row['orientation']:10s} exported={row['exported']} "
            f"count={row['n']:5d} duration={row['minutes'] or 0:8.2f} min"
        )
    return 0


def search(args: argparse.Namespace) -> int:
    con = connect(args.db)
    where = ["c.active=1"]
    params: list[Any] = []
    if args.role:
        where.append("c.product_role=?")
        params.append(args.role)
    if args.tier:
        where.append("c.candidate_tier=?")
        params.append(args.tier)
    if args.orientation:
        where.append("c.orientation=?")
        params.append(args.orientation)
    if args.exported is not None:
        where.append("c.exported=?")
        params.append(1 if args.exported else 0)
    if args.location:
        where.append("lower(c.location_json) LIKE ?")
        params.append("%" + args.location.casefold() + "%")

    query = args.query.strip()
    use_fts = bool(query and table_exists(con, "reconstructed_candidates_fts"))
    if use_fts:
        sql = f"""
            SELECT c.*
            FROM vclip_pool_catalog c
            JOIN reconstructed_candidates_fts
              ON reconstructed_candidates_fts.stock_clip_id = c.stock_clip_id
            WHERE {' AND '.join(where)}
              AND reconstructed_candidates_fts MATCH ?
            ORDER BY bm25(reconstructed_candidates_fts), c.duration_seconds DESC
            LIMIT ?
        """
        params.extend([query, args.limit])
        try:
            rows = con.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            use_fts = False
    if not use_fts:
        if query:
            where.append(
                "(lower(c.project_name) LIKE ? OR lower(c.event_name) LIKE ? OR lower(c.source_name) LIKE ? OR lower(c.location_json) LIKE ?)"
            )
            token = "%" + query.casefold() + "%"
            params.extend([token, token, token, token])
        params.append(args.limit)
        rows = con.execute(
            f"SELECT c.* FROM vclip_pool_catalog c WHERE {' AND '.join(where)} ORDER BY c.duration_seconds DESC LIMIT ?",
            params,
        ).fetchall()

    payload = [dict(row) for row in rows]
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    for row in payload:
        print(
            f"{row['stock_clip_id']}  {row['product_role']:15s} "
            f"{row['duration_seconds']:6.2f}s  {row['orientation']:9s} "
            f"exported={row['exported']}"
        )
        print(f"  {row['project_name']}")
        label = location_label(row.get("location_json"))
        if label:
            print(f"  {label}")
        if row.get("exported_path"):
            print(f"  {row['exported_path']}")
    return 0


def init(args: argparse.Namespace) -> int:
    con = connect(args.db)
    con.commit()
    print(f"Initialized reconstructed VClip pool tables in {args.db}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, required=True)
    sub = p.add_subparsers(dest="command", required=True)

    cmd = sub.add_parser("init")
    cmd.set_defaults(handler=init)

    cmd = sub.add_parser("import-corpus")
    cmd.add_argument("--manifest", type=Path, required=True)
    cmd.set_defaults(handler=import_corpus)

    cmd = sub.add_parser("register-plan")
    cmd.add_argument("--manifest", type=Path, required=True)
    cmd.set_defaults(handler=register_plan)

    cmd = sub.add_parser("ingest-exports")
    cmd.add_argument("--manifest", type=Path, required=True)
    cmd.add_argument("--report", type=Path, required=True)
    cmd.add_argument("--ffprobe", default="ffprobe")
    cmd.add_argument("--duration-tolerance", type=float, default=0.25)
    cmd.set_defaults(handler=ingest_exports)

    cmd = sub.add_parser("stats")
    cmd.set_defaults(handler=stats)

    cmd = sub.add_parser("search")
    cmd.add_argument("query", nargs="?", default="")
    cmd.add_argument("--role", choices=("ready_cut", "extended_master"))
    cmd.add_argument("--tier")
    cmd.add_argument("--orientation", choices=("landscape", "vertical", "unknown"))
    cmd.add_argument("--location")
    cmd.add_argument("--exported", action=argparse.BooleanOptionalAction, default=None)
    cmd.add_argument("--limit", type=int, default=50)
    cmd.add_argument("--json", action="store_true")
    cmd.set_defaults(handler=search)
    return p


def main() -> int:
    args = parser().parse_args()
    args.db = args.db.expanduser().resolve()
    if hasattr(args, "manifest"):
        args.manifest = args.manifest.expanduser().resolve()
    if hasattr(args, "report"):
        args.report = args.report.expanduser().resolve()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
