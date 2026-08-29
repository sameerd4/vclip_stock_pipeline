#!/usr/bin/env python3
"""
Build an isolated production corpus + SQLite catalog for OpenAI-approved
VClip MASTER REVIEW promotions.

This is intentionally non-destructive:
- reads promotion-ready.csv
- reads the reconstructed FCPXMLs referenced by that manifest
- reads the existing VClip SQLite DB
- writes NEW promotion-only FCPXML files
- writes a NEW isolated SQLite DB via SQLite backup
- deactivates old reconstructed candidates ONLY in the isolated DB copy
- registers promoted projects as active extended_master candidates

The original reconstructed XML corpus and main vclip.sqlite3 are never modified.

Expected input is the output of vclip_finalize_openai_qc_manifest.py.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import re
import sqlite3
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

try:
    from vclip_pipeline.stockify.core import local_name, stable_uid
    from vclip_pipeline.stockify.fcpxml import (
        add_vclip_metadata,
        first_direct_child,
        read_vclip_metadata,
        validate_fcpxml,
        video_treatment_signature,
        write_fcpxml,
    )
except Exception as exc:
    raise SystemExit(
        "Could not import VClip. Run with "
        "PYTHONPATH=<repo>/src:<repo>/scripts. "
        f"Import error: {exc}"
    )


RECOVERY_VERSION = "qc-openai-promotion-v1"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def project_stock_id(project: ET.Element) -> str:
    clip = next(
        (node for node in project.iter() if local_name(node.tag) == "asset-clip"),
        None,
    )
    if clip is None:
        return ""
    sid = read_vclip_metadata(clip).get("com.vclip.stock_clip_id")
    if sid:
        return sid
    blob = ET.tostring(project, encoding="unicode")
    match = re.search(r"VCLIP_[0-9A-F]{12,64}", blob)
    return match.group(0) if match else ""


def primary_clip(project: ET.Element) -> ET.Element:
    clip = next(
        (node for node in project.iter() if local_name(node.tag) == "asset-clip"),
        None,
    )
    if clip is None:
        raise RuntimeError(f"Project has no asset-clip: {project.get('name')!r}")
    return clip


def parse_seconds(value: str | None) -> Fraction:
    if not value:
        return Fraction(0, 1)
    text = value.strip()
    if text.endswith("s"):
        text = text[:-1]
    if "/" in text:
        a, b = text.split("/", 1)
        return Fraction(int(a), int(b))
    return Fraction(text)


def format_info(project: ET.Element, resources: ET.Element) -> tuple[int | None, int | None, float | None, str | None]:
    index = {
        child.get("id"): child
        for child in list(resources)
        if child.get("id")
    }
    sequence = first_direct_child(project, "sequence")
    if sequence is None:
        return None, None, None, None
    fmt = index.get(sequence.get("format") or "")
    if fmt is None:
        return None, None, None, None

    def as_int(value: str | None) -> int | None:
        try:
            return int(value or "")
        except Exception:
            return None

    width = as_int(fmt.get("width"))
    height = as_int(fmt.get("height"))
    frame_duration = fmt.get("frameDuration")
    fps = None
    if frame_duration:
        seconds = parse_seconds(frame_duration)
        if seconds > 0:
            fps = float(1 / seconds)

    orientation = None
    if width and height:
        if width > height:
            orientation = "landscape"
        elif height > width:
            orientation = "portrait"
        else:
            orientation = "square"

    return width, height, fps, orientation


def file_url_path(value: str | None) -> Path | None:
    if not value:
        return None
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme == "file":
        return Path(urllib.parse.unquote(parsed.path))
    if parsed.scheme:
        return None
    return Path(urllib.parse.unquote(value)).expanduser()


def source_path_for_clip(clip: ET.Element, resources: ET.Element) -> Path | None:
    index = {
        child.get("id"): child
        for child in list(resources)
        if child.get("id")
    }
    resource = index.get(clip.get("ref") or "")
    if resource is None:
        return None

    ranked: list[tuple[int, Path]] = []

    def add(value: str | None, rank: int) -> None:
        path = file_url_path(value)
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
    if not ranked:
        return None

    for _rank, path in ranked:
        if path.is_file():
            return path
    return ranked[0][1]


def clean_project_name(value: str, stock_id: str) -> str:
    name = value.strip()
    name = re.sub(r"^MASTER REVIEW\s+—\s+", "", name, flags=re.I)
    name = re.sub(r"^QC ORIGINAL\s+—\s+", "", name, flags=re.I)
    if not name:
        name = stock_id
    return name


def expected_basename(stock_id: str, project_name: str) -> str:
    # Export-plan/AX names are ASCII and always begin with immutable VCLIP ID.
    tail = project_name.encode("ascii", "ignore").decode("ascii")
    tail = re.sub(r"[^A-Za-z0-9]+", "_", tail).strip("_")
    tail = tail[:150].rstrip("_")
    if not tail:
        tail = "QC_Recovered_Master"
    return f"{stock_id}__{tail}"


def tier_for(duration: float) -> str:
    if duration >= 10:
        return "QC Recovery A Clean 10s+"
    return "QC Recovery A Clean 5-10s"


def clone_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()

    src = sqlite3.connect(source)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def build_promoted_project(
    original: ET.Element,
    *,
    stock_id: str,
) -> tuple[ET.Element, str]:
    project = copy.deepcopy(original)
    original_name = project.get("name") or stock_id
    clean_name = clean_project_name(original_name, stock_id)
    project.set("name", clean_name)
    project.set(
        "uid",
        stable_uid("vclip-qc-openai-promoted-project", stock_id, clean_name),
    )

    clip = primary_clip(project)
    add_vclip_metadata(
        clip,
        {
            "com.vclip.qc_recovery.version": RECOVERY_VERSION,
            "com.vclip.qc_recovery.promoted": "1",
            "com.vclip.qc_recovery.source_bucket": "master_review",
            "com.vclip.qc_recovery.visual_gate": "openai_production_qc",
        },
    )
    return project, clean_name


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--promotion-csv", type=Path, required=True)
    p.add_argument("--source-xml-root", type=Path, required=True)
    p.add_argument("--source-db", type=Path, required=True)
    p.add_argument("--output-root", type=Path, required=True)
    return p


def main() -> int:
    args = parser().parse_args()

    promotion_csv = args.promotion_csv.expanduser().resolve()
    source_xml_root = args.source_xml_root.expanduser().resolve()
    source_db = args.source_db.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()

    xml_out_root = output_root / "xml"
    isolated_db = output_root / "vclip-qc-promotion.sqlite3"
    registry_csv = output_root / "promotion-registry.csv"
    summary_json = output_root / "summary.json"

    rows = read_csv(promotion_csv)
    if not rows:
        raise SystemExit("Promotion CSV is empty")

    by_id = {row["stock_clip_id"]: row for row in rows}
    if len(by_id) != len(rows):
        raise SystemExit("Duplicate stock_clip_id in promotion CSV")

    if any((row.get("final_bucket") or "") != "PROMOTION_READY" for row in rows):
        bad = [
            row["stock_clip_id"]
            for row in rows
            if (row.get("final_bucket") or "") != "PROMOTION_READY"
        ]
        raise SystemExit(
            "Promotion CSV contains non-PROMOTION_READY rows: "
            + ", ".join(bad[:20])
        )

    xml_groups: dict[Path, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        xml_path = Path(row["xml_path"]).expanduser().resolve()
        if not xml_path.is_file():
            raise SystemExit(f"Missing source FCPXML: {xml_path}")
        try:
            xml_path.relative_to(source_xml_root)
        except ValueError:
            raise SystemExit(
                f"Source XML is outside --source-xml-root: {xml_path}"
            )
        xml_groups[xml_path].append(row)

    # Reuse the current reconstruction run ID in the isolated DB. The export
    # planner only requires all active candidates to belong to exactly one run.
    src_con = sqlite3.connect(source_db)
    src_con.row_factory = sqlite3.Row
    active_runs = src_con.execute(
        """
        SELECT DISTINCT reconstruction_run_id
        FROM reconstructed_candidates
        WHERE active=1
        """
    ).fetchall()
    if len(active_runs) != 1:
        raise SystemExit(
            f"Expected one active reconstruction run in source DB, got {len(active_runs)}"
        )
    reconstruction_run_id = str(active_runs[0]["reconstruction_run_id"])
    src_con.close()

    output_root.mkdir(parents=True, exist_ok=True)
    xml_out_root.mkdir(parents=True, exist_ok=True)

    registry: list[dict[str, Any]] = []
    written_xmls: list[str] = []

    print("QC OPENAI PROMOTION CORPUS")
    print("==========================")
    print("promotion IDs :", len(rows))
    print("source XMLs   :", len(xml_groups))
    print("source run ID :", reconstruction_run_id)
    print()

    selected_total = 0

    for source_xml, selected_rows in sorted(
        xml_groups.items(), key=lambda pair: str(pair[0])
    ):
        wanted = {row["stock_clip_id"] for row in selected_rows}

        tree = ET.parse(source_xml)
        source_root = tree.getroot()
        resources = first_direct_child(source_root, "resources")
        library = next(
            (
                node
                for node in source_root.iter()
                if local_name(node.tag) == "library"
            ),
            None,
        )
        if resources is None or library is None:
            raise RuntimeError(f"Malformed reconstructed FCPXML: {source_xml}")

        found: dict[str, tuple[str, ET.Element]] = {}
        for event in list(library):
            if local_name(event.tag) != "event":
                continue
            event_name = event.get("name") or ""
            if "qc review" not in event_name.casefold():
                continue
            for project in list(event):
                if local_name(project.tag) != "project":
                    continue
                sid = project_stock_id(project)
                if sid in wanted:
                    found[sid] = (event_name, project)

        missing = sorted(wanted - set(found))
        if missing:
            raise RuntimeError(
                f"{source_xml}: missing promoted IDs in QC Review: "
                + ", ".join(missing[:20])
            )

        new_root = copy.deepcopy(source_root)
        new_resources = first_direct_child(new_root, "resources")
        new_library = next(
            (
                node
                for node in new_root.iter()
                if local_name(node.tag) == "library"
            ),
            None,
        )
        assert new_resources is not None and new_library is not None

        for event in list(new_library):
            if local_name(event.tag) == "event":
                new_library.remove(event)

        # Group selected projects by original scope so event names remain useful.
        scoped: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in selected_rows:
            scoped[row.get("scope") or "QC Recovery"].append(row)

        # Build a lookup from copied resources. Projects from this XML retain
        # their existing refs, so the source resource table remains valid.
        for scope, scope_rows in sorted(scoped.items()):
            event_name = (
                f"{scope} — Extended Masters — OpenAI QC Recovery v1"
            )
            event = ET.SubElement(
                new_library,
                "event",
                {
                    "name": event_name,
                    "uid": stable_uid(
                        "vclip-qc-openai-promoted-event",
                        str(source_xml),
                        event_name,
                    ),
                },
            )

            for row in sorted(scope_rows, key=lambda r: r["stock_clip_id"]):
                sid = row["stock_clip_id"]
                _old_event, original_project = found[sid]
                project, clean_name = build_promoted_project(
                    original_project,
                    stock_id=sid,
                )
                event.append(project)

                clip = primary_clip(project)
                width, height, frame_rate, orientation = format_info(
                    project, new_resources
                )
                if width is None or height is None or frame_rate is None:
                    raise RuntimeError(
                        f"Could not derive format for promoted project {sid}"
                    )

                source_path = source_path_for_clip(clip, new_resources)
                if source_path is None:
                    raise RuntimeError(
                        f"Could not resolve source resource for {sid}"
                    )

                duration = float(row["duration_s"])
                start = float(row["start_s"])
                effect_sig = video_treatment_signature(clip)
                metadata = read_vclip_metadata(clip)

                rel_source_xml = source_xml.relative_to(source_xml_root)
                rel_out = rel_source_xml.with_name(
                    rel_source_xml.stem + "--openai-qc-promoted.fcpxml"
                )

                registry.append(
                    {
                        "stock_clip_id": sid,
                        "reconstruction_run_id": reconstruction_run_id,
                        "shard_path": rel_out.as_posix(),
                        "event_name": event_name,
                        "project_name": clean_name,
                        "product_role": "extended_master",
                        "source_name": clip.get("name") or row.get("source_name") or "",
                        "source_path": str(source_path),
                        "source_start_seconds": start,
                        "duration_seconds": duration,
                        "width": width,
                        "height": height,
                        "frame_rate": frame_rate,
                        "orientation": orientation,
                        "candidate_tier": tier_for(duration),
                        "readiness_basis": "openai_production_qc_clear",
                        "qc_status": "PASS",
                        "operator_status": "CLEAN",
                        "visual_status": row.get("visual_status") or "",
                        "camera_lut": metadata.get("com.vclip.camera_lut") or None,
                        "effect_signature": effect_sig,
                        "location_json": "{}",
                        "capture_time_json": "{}",
                        "expected_export_basename": expected_basename(
                            sid, clean_name
                        ),
                        "active": 1,
                        "duplicate_of": None,
                        "created_at": now(),
                        "updated_at": now(),
                        "source_reconstructed_xml": str(source_xml),
                        "openai_person_presence": row.get("person_presence") or "",
                        "openai_stock_usability": row.get("stock_usability") or "",
                    }
                )
                selected_total += 1

        rel_source_xml = source_xml.relative_to(source_xml_root)
        rel_out = rel_source_xml.with_name(
            rel_source_xml.stem + "--openai-qc-promoted.fcpxml"
        )
        out_xml = xml_out_root / rel_out
        out_xml.parent.mkdir(parents=True, exist_ok=True)

        validation = validate_fcpxml(new_root)
        if not validation.passed:
            raise RuntimeError(
                f"Promoted XML failed validation {source_xml}: "
                f"{validation.errors[:20]}"
            )

        write_fcpxml(new_root, out_xml)
        written_xmls.append(rel_out.as_posix())

    if selected_total != len(rows):
        raise RuntimeError(
            f"Promotion count mismatch: selected={selected_total}, manifest={len(rows)}"
        )

    # Ensure immutable IDs and export basenames are unique before touching DB copy.
    ids = [row["stock_clip_id"] for row in registry]
    basenames = [row["expected_export_basename"] for row in registry]
    if len(set(ids)) != len(ids):
        raise RuntimeError("Duplicate promoted stock_clip_id")
    if len(set(basenames)) != len(basenames):
        raise RuntimeError("Duplicate promoted expected_export_basename")

    clone_database(source_db, isolated_db)

    con = sqlite3.connect(isolated_db)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")

    # Isolation contract: no existing customer candidate stays active in this DB.
    con.execute("UPDATE reconstructed_candidates SET active=0")

    db_columns = [
        "stock_clip_id",
        "reconstruction_run_id",
        "shard_path",
        "event_name",
        "project_name",
        "product_role",
        "source_name",
        "source_path",
        "source_start_seconds",
        "duration_seconds",
        "width",
        "height",
        "frame_rate",
        "orientation",
        "candidate_tier",
        "readiness_basis",
        "qc_status",
        "operator_status",
        "visual_status",
        "camera_lut",
        "effect_signature",
        "location_json",
        "capture_time_json",
        "expected_export_basename",
        "active",
        "duplicate_of",
        "created_at",
        "updated_at",
    ]

    sql = (
        "INSERT INTO reconstructed_candidates("
        + ",".join(db_columns)
        + ") VALUES ("
        + ",".join("?" for _ in db_columns)
        + ")"
    )

    for row in registry:
        con.execute(sql, [row.get(column) for column in db_columns])

    con.commit()

    active = con.execute(
        """
        SELECT product_role, COUNT(*) AS n
        FROM reconstructed_candidates
        WHERE active=1
        GROUP BY product_role
        ORDER BY product_role
        """
    ).fetchall()

    active_total = sum(int(row["n"]) for row in active)
    active_run_count = con.execute(
        """
        SELECT COUNT(DISTINCT reconstruction_run_id)
        FROM reconstructed_candidates
        WHERE active=1
        """
    ).fetchone()[0]

    fk_errors = con.execute("PRAGMA foreign_key_check").fetchall()
    con.close()

    if active_total != len(registry):
        raise RuntimeError(
            f"Isolated DB active count mismatch: {active_total} vs {len(registry)}"
        )
    if active_run_count != 1:
        raise RuntimeError(
            f"Isolated DB has {active_run_count} active reconstruction runs"
        )
    if fk_errors:
        raise RuntimeError(
            f"Isolated DB foreign-key errors: {fk_errors[:20]}"
        )

    registry_fields = list(registry[0].keys())
    with registry_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=registry_fields)
        writer.writeheader()
        writer.writerows(registry)

    orientation_counts = Counter(row["orientation"] for row in registry)
    visual_counts = Counter(row["visual_status"] for row in registry)

    summary = {
        "recovery_version": RECOVERY_VERSION,
        "promotion_count": len(registry),
        "source_xml_count": len(xml_groups),
        "written_xml_count": len(written_xmls),
        "reconstruction_run_id": reconstruction_run_id,
        "isolated_db": str(isolated_db),
        "xml_root": str(xml_out_root),
        "registry_csv": str(registry_csv),
        "active_candidate_count": active_total,
        "active_run_count": active_run_count,
        "foreign_key_error_count": len(fk_errors),
        "orientation_counts": dict(orientation_counts),
        "visual_status_counts": dict(visual_counts),
        "written_xmls": written_xmls,
    }
    summary_json.write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )

    print()
    print("QC PROMOTION PRODUCTION CORPUS")
    print("==============================")
    print("promoted projects :", len(registry))
    print("output XML files  :", len(written_xmls))
    print("active DB rows    :", active_total)
    print("active run count  :", active_run_count)
    print("foreign-key errs  :", len(fk_errors))
    print("XML root          :", xml_out_root)
    print("isolated DB       :", isolated_db)
    print("registry          :", registry_csv)
    print("summary           :", summary_json)
    print()
    print("ACTIVE ROLES")
    print("------------")
    for row in active:
        print(f"{row['n']:4d}  {row['product_role']}")
    print()
    print("ORIENTATION")
    print("-----------")
    for key, value in orientation_counts.most_common():
        print(f"{value:4d}  {key}")
    print()
    print("VISUAL STATUS")
    print("-------------")
    for key, value in visual_counts.most_common():
        print(f"{value:4d}  {key or '(blank)'}")
    print()
    print("QC PROMOTION PRODUCTION CORPUS: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
