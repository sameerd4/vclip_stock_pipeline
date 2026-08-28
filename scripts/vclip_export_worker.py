#!/usr/bin/env python3
"""Fail-closed Final Cut batch-export orchestrator for a VClip export plan.

The Swift AX helper drives Final Cut's native renderer. This worker verifies the
output directory, waits for every expected file to become stable, ffprobes every
master, and writes a receipt. It does NOT ingest the canonical DB; ingestion is a
separate explicit transaction.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    fmt = data.get("format") or {}
    rate = stream.get("r_frame_rate") or "0/1"
    num, den = rate.split("/", 1)
    fps = float(num) / float(den) if float(den) else 0.0
    return {
        "duration_seconds": float(fmt.get("duration") or 0),
        "width": stream.get("width"),
        "height": stream.get("height"),
        "frame_rate": fps,
        "codec_name": stream.get("codec_name"),
    }


def video_files(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
    )


def receipt_valid(receipt_path: Path) -> bool:
    if not receipt_path.is_file():
        return False
    try:
        data = json.loads(receipt_path.read_text(encoding="utf-8"))
        return data.get("status") == "complete" and all(
            Path(item["path"]).is_file() for item in data.get("files", [])
        )
    except Exception:
        return False


def update_batch_db(db: Path | None, batch_id: str, **fields: Any) -> None:
    if db is None:
        return
    con = sqlite3.connect(db)
    assignments = ", ".join(f"{key}=?" for key in fields)
    con.execute(
        f"UPDATE master_export_batches SET {assignments} WHERE id=?",
        [*fields.values(), batch_id],
    )
    con.commit()
    con.close()


def wait_for_outputs(
    *,
    output_dir: Path,
    expected_items: list[dict[str, Any]],
    ffprobe: str,
    timeout: float,
    stable_seconds: float,
    poll_seconds: float,
    duration_tolerance: float,
) -> list[dict[str, Any]]:
    expected = {item["expected_basename"]: item for item in expected_items}
    first_seen: dict[str, float] = {}
    last_state: dict[str, tuple[int, int]] = {}
    stable_since: dict[str, float] = {}
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        files = video_files(output_dir)
        by_stem: dict[str, list[Path]] = {}
        for path in files:
            by_stem.setdefault(path.stem, []).append(path)
        unexpected = sorted(set(by_stem) - set(expected))
        if unexpected:
            raise RuntimeError(
                f"Unexpected exported video basename(s) in {output_dir}: {unexpected[:20]}"
            )
        ambiguous = {stem: paths for stem, paths in by_stem.items() if len(paths) > 1}
        if ambiguous:
            raise RuntimeError(
                "Multiple files share an expected basename: "
                + ", ".join(f"{stem}={paths}" for stem, paths in ambiguous.items())
            )

        all_present = True
        all_stable = True
        current = time.monotonic()
        for stem in expected:
            paths = by_stem.get(stem)
            if not paths:
                all_present = False
                all_stable = False
                continue
            path = paths[0]
            stat = path.stat()
            state = (stat.st_size, stat.st_mtime_ns)
            first_seen.setdefault(stem, current)
            if last_state.get(stem) != state:
                last_state[stem] = state
                stable_since[stem] = current
                all_stable = False
            elif current - stable_since.get(stem, current) < stable_seconds:
                all_stable = False

        if all_present and all_stable and len(files) == len(expected):
            results: list[dict[str, Any]] = []
            probe_not_ready: list[tuple[str, str]] = []

            for stem, item in expected.items():
                path = by_stem[stem][0]

                try:
                    media = probe(path, ffprobe)
                except Exception as exc:
                    # Final Cut can leave the exported path size/mtime stable
                    # briefly before the MP4/MOV container is fully finalized
                    # (for example before the moov atom is readable).
                    #
                    # Never accept the file without a successful probe, but
                    # also don't classify a transient container-finalization
                    # race as permanent corruption. The overall worker timeout
                    # remains the hard fail-closed boundary.
                    stable_since[stem] = current
                    probe_not_ready.append(
                        (stem, f"{type(exc).__name__}: {exc}")
                    )
                    continue

                fps = float(item.get("frame_rate") or media["frame_rate"] or 30.0)
                tolerance = max(duration_tolerance, 2.0 / max(1.0, fps))
                delta = abs(media["duration_seconds"] - float(item["duration_seconds"]))
                if delta > tolerance:
                    raise RuntimeError(
                        f"Duration mismatch for {stem}: expected {item['duration_seconds']:.3f}s, "
                        f"got {media['duration_seconds']:.3f}s, tolerance {tolerance:.3f}s"
                    )
                if item.get("width") and int(media["width"] or 0) != int(item["width"]):
                    raise RuntimeError(
                        f"Width mismatch for {stem}: expected {item['width']}, got {media['width']}"
                    )
                if item.get("height") and int(media["height"] or 0) != int(item["height"]):
                    raise RuntimeError(
                        f"Height mismatch for {stem}: expected {item['height']}, got {media['height']}"
                    )
                results.append(
                    {
                        "stock_clip_id": item["stock_clip_id"],
                        "expected_basename": stem,
                        "path": str(path.resolve()),
                        "filename": path.name,
                        "file_size_bytes": path.stat().st_size,
                        **media,
                    }
                )

            if probe_not_ready:
                for stem, error in probe_not_ready[:5]:
                    print(
                        f"      waiting for container finalization: "
                        f"{stem}: {error}",
                        flush=True,
                    )
                time.sleep(poll_seconds)
                continue

            return results

        done = sum(1 for stem in expected if stem in by_stem)
        print(
            f"      exports present {done}/{len(expected)}; "
            f"stable {sum(1 for stem in expected if stem in stable_since and current - stable_since[stem] >= stable_seconds)}/{len(expected)}",
            flush=True,
        )
        time.sleep(poll_seconds)

    raise TimeoutError(f"Timed out after {timeout:.0f}s waiting for exports in {output_dir}")


def run(args: argparse.Namespace) -> int:
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    library_root = manifest.get("library_root")
    library_name = manifest.get("library_name")
    if bool(library_root) != bool(library_name):
        raise RuntimeError(
            "Export manifest must provide library_root and library_name together"
        )

    items_by_batch: dict[str, list[dict[str, Any]]] = {}
    for item in manifest["items"]:
        items_by_batch.setdefault(item["batch_id"], []).append(item)

    batches = list(manifest["batches"])
    if args.start_batch:
        batches = [batch for batch in batches if int(batch["batch_index"]) >= args.start_batch]
    if args.limit_batches:
        batches = batches[: args.limit_batches]

    print("VClip Final Cut export worker")
    print("=============================")
    print(f"Plan:    {manifest['plan_id']}")
    print(f"Batches: {len(batches)}")
    print(f"Items:   {sum(len(items_by_batch[b['batch_id']]) for b in batches)}")
    if library_root and library_name:
        print(f"Library: {library_root}/{library_name}.fcpbundle")
    print()

    completed = 0
    for index, batch in enumerate(batches, 1):
        batch_id = batch["batch_id"]
        receipt_path = Path(batch["receipt_path"])
        output_dir = Path(batch["output_directory"])
        expected_items = items_by_batch[batch_id]
        print(f"[{index:03d}/{len(batches):03d}] {batch_id}  projects={len(expected_items)}")

        if args.resume and receipt_valid(receipt_path):
            completed += 1
            print("    receipt already complete")
            continue

        output_dir.mkdir(parents=True, exist_ok=True)
        existing = video_files(output_dir)

        if existing:
            print(
                f"    output directory contains {len(existing)} video file(s) "
                "without a complete receipt; attempting recovery",
                flush=True,
            )

            try:
                recovered_files = wait_for_outputs(
                    output_dir=output_dir,
                    expected_items=expected_items,
                    ffprobe=args.ffprobe,
                    timeout=args.recovery_timeout,
                    stable_seconds=args.stable_seconds,
                    poll_seconds=args.poll_seconds,
                    duration_tolerance=args.duration_tolerance,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Output directory is non-empty but could not be safely "
                    f"recovered: {output_dir}. "
                    f"Existing files were preserved. Recovery error: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

            recovered_at = now()
            receipt = {
                "schema_version": 1,
                "status": "complete",
                "plan_id": manifest["plan_id"],
                "batch_id": batch_id,
                "started_at": recovered_at,
                "completed_at": recovered_at,
                "recovered_existing_outputs": True,
                "files": recovered_files,
            }

            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text(
                json.dumps(receipt, indent=2),
                encoding="utf-8",
            )

            update_batch_db(
                args.db,
                batch_id,
                status="rendered",
                receipt_path=str(receipt_path),
                completed_at=recovered_at,
                error_text=None,
            )

            completed += 1
            print(
                f"    recovered complete batch: "
                f"{len(recovered_files)} master(s)",
                flush=True,
            )
            continue

        started_at = now()
        update_batch_db(
            args.db,
            batch_id,
            status="running",
            started_at=started_at,
            error_text=None,
        )
        resolutions = {
            (int(item["width"]), int(item["height"]))
            for item in expected_items
            if item.get("width") and item.get("height")
        }
        if len(resolutions) != 1:
            raise RuntimeError(
                f"Batch {batch_id} must have exactly one expected resolution; "
                f"found {sorted(resolutions)}"
            )
        expected_width, expected_height = next(iter(resolutions))

        command = [
            str(args.ax_binary),
            "export-batch",
            "--xml",
            batch["xml_path"],
            "--event",
            batch["event_name"],
            "--expected",
            str(batch["expected_count"]),
            "--output",
            batch["output_directory"],
            "--destination",
            manifest["share_destination"],
            "--width",
            str(expected_width),
            "--height",
            str(expected_height),
        ]
        if library_root and library_name:
            command.extend(
                [
                    "--library-root",
                    str(library_root),
                    "--library-name",
                    str(library_name),
                ]
            )

        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            print("    driving Final Cut...", flush=True)
            result = subprocess.run(command, capture_output=True, text=True)
            if result.stderr:
                print(result.stderr.rstrip())
            if result.returncode != 0:
                raise RuntimeError(
                    f"AX helper failed with code {result.returncode}: {result.stdout.strip()}"
                )
            ax_receipt = json.loads(result.stdout)
            if ax_receipt.get("status") != "share_started":
                raise RuntimeError(f"AX helper did not start share: {ax_receipt}")

            print("    waiting for Final Cut renders...", flush=True)
            files = wait_for_outputs(
                output_dir=output_dir,
                expected_items=expected_items,
                ffprobe=args.ffprobe,
                timeout=args.timeout,
                stable_seconds=args.stable_seconds,
                poll_seconds=args.poll_seconds,
                duration_tolerance=args.duration_tolerance,
            )
            receipt = {
                "schema_version": 1,
                "status": "complete",
                "plan_id": manifest["plan_id"],
                "batch_id": batch_id,
                "started_at": started_at,
                "completed_at": now(),
                "ax_receipt": ax_receipt,
                "files": files,
            }
            receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
            update_batch_db(
                args.db,
                batch_id,
                status="rendered",
                receipt_path=str(receipt_path),
                completed_at=receipt["completed_at"],
                error_text=None,
            )
            completed += 1
            print(f"    complete: {len(files)} master(s)")
        except Exception as exc:
            failed_receipt = {
                "schema_version": 1,
                "status": "failed",
                "plan_id": manifest["plan_id"],
                "batch_id": batch_id,
                "started_at": started_at,
                "failed_at": now(),
                "error": f"{type(exc).__name__}: {exc}",
            }
            receipt_path.write_text(json.dumps(failed_receipt, indent=2), encoding="utf-8")
            update_batch_db(
                args.db,
                batch_id,
                status="failed",
                receipt_path=str(receipt_path),
                completed_at=failed_receipt["failed_at"],
                error_text=failed_receipt["error"],
            )
            print(f"    FAILED: {failed_receipt['error']}")
            return 2

    print()
    print(f"Completed batches: {completed}/{len(batches)}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--ax-binary", type=Path, required=True)
    p.add_argument("--db", type=Path)
    p.add_argument("--ffprobe", default="ffprobe")
    p.add_argument("--timeout", type=float, default=7200)
    p.add_argument("--stable-seconds", type=float, default=15)
    p.add_argument("--poll-seconds", type=float, default=5)
    p.add_argument("--duration-tolerance", type=float, default=0.25)
    p.add_argument(
        "--recovery-timeout",
        type=float,
        default=120,
        help="Seconds to validate/finalize pre-existing batch outputs before failing closed.",
    )
    p.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--start-batch", type=int)
    p.add_argument("--limit-batches", type=int)
    return p


if __name__ == "__main__":
    args = parser().parse_args()
    args.manifest = args.manifest.expanduser().resolve()
    args.ax_binary = args.ax_binary.expanduser().resolve()
    if args.db:
        args.db = args.db.expanduser().resolve()
    raise SystemExit(run(args))
