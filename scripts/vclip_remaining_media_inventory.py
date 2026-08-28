#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import unquote, urlparse


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def file_path(value):
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme == "file":
        return unquote(parsed.path)
    return unquote(value)


def volume_name(path):
    if not path:
        return "(unknown)"
    parts = Path(path).parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return parts[2]
    return "(internal/other)"


def volume_mounted(path):
    if not path:
        return False
    parts = Path(path).parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return (Path("/Volumes") / parts[2]).exists()
    return True


def read_metadata(node):
    out = {}
    for child in node.iter():
        if local_name(child.tag) != "md":
            continue
        key = child.get("key")
        if key:
            out[key] = child.get("value") or ""
    return out


def xml_sources(xml_path):
    root = ET.parse(xml_path).getroot()

    resources = next(
        (
            x for x in list(root)
            if local_name(x.tag) == "resources"
        ),
        None,
    )

    if resources is None:
        return {}

    resources_by_id = {
        x.get("id"): x
        for x in list(resources)
        if x.get("id")
    }

    result = {}

    for project in root.iter():
        if local_name(project.tag) != "project":
            continue

        stock_id = ""

        for node in project.iter():
            md = read_metadata(node)
            if md.get("com.vclip.stock_clip_id"):
                stock_id = md["com.vclip.stock_clip_id"]
                break

        if not stock_id:
            continue

        paths = set()

        for clip in project.iter():
            if local_name(clip.tag) != "asset-clip":
                continue

            asset = resources_by_id.get(
                clip.get("ref") or ""
            )

            if asset is None:
                continue

            for node in asset.iter():
                if (
                    local_name(node.tag) == "media-rep"
                    and node.get("src")
                ):
                    paths.add(
                        file_path(node.get("src"))
                    )

        result[stock_id] = sorted(
            p for p in paths if p
        )

    return result


def probe(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries",
            (
                "stream=width,height,r_frame_rate,"
                "codec_name,pix_fmt:format=duration"
            ),
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        return None, (
            result.stderr.strip()
            or "ffprobe failed"
        )

    data = json.loads(result.stdout)
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}

    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "frame_rate":
            stream.get("r_frame_rate") or "",
        "codec":
            stream.get("codec_name") or "",
        "pix_fmt":
            stream.get("pix_fmt") or "",
        "duration_seconds":
            float(fmt.get("duration") or 0),
    }, ""


def complete_receipt_ids(batch):
    path = Path(batch["receipt_path"])

    if not path.is_file():
        return []

    try:
        receipt = json.loads(
            path.read_text(encoding="utf-8")
        )
    except Exception:
        return []

    if receipt.get("status") != "complete":
        return []

    return [
        row["stock_clip_id"]
        for row in receipt.get("files", [])
        if row.get("stock_clip_id")
    ]


def build_waves(
    batches,
    max_batches=12,
    max_items=100,
):
    waves = []
    current = []
    count = 0

    for batch in sorted(
        batches,
        key=lambda b: int(b["batch_index"]),
    ):
        n = int(batch["expected_count"])

        if current and (
            len(current) >= max_batches
            or count + n > max_items
        ):
            waves.append(current)
            current = []
            count = 0

        current.append(batch)
        count += n

    if current:
        waves.append(current)

    return waves


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--db",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--expect-exported-products",
        type=int,
    )
    parser.add_argument(
        "--expect-remaining-products",
        type=int,
    )
    args = parser.parse_args()

    manifest = json.loads(
        args.manifest.read_text(
            encoding="utf-8"
        )
    )

    batches = sorted(
        manifest["batches"],
        key=lambda b: int(
            b["batch_index"]
        ),
    )

    batch_by_id = {
        b["batch_id"]: b
        for b in batches
    }

    exported_ids = set()

    for batch in batches:
        exported_ids.update(
            complete_receipt_ids(batch)
        )

    remaining = [
        item
        for item in manifest["items"]
        if item["stock_clip_id"]
        not in exported_ids
    ]

    if (
        args.expect_exported_products
        is not None
        and len(exported_ids)
        != args.expect_exported_products
    ):
        raise SystemExit(
            "BLOCK: exported product count "
            f"is {len(exported_ids)}, expected "
            f"{args.expect_exported_products}"
        )

    if (
        args.expect_remaining_products
        is not None
        and len(remaining)
        != args.expect_remaining_products
    ):
        raise SystemExit(
            "BLOCK: remaining product count "
            f"is {len(remaining)}, expected "
            f"{args.expect_remaining_products}"
        )

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    candidates = {
        row["stock_clip_id"]: row
        for row in con.execute(
            """
            SELECT
                stock_clip_id,
                project_name,
                product_role,
                source_name,
                source_path,
                active
            FROM reconstructed_candidates
            """
        )
    }

    con.close()

    waves = build_waves(batches)

    wave_for_batch = {}

    for number, wave in enumerate(
        waves,
        start=1,
    ):
        for batch in wave:
            wave_for_batch[
                batch["batch_id"]
            ] = number

    xml_cache = {}
    probe_cache = {}
    rows = []

    remaining = sorted(
        remaining,
        key=lambda item: (
            int(
                batch_by_id[
                    item["batch_id"]
                ]["batch_index"]
            ),
            item["stock_clip_id"],
        ),
    )

    for item in remaining:
        stock_id = item["stock_clip_id"]
        batch = batch_by_id[
            item["batch_id"]
        ]

        batch_id = batch["batch_id"]
        batch_index = int(
            batch["batch_index"]
        )

        if batch_id not in xml_cache:
            xml_cache[batch_id] = (
                xml_sources(
                    Path(batch["xml_path"])
                )
            )

        xml_paths = xml_cache[
            batch_id
        ].get(stock_id, [])

        xml_source = (
            xml_paths[0]
            if len(xml_paths) == 1
            else ""
        )

        candidate = candidates.get(stock_id)

        db_source = (
            file_path(
                candidate["source_path"]
            )
            if candidate
            else ""
        )

        issues = []

        if candidate is None:
            issues.append(
                "missing_db_candidate"
            )
        elif int(candidate["active"]) != 1:
            issues.append(
                "db_candidate_inactive"
            )

        if not db_source:
            issues.append(
                "missing_db_source_path"
            )

        if not xml_paths:
            issues.append(
                "missing_batch_xml_source"
            )
        elif len(xml_paths) > 1:
            issues.append(
                "multiple_batch_xml_sources"
            )

        if (
            db_source
            and xml_source
            and Path(db_source)
            != Path(xml_source)
        ):
            issues.append(
                "db_xml_source_path_mismatch"
            )

        chosen = (
            db_source
            or xml_source
        )

        mounted = volume_mounted(
            chosen
        )

        if chosen not in probe_cache:
            path = (
                Path(chosen)
                if chosen
                else None
            )

            exists = bool(
                path
                and path.is_file()
            )

            media = None
            probe_error = ""

            if exists:
                media, probe_error = probe(
                    path
                )

            probe_cache[chosen] = {
                "mounted": mounted,
                "exists": exists,
                "readable":
                    media is not None,
                "media": media,
                "probe_error":
                    probe_error,
            }

        state = probe_cache[chosen]

        if not chosen:
            availability = (
                "NO_SOURCE_PATH"
            )
        elif not state["mounted"]:
            availability = (
                "VOLUME_NOT_MOUNTED"
            )
        elif not state["exists"]:
            availability = (
                "SOURCE_FILE_MISSING"
            )
        elif not state["readable"]:
            availability = (
                "SOURCE_UNREADABLE"
            )
        else:
            availability = "READY_NOW"

        mapping_problem_names = {
            "missing_db_candidate",
            "db_candidate_inactive",
            "missing_db_source_path",
            "missing_batch_xml_source",
            "multiple_batch_xml_sources",
            "db_xml_source_path_mismatch",
        }

        mapping_ok = not any(
            issue in mapping_problem_names
            for issue in issues
        )

        ready_now = (
            availability == "READY_NOW"
            and mapping_ok
        )

        media = (
            state["media"]
            or {}
        )

        rows.append({
            "wave":
                wave_for_batch[batch_id],
            "batch_index":
                batch_index,
            "batch_id":
                batch_id,
            "stock_clip_id":
                stock_id,
            "project_name":
                candidate["project_name"]
                if candidate else "",
            "product_role":
                candidate["product_role"]
                if candidate else "",
            "source_name":
                candidate["source_name"]
                if candidate else "",
            "db_source_path":
                db_source,
            "batch_xml_source_path":
                xml_source,
            "volume":
                volume_name(chosen),
            "volume_mounted":
                state["mounted"],
            "source_exists":
                state["exists"],
            "source_readable":
                state["readable"],
            "availability":
                availability,
            "mapping_ok":
                mapping_ok,
            "ready_now":
                ready_now,
            "source_width":
                media.get("width", ""),
            "source_height":
                media.get("height", ""),
            "source_frame_rate":
                media.get(
                    "frame_rate", ""
                ),
            "source_codec":
                media.get("codec", ""),
            "source_pix_fmt":
                media.get("pix_fmt", ""),
            "issues":
                " | ".join(issues),
            "probe_error":
                state["probe_error"],
        })

    args.output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    csv_path = (
        args.output_dir
        / "remaining-media-availability.csv"
    )

    json_path = (
        args.output_dir
        / "remaining-media-availability.json"
    )

    with csv_path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=list(
                rows[0].keys()
            ),
        )
        writer.writeheader()
        writer.writerows(rows)

    json_path.write_text(
        json.dumps(
            {
                "plan_id":
                    manifest["plan_id"],
                "exported_products":
                    len(exported_ids),
                "remaining_products":
                    len(rows),
                "rows":
                    rows,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    ready = [
        row
        for row in rows
        if row["ready_now"]
    ]

    deferred = [
        row
        for row in rows
        if not row["ready_now"]
    ]

    all_sources = {
        row["db_source_path"]
        or row[
            "batch_xml_source_path"
        ]
        for row in rows
        if (
            row["db_source_path"]
            or row[
                "batch_xml_source_path"
            ]
        )
    }

    ready_sources = {
        row["db_source_path"]
        or row[
            "batch_xml_source_path"
        ]
        for row in ready
    }

    print(
        "VCLIP REMAINING "
        "ORIGINAL-MEDIA INVENTORY"
    )
    print(
        "========================================"
    )
    print(
        f"Plan:                    "
        f"{manifest['plan_id']}"
    )
    print(
        f"Exported products:       "
        f"{len(exported_ids):,}"
    )
    print(
        f"Remaining products:      "
        f"{len(rows):,}"
    )
    print(
        f"Remaining unique sources:"
        f" {len(all_sources):,}"
    )

    print()
    print("CURRENTLY RUNNABLE")
    print("------------------")
    print(
        f"Products:                "
        f"{len(ready):,}"
    )
    print(
        f"Unique source files:     "
        f"{len(ready_sources):,}"
    )

    print()
    print("DEFERRED / BLOCKED")
    print("------------------")
    print(
        f"Products:                "
        f"{len(deferred):,}"
    )

    print()
    print("AVAILABILITY")
    print("------------")

    for status, n in Counter(
        row["availability"]
        for row in rows
    ).most_common():
        print(
            f"{n:5d}  {status}"
        )

    print()
    print("BY SOURCE VOLUME")
    print("----------------")

    by_volume = defaultdict(list)

    for row in rows:
        by_volume[
            row["volume"]
        ].append(row)

    for volume, group in sorted(
        by_volume.items(),
        key=lambda x: (
            -len(x[1]),
            x[0].casefold(),
        ),
    ):
        group_ready = [
            r for r in group
            if r["ready_now"]
        ]

        sources = {
            r["db_source_path"]
            or r[
                "batch_xml_source_path"
            ]
            for r in group
            if (
                r["db_source_path"]
                or r[
                    "batch_xml_source_path"
                ]
            )
        }

        readable_sources = {
            r["db_source_path"]
            or r[
                "batch_xml_source_path"
            ]
            for r in group_ready
        }

        mounted = any(
            r["volume_mounted"]
            for r in group
        )

        print(
            f"{len(group):5d} products  "
            f"{len(group_ready):5d} ready  "
            f"{len(sources):4d} sources  "
            f"{len(readable_sources):4d} readable  "
            f"mounted="
            f"{'yes' if mounted else 'no '}  "
            f"{volume}"
        )

    print()
    print("BY WAVE")
    print("-------")

    by_wave = defaultdict(list)

    for row in rows:
        by_wave[
            int(row["wave"])
        ].append(row)

    for wave in sorted(by_wave):
        group = by_wave[wave]
        n_ready = sum(
            bool(r["ready_now"])
            for r in group
        )

        if n_ready == len(group):
            status = "FULLY RUNNABLE"
        elif n_ready == 0:
            status = "BLOCKED"
        else:
            status = "PARTIAL"

        batch_indexes = [
            r["batch_index"]
            for r in group
        ]

        print(
            f"Wave {wave:02d}  "
            f"batches "
            f"{min(batch_indexes):03d}-"
            f"{max(batch_indexes):03d}  "
            f"{n_ready:4d}/"
            f"{len(group):4d} ready  "
            f"{status}"
        )

    print()
    print("BY REMAINING BATCH")
    print("------------------")

    by_batch = defaultdict(list)

    for row in rows:
        by_batch[
            row["batch_id"]
        ].append(row)

    for batch_id in sorted(
        by_batch,
        key=lambda x: int(
            batch_by_id[x][
                "batch_index"
            ]
        ),
    ):
        group = by_batch[batch_id]

        n_ready = sum(
            bool(r["ready_now"])
            for r in group
        )

        if n_ready == len(group):
            status = "RUN"
        elif n_ready == 0:
            status = "DEFER"
        else:
            status = "MIXED"

        volumes = ", ".join(
            sorted({
                r["volume"]
                for r in group
            })
        )

        print(
            f"{int(batch_by_id[batch_id]['batch_index']):03d}  "
            f"{n_ready:3d}/{len(group):3d} ready  "
            f"{status:5s}  "
            f"{volumes}"
        )

    if deferred:
        print()
        print(
            "DEFERRED BY VOLUME / CONDITION"
        )
        print(
            "------------------------------"
        )

        deferred_by_volume = (
            defaultdict(list)
        )

        for row in deferred:
            deferred_by_volume[
                row["volume"]
            ].append(row)

        for volume, group in sorted(
            deferred_by_volume.items(),
            key=lambda x: (
                -len(x[1]),
                x[0].casefold(),
            ),
        ):
            sources = {
                r["db_source_path"]
                or r[
                    "batch_xml_source_path"
                ]
                for r in group
                if (
                    r["db_source_path"]
                    or r[
                        "batch_xml_source_path"
                    ]
                )
            }

            reasons = Counter(
                r["availability"]
                for r in group
            )

            reason_text = ", ".join(
                f"{key}={value}"
                for key, value
                in reasons.items()
            )

            print(
                f"{len(group):5d} products  "
                f"{len(sources):4d} sources  "
                f"{volume}  "
                f"[{reason_text}]"
            )

    mapping_problems = [
        row
        for row in rows
        if not row["mapping_ok"]
    ]

    if mapping_problems:
        print()
        print("SOURCE-MAPPING PROBLEMS")
        print("-----------------------")

        for row in mapping_problems[:100]:
            print(
                f"batch="
                f"{row['batch_index']:03d} "
                f"{row['stock_clip_id']} "
                f"{row['issues']}"
            )

    print()
    print("CSV: ", csv_path)
    print("JSON:", json_path)

    if mapping_problems:
        print()
        print(
            "RESULT: BLOCK — source "
            "mapping problems found"
        )
        return 3

    print()
    print("RESULT: INVENTORY COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
