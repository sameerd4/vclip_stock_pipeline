#!/usr/bin/env python3
"""Run VClip Final Cut exports in disposable, audited waves.

A wave succeeds only when:
- the worker completes every batch,
- every expected receipt is complete,
- rendered file metadata agrees with the export manifest,
- every product still maps to an active canonical DB candidate,
- known candidate location metadata does not disagree with parent provenance.

Only after those checks pass does the supervisor quit Final Cut and delete the
disposable staging library. Any failure preserves the library for debugging.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vclip_ready_location_inventory import normalize, parse_location


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def build_waves(
    batches: list[dict[str, Any]],
    max_batches: int,
    max_items: int,
) -> list[list[dict[str, Any]]]:
    ordered = sorted(batches, key=lambda b: int(b["batch_index"]))
    waves: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    items = 0

    for batch in ordered:
        count = int(batch["expected_count"])

        if current and (
            len(current) >= max_batches
            or items + count > max_items
        ):
            waves.append(current)
            current = []
            items = 0

        current.append(batch)
        items += count

    if current:
        waves.append(current)

    return waves


def batch_receipt_complete(batch: dict[str, Any]) -> bool:
    receipt = read_json(Path(batch["receipt_path"]))
    if not receipt or receipt.get("status") != "complete":
        return False

    files = receipt.get("files") or []
    if len(files) != int(batch["expected_count"]):
        return False

    return all(
        item.get("path") and Path(item["path"]).is_file()
        for item in files
    )


def wave_complete_count(wave: list[dict[str, Any]]) -> int:
    return sum(batch_receipt_complete(batch) for batch in wave)


def all_prior_batches_complete(
    batches: list[dict[str, Any]],
    first_batch_index: int,
) -> bool:
    return all(
        batch_receipt_complete(batch)
        for batch in batches
        if int(batch["batch_index"]) < first_batch_index
    )


def directory_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0

    result = subprocess.run(
        ["du", "-sk", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return 0

    try:
        kib = int(result.stdout.split()[0])
    except Exception:
        return 0

    return kib * 1024


def gib(value: int) -> float:
    return value / 1024**3


def free_gib(path: Path) -> float:
    return shutil.disk_usage(path).free / 1024**3


def quit_final_cut(timeout: float = 120.0) -> None:
    subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "Final Cut Pro" to quit',
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        result = subprocess.run(
            ["pgrep", "-x", "Final Cut Pro"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return

        print("    waiting for Final Cut to quit...", flush=True)
        time.sleep(2)

    raise RuntimeError(
        f"Final Cut did not quit within {timeout:.0f}s"
    )


def cleanup_staging_library(
    staging: Path,
    library_root: Path,
    library_name: str,
) -> int:
    expected = library_root / f"{library_name}.fcpbundle"

    if staging.resolve() != expected.resolve():
        raise RuntimeError(
            f"Refusing staging cleanup: unexpected path {staging}"
        )

    if staging.name != f"{library_name}.fcpbundle":
        raise RuntimeError(
            f"Refusing staging cleanup: unexpected bundle name {staging.name}"
        )

    if staging.suffix != ".fcpbundle":
        raise RuntimeError(
            "Refusing staging cleanup: target is not an .fcpbundle"
        )

    quit_final_cut()

    size = directory_size_bytes(staging)

    if staging.exists():
        print(
            f"    deleting validated staging library "
            f"({gib(size):.1f} GiB): {staging}",
            flush=True,
        )
        shutil.rmtree(staging)

    if staging.exists():
        raise RuntimeError(
            f"Staging library still exists after deletion: {staging}"
        )

    return size


def normalized_label(raw: str | None) -> str:
    loc = parse_location(raw)
    return normalize(loc.get("label", ""))


def audit_wave(
    *,
    manifest: dict[str, Any],
    wave: list[dict[str, Any]],
    db_path: Path,
) -> dict[str, Any]:
    items_by_batch: dict[str, list[dict[str, Any]]] = {}

    for item in manifest["items"]:
        items_by_batch.setdefault(
            item["batch_id"], []
        ).append(item)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    problems: list[dict[str, Any]] = []
    provenance = Counter()
    known_locations = 0
    unknown_locations = 0
    rendered_bytes = 0
    product_count = 0

    parent_location_sql = """
    WITH ranked AS (
        SELECT
            sc.location_json,
            ROW_NUMBER() OVER (
                PARTITION BY sc.stock_clip_id
                ORDER BY
                    COALESCE(sc.updated_at, sc.created_at, '') DESC,
                    sc.rowid DESC
            ) AS rn
        FROM stock_candidates sc
        WHERE sc.stock_clip_id=?
          AND sc.eligibility_status='accepted'
    )
    SELECT location_json
    FROM ranked
    WHERE rn=1
    """

    for batch in wave:
        receipt_path = Path(batch["receipt_path"])
        receipt = read_json(receipt_path)

        if not receipt or receipt.get("status") != "complete":
            problems.append(
                {
                    "batch_index": int(batch["batch_index"]),
                    "batch_id": batch["batch_id"],
                    "issue": "receipt_not_complete",
                }
            )
            continue

        expected_items = {
            item["stock_clip_id"]: item
            for item in items_by_batch.get(batch["batch_id"], [])
        }
        rendered = {
            row["stock_clip_id"]: row
            for row in receipt.get("files", [])
        }

        if set(rendered) != set(expected_items):
            problems.append(
                {
                    "batch_index": int(batch["batch_index"]),
                    "batch_id": batch["batch_id"],
                    "issue": "receipt_identity_mismatch",
                    "expected": sorted(expected_items),
                    "actual": sorted(rendered),
                }
            )

        for stock_id, item in expected_items.items():
            product_count += 1
            issues: list[str] = []

            actual = rendered.get(stock_id)
            if actual is None:
                issues.append("missing_rendered_receipt_item")
                continue

            path = Path(actual["path"])
            if not path.is_file():
                issues.append("rendered_file_missing")

            rendered_bytes += int(
                actual.get("file_size_bytes") or 0
            )

            if path.stem != item["expected_basename"]:
                issues.append("filename_mismatch")

            if (
                item.get("width")
                and int(actual.get("width") or 0)
                != int(item["width"])
            ):
                issues.append("width_mismatch")

            if (
                item.get("height")
                and int(actual.get("height") or 0)
                != int(item["height"])
            ):
                issues.append("height_mismatch")

            expected_fps = float(
                item.get("frame_rate") or 0
            )
            actual_fps = float(
                actual.get("frame_rate") or 0
            )

            if (
                expected_fps
                and abs(actual_fps - expected_fps) > 0.10
            ):
                issues.append("fps_mismatch")

            expected_duration = float(
                item["duration_seconds"]
            )
            actual_duration = float(
                actual.get("duration_seconds") or 0
            )
            fps = expected_fps or actual_fps or 30.0
            tolerance = max(0.25, 2.0 / fps)

            if (
                abs(actual_duration - expected_duration)
                > tolerance
            ):
                issues.append("duration_mismatch")

            candidate = con.execute(
                """
                SELECT
                    stock_clip_id,
                    project_name,
                    product_role,
                    expected_export_basename,
                    location_json,
                    active
                FROM reconstructed_candidates
                WHERE stock_clip_id=?
                """,
                (stock_id,),
            ).fetchone()

            if candidate is None:
                issues.append("missing_db_candidate")
                candidate_label = ""
            else:
                if int(candidate["active"]) != 1:
                    issues.append("db_candidate_inactive")

                if (
                    candidate["product_role"]
                    != item["product_role"]
                ):
                    issues.append("product_role_mismatch")

                if (
                    candidate["expected_export_basename"]
                    != item["expected_basename"]
                ):
                    issues.append("db_basename_mismatch")

                candidate_label = normalized_label(
                    candidate["location_json"]
                )

            parent_ids = [
                row["parent_stock_clip_id"]
                for row in con.execute(
                    """
                    SELECT parent_stock_clip_id
                    FROM reconstructed_candidate_parents
                    WHERE stock_clip_id=?
                    ORDER BY parent_stock_clip_id
                    """,
                    (stock_id,),
                ).fetchall()
            ]

            parent_labels: set[str] = set()

            for parent_id in parent_ids:
                row = con.execute(
                    parent_location_sql,
                    (parent_id,),
                ).fetchone()

                if row is None:
                    continue

                label = normalized_label(
                    row["location_json"]
                )
                if label:
                    parent_labels.add(label)

            candidate_norm = candidate_label.casefold()
            parent_norm = {
                value.casefold()
                for value in parent_labels
            }

            if candidate_label:
                known_locations += 1

                if parent_norm:
                    if candidate_norm in parent_norm:
                        provenance["match"] += 1
                    else:
                        provenance["disagree"] += 1
                        issues.append(
                            "location_parent_disagreement"
                        )
                else:
                    provenance["candidate_only"] += 1
            else:
                unknown_locations += 1

                if parent_norm:
                    provenance["missing_on_candidate"] += 1
                    issues.append(
                        "location_missing_on_candidate"
                    )
                else:
                    provenance["unknown"] += 1

            if issues:
                problems.append(
                    {
                        "batch_index": int(
                            batch["batch_index"]
                        ),
                        "batch_id": batch["batch_id"],
                        "stock_clip_id": stock_id,
                        "project_name": item[
                            "original_project_name"
                        ],
                        "issues": issues,
                        "candidate_location": candidate_label,
                        "parent_locations": sorted(
                            parent_labels
                        ),
                    }
                )

    con.close()

    return {
        "passed": not problems,
        "products": product_count,
        "rendered_bytes": rendered_bytes,
        "known_locations": known_locations,
        "unknown_locations": unknown_locations,
        "location_provenance": dict(provenance),
        "problems": problems,
    }


def write_wave_record(
    *,
    path: Path,
    wave_number: int,
    wave: list[dict[str, Any]],
    audit: dict[str, Any],
    staging_size_bytes: int,
    free_before_cleanup_gib: float,
    free_after_cleanup_gib: float | None,
    status: str,
) -> None:
    record = {
        "schema_version": 1,
        "recorded_at": now(),
        "status": status,
        "wave_number": wave_number,
        "first_batch": int(
            wave[0]["batch_index"]
        ),
        "last_batch": int(
            wave[-1]["batch_index"]
        ),
        "batch_count": len(wave),
        "product_count": sum(
            int(batch["expected_count"])
            for batch in wave
        ),
        "audit": audit,
        "staging_size_bytes": staging_size_bytes,
        "staging_size_gib": gib(
            staging_size_bytes
        ),
        "free_before_cleanup_gib":
            free_before_cleanup_gib,
        "free_after_cleanup_gib":
            free_after_cleanup_gib,
    }

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    path.write_text(
        json.dumps(record, indent=2),
        encoding="utf-8",
    )


def run(args: argparse.Namespace) -> int:
    manifest_path = (
        args.manifest.expanduser().resolve()
    )
    manifest = json.loads(
        manifest_path.read_text(
            encoding="utf-8"
        )
    )

    batches = sorted(
        manifest["batches"],
        key=lambda b: int(
            b["batch_index"]
        ),
    )

    waves = build_waves(
        batches,
        args.max_batches,
        args.max_items,
    )

    library_root_raw = manifest.get(
        "library_root"
    )
    library_name = manifest.get(
        "library_name"
    )

    if not library_root_raw or not library_name:
        raise RuntimeError(
            "Manifest must contain library_root "
            "and library_name"
        )

    library_root = Path(
        library_root_raw
    ).expanduser().resolve()

    staging = (
        library_root
        / f"{library_name}.fcpbundle"
    )

    render_root = Path(
        manifest["render_root"]
    ).expanduser().resolve()

    supervisor_root = (
        Path(manifest["plan_root"])
        / "supervisor"
    )

    start_wave = args.start_wave or 1
    end_wave = (
        args.end_wave
        if args.end_wave is not None
        else len(waves)
    )

    if not 1 <= start_wave <= len(waves):
        raise RuntimeError(
            f"start-wave must be 1..{len(waves)}"
        )

    if not start_wave <= end_wave <= len(waves):
        raise RuntimeError(
            f"end-wave must be "
            f"{start_wave}..{len(waves)}"
        )

    print("VCLIP FINAL CUT WAVE SUPERVISOR")
    print("===============================")
    print(
        f"Plan:          {manifest['plan_id']}"
    )
    print(
        f"Total batches: {len(batches)}"
    )
    print(
        f"Total waves:   {len(waves)}"
    )
    print(
        f"Target waves:  "
        f"{start_wave}-{end_wave}"
    )
    print(
        f"Wave cap:      "
        f"{args.max_batches} batches / "
        f"{args.max_items} products"
    )
    print(
        f"Staging:       {staging}"
    )
    print()

    for number, wave in enumerate(
        waves,
        start=1,
    ):
        first = int(
            wave[0]["batch_index"]
        )
        last = int(
            wave[-1]["batch_index"]
        )
        products = sum(
            int(batch["expected_count"])
            for batch in wave
        )
        complete = wave_complete_count(
            wave
        )

        marker = ""
        if number < start_wave or number > end_wave:
            marker = " [outside target]"
        elif complete == len(wave):
            marker = " [complete]"
        elif complete:
            marker = (
                f" [partial {complete}/"
                f"{len(wave)}]"
            )

        print(
            f"Wave {number:02d}: "
            f"batches {first:03d}-{last:03d} "
            f"batches={len(wave):2d} "
            f"products={products:3d}"
            f"{marker}"
        )

    if args.dry_run:
        print()
        print(
            f"Staging exists: {staging.exists()}"
        )
        print(
            f"Free space:     "
            f"{free_gib(render_root):.1f} GiB"
        )
        print()
        print("DRY RUN COMPLETE")
        return 0

    print()

    for wave_number in range(
        start_wave,
        end_wave + 1,
    ):
        wave = waves[wave_number - 1]
        first = int(
            wave[0]["batch_index"]
        )
        last = int(
            wave[-1]["batch_index"]
        )
        products = sum(
            int(batch["expected_count"])
            for batch in wave
        )
        complete = wave_complete_count(
            wave
        )

        print()
        print(
            f"=== WAVE {wave_number:02d} "
            f"— batches {first:03d}-{last:03d} "
            f"— {products} products ==="
        )

        if complete == len(wave):
            print(
                "    receipts already complete; "
                "auditing wave"
            )

            audit = audit_wave(
                manifest=manifest,
                wave=wave,
                db_path=args.db,
            )

            if not audit["passed"]:
                print("    AUDIT FAILED")
                for problem in audit[
                    "problems"
                ][:20]:
                    print(
                        "      ",
                        json.dumps(
                            problem,
                            sort_keys=True,
                        ),
                    )
                return 3

            print(
                "    audit PASS "
                f"({audit['products']} products)"
            )
            continue

        if staging.exists():
            if complete == 0:
                if all_prior_batches_complete(
                    batches,
                    first,
                ):
                    print(
                        "    staging library appears "
                        "to belong to a completed "
                        "prior wave"
                    )
                    cleanup_staging_library(
                        staging,
                        library_root,
                        library_name,
                    )
                else:
                    print(
                        "    BLOCK: staging library "
                        "exists but prior batches are "
                        "not all complete"
                    )
                    print(
                        "    preserving staging library"
                    )
                    return 4
            else:
                print(
                    f"    resuming partial wave "
                    f"with {complete}/{len(wave)} "
                    f"complete receipts"
                )

        free_before = free_gib(
            render_root
        )

        if free_before < args.min_free_gib:
            print(
                f"    BLOCK: only "
                f"{free_before:.1f} GiB free; "
                f"minimum is "
                f"{args.min_free_gib:.1f} GiB"
            )
            return 5

        worker_cmd = [
            sys.executable,
            str(args.worker),
            "--manifest",
            str(manifest_path),
            "--ax-binary",
            str(args.ax_binary),
            "--db",
            str(args.db),
            "--start-batch",
            str(first),
            "--limit-batches",
            str(len(wave)),
        ]

        print(
            "    launching worker...",
            flush=True,
        )

        result = subprocess.run(
            worker_cmd,
            env=os.environ.copy(),
        )

        if result.returncode != 0:
            print()
            print(
                f"    WAVE FAILED: worker exited "
                f"{result.returncode}"
            )
            print(
                "    staging library PRESERVED:"
            )
            print(
                f"    {staging}"
            )
            print(
                "    Fix the failure and rerun "
                "the same supervisor command."
            )
            return result.returncode or 2

        audit = audit_wave(
            manifest=manifest,
            wave=wave,
            db_path=args.db,
        )

        staging_size = (
            directory_size_bytes(staging)
        )
        free_before_cleanup = free_gib(
            render_root
        )

        if not audit["passed"]:
            record_path = (
                supervisor_root
                / f"wave-{wave_number:02d}.json"
            )

            write_wave_record(
                path=record_path,
                wave_number=wave_number,
                wave=wave,
                audit=audit,
                staging_size_bytes=staging_size,
                free_before_cleanup_gib=
                    free_before_cleanup,
                free_after_cleanup_gib=None,
                status="audit_failed",
            )

            print()
            print(
                "    WAVE AUDIT FAILED"
            )
            for problem in audit[
                "problems"
            ][:20]:
                print(
                    "      ",
                    json.dumps(
                        problem,
                        sort_keys=True,
                    ),
                )

            print(
                "    staging library PRESERVED"
            )
            return 3

        print(
            f"    audit PASS: "
            f"{audit['products']} products, "
            f"known location="
            f"{audit['known_locations']}, "
            f"unknown="
            f"{audit['unknown_locations']}"
        )
        print(
            f"    location provenance: "
            f"{audit['location_provenance']}"
        )
        print(
            f"    rendered masters: "
            f"{gib(audit['rendered_bytes']):.2f} GiB"
        )
        print(
            f"    staging library: "
            f"{gib(staging_size):.2f} GiB"
        )

        cleanup_staging_library(
            staging,
            library_root,
            library_name,
        )

        free_after_cleanup = free_gib(
            render_root
        )

        record_path = (
            supervisor_root
            / f"wave-{wave_number:02d}.json"
        )

        write_wave_record(
            path=record_path,
            wave_number=wave_number,
            wave=wave,
            audit=audit,
            staging_size_bytes=staging_size,
            free_before_cleanup_gib=
                free_before_cleanup,
            free_after_cleanup_gib=
                free_after_cleanup,
            status="complete",
        )

        print(
            f"    free after cleanup: "
            f"{free_after_cleanup:.1f} GiB"
        )
        print(
            f"    wave record: {record_path}"
        )
        print(
            f"=== WAVE {wave_number:02d} PASS ==="
        )

    print()
    print(
        f"SUPERVISOR COMPLETE — "
        f"waves {start_wave}-{end_wave}"
    )
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__
    )
    p.add_argument(
        "--manifest",
        type=Path,
        required=True,
    )
    p.add_argument(
        "--worker",
        type=Path,
        required=True,
    )
    p.add_argument(
        "--ax-binary",
        type=Path,
        required=True,
    )
    p.add_argument(
        "--db",
        type=Path,
        required=True,
    )
    p.add_argument(
        "--max-batches",
        type=int,
        default=12,
    )
    p.add_argument(
        "--max-items",
        type=int,
        default=100,
    )
    p.add_argument(
        "--start-wave",
        type=int,
    )
    p.add_argument(
        "--end-wave",
        type=int,
    )
    p.add_argument(
        "--min-free-gib",
        type=float,
        default=80.0,
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
    )
    return p


if __name__ == "__main__":
    args = parser().parse_args()

    args.worker = (
        args.worker.expanduser().resolve()
    )
    args.ax_binary = (
        args.ax_binary.expanduser().resolve()
    )
    args.db = (
        args.db.expanduser().resolve()
    )

    raise SystemExit(run(args))
