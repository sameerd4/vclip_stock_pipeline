#!/usr/bin/env python3
"""Mass-produce reconstructed VClip shards with shared telemetry/visual caches.

Runs the proven single-shard reconstruction compiler over one or more physical
review roots. Each shard is an independent, resumable transaction. The canonical
DB and input shards remain read-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class ShardState:
    key: str
    input_path: str
    input_sha256: str
    output_path: str
    report_path: str
    log_path: str
    settings_sha256: str
    status: str
    started_at: str | None = None
    completed_at: str | None = None
    runtime_seconds: float | None = None
    counts: dict[str, int] | None = None
    error: str | None = None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def discover(roots: list[Path], output_root: Path) -> list[tuple[str, Path, Path]]:
    out: list[tuple[str, Path, Path]] = []
    seen: set[Path] = set()
    output_resolved = output_root.resolve()
    for root in roots:
        root = root.expanduser().resolve()
        if root.is_file():
            candidates = [root] if root.suffix.lower() == ".fcpxml" else []
            base = root.parent
            label = root.parent.name or "files"
        elif root.is_dir():
            candidates = sorted(root.rglob("*.fcpxml"))
            base = root
            label = root.name
        else:
            candidates = []
            base = root
            label = root.name or "missing"
        for path in candidates:
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in seen:
                continue
            try:
                resolved.relative_to(output_resolved)
                continue
            except ValueError:
                pass
            seen.add(resolved)
            relative = path.relative_to(base) if path != root else Path(path.name)
            out.append((label, path, relative))
    return sorted(out, key=lambda row: (row[0].casefold(), str(row[2]).casefold()))


def load_state(path: Path) -> dict[str, ShardState]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {
            item["key"]: ShardState(**item)
            for item in payload.get("shards", [])
        }
    except Exception:
        return {}


def write_state(path: Path, states: dict[str, ShardState], settings: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "settings": settings,
        "shards": [asdict(states[key]) for key in sorted(states)],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def report_counts(path: Path) -> dict[str, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    keys = (
        "anchors_total",
        "source_video_count",
        "source_with_flight_telemetry",
        "historical_original_count",
        "ready_project_count",
        "extended_master_count",
        "repair_candidate_count",
        "qc_review_project_count",
    )
    return {key: int(data.get(key) or 0) for key in keys}


def run_one(
    *,
    key: str,
    input_path: Path,
    output_path: Path,
    report_path: Path,
    log_path: Path,
    args: argparse.Namespace,
    settings_sha: str,
    input_sha: str,
) -> ShardState:
    started = time.monotonic()
    state = ShardState(
        key=key,
        input_path=str(input_path),
        input_sha256=input_sha,
        output_path=str(output_path),
        report_path=str(report_path),
        log_path=str(log_path),
        settings_sha256=settings_sha,
        status="running",
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(args.python),
        str(args.compiler),
        "--input",
        str(input_path),
        "--output",
        str(output_path),
        "--report",
        str(report_path),
        "--flight-record-root",
        str(args.flight_record_root),
        "--cache-dir",
        str(args.telemetry_cache_dir),
        "--visual-helper",
        str(args.visual_helper),
        "--visual-cache-dir",
        str(args.visual_cache_dir),
        "--visual-fps",
        str(args.visual_fps),
        "--visual-width",
        str(args.visual_width),
        "--min-duration",
        str(args.min_duration),
        "--target-duration",
        str(args.target_duration),
        "--max-duration",
        str(args.max_duration),
        "--max-extension-each-side",
        str(args.max_extension_each_side),
        "--transition-pad",
        str(args.transition_pad),
    ]
    for root in args.media_root:
        cmd.extend(["--media-root", str(root)])

    env = os.environ.copy()
    with log_path.open("w", encoding="utf-8") as log:
        log.write("COMMAND\n=======\n" + " ".join(cmd) + "\n\n")
        log.flush()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(line)
            log.flush()
            if (
                "RECONSTRUCTION COMPLETE" in line
                or "edit-frame boundary audit" in line
                or "Traceback" in line
            ):
                print(f"    {line.rstrip()}", flush=True)
        code = proc.wait()

    state.runtime_seconds = round(time.monotonic() - started, 3)
    state.completed_at = datetime.now(timezone.utc).isoformat()
    if code == 0 and output_path.is_file() and report_path.is_file():
        state.status = "complete"
        try:
            state.counts = report_counts(report_path)
        except Exception as exc:
            state.status = "failed"
            state.error = f"Invalid report: {type(exc).__name__}: {exc}"
    else:
        state.status = "failed"
        state.error = f"Compiler exit code {code}; inspect {log_path}"
    return state


def run(args: argparse.Namespace) -> int:
    args.input_root = [path.expanduser().resolve() for path in args.input_root]
    args.output_root = args.output_root.expanduser().resolve()
    args.compiler = args.compiler.expanduser().resolve()
    args.python = args.python.expanduser().resolve()
    args.flight_record_root = args.flight_record_root.expanduser().resolve()
    args.media_root = [path.expanduser().resolve() for path in args.media_root]
    args.telemetry_cache_dir = args.telemetry_cache_dir.expanduser().resolve()
    args.visual_cache_dir = args.visual_cache_dir.expanduser().resolve()
    args.visual_helper = args.visual_helper.expanduser().resolve()

    if not args.compiler.is_file():
        raise SystemExit(f"Compiler does not exist: {args.compiler}")
    if not args.visual_helper.is_file():
        raise SystemExit(f"Vision helper does not exist: {args.visual_helper}")
    if not args.flight_record_root.is_dir():
        raise SystemExit(f"Flight-record root does not exist: {args.flight_record_root}")

    shards = discover(args.input_root, args.output_root)
    if args.limit is not None:
        shards = shards[: args.limit]
    if not shards:
        raise SystemExit("No shard FCPXML files found.")

    settings = {
        "compiler": str(args.compiler),
        "flight_record_root": str(args.flight_record_root),
        "media_roots": [str(path) for path in args.media_root],
        "telemetry_cache_dir": str(args.telemetry_cache_dir),
        "visual_cache_dir": str(args.visual_cache_dir),
        "visual_helper": str(args.visual_helper),
        "visual_fps": args.visual_fps,
        "visual_width": args.visual_width,
        "min_duration": args.min_duration,
        "target_duration": args.target_duration,
        "max_duration": args.max_duration,
        "max_extension_each_side": args.max_extension_each_side,
        "transition_pad": args.transition_pad,
    }
    settings_sha = stable_hash(settings)
    state_path = args.output_root / "corpus-reconstruction-state.json"
    states = load_state(state_path)

    print("VClip corpus reconstruction")
    print("===========================")
    print(f"Shards discovered: {len(shards):,}")
    print(f"Output root:       {args.output_root}")
    print(f"Visual sampling:   {args.visual_fps:g} fps @ {args.visual_width}px")
    print(f"Resume:            {args.resume}")
    print()

    complete = 0
    skipped = 0
    failed = 0
    total_started = time.monotonic()

    for index, (label, input_path, relative) in enumerate(shards, 1):
        key = f"{label}/{relative.as_posix()}"
        stem_relative = relative.with_suffix("")
        output_path = (
            args.output_root
            / "raw"
            / label
            / stem_relative.parent
            / f"{stem_relative.name}--reconstructed.fcpxml"
        )
        report_path = (
            args.output_root
            / "reports"
            / label
            / stem_relative.parent
            / f"{stem_relative.name}--reconstruction.json"
        )
        log_path = (
            args.output_root
            / "logs"
            / label
            / stem_relative.parent
            / f"{stem_relative.name}.log"
        )
        input_sha = sha256_file(input_path)
        prior = states.get(key)
        reusable = bool(
            args.resume
            and prior
            and prior.status == "complete"
            and prior.input_sha256 == input_sha
            and prior.settings_sha256 == settings_sha
            and Path(prior.output_path).is_file()
            and Path(prior.report_path).is_file()
        )
        print(f"[{index:03d}/{len(shards):03d}] {key}")
        if reusable:
            skipped += 1
            print("    cached/resumed")
            continue

        state = run_one(
            key=key,
            input_path=input_path,
            output_path=output_path,
            report_path=report_path,
            log_path=log_path,
            args=args,
            settings_sha=settings_sha,
            input_sha=input_sha,
        )
        states[key] = state
        write_state(state_path, states, settings)
        if state.status == "complete":
            complete += 1
            counts = state.counts or {}
            print(
                "    complete "
                f"ready={counts.get('ready_project_count', 0)} "
                f"masters={counts.get('extended_master_count', 0)} "
                f"qc={counts.get('qc_review_project_count', 0)} "
                f"runtime={state.runtime_seconds:.1f}s"
            )
        else:
            failed += 1
            print(f"    FAILED: {state.error}")
            if args.fail_fast:
                break

    runtime = time.monotonic() - total_started
    write_state(state_path, states, settings)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "settings": settings,
        "shards_discovered": len(shards),
        "completed_this_run": complete,
        "resumed": skipped,
        "failed": failed,
        "runtime_seconds": round(runtime, 3),
        "state_path": str(state_path),
    }
    summary_path = args.output_root / "corpus-reconstruction-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print()
    print("CORPUS RECONSTRUCTION SUMMARY")
    print("=============================")
    print(f"Completed this run: {complete}")
    print(f"Resumed/skipped:    {skipped}")
    print(f"Failed:             {failed}")
    print(f"Runtime:            {runtime / 60:.1f} min")
    print(f"State:              {state_path}")
    print(f"Summary:            {summary_path}")
    if failed and not args.allow_failures:
        return 1
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input-root", type=Path, action="append", required=True)
    p.add_argument("--output-root", type=Path, required=True)
    p.add_argument("--compiler", type=Path, required=True)
    p.add_argument("--python", type=Path, default=Path(sys.executable))
    p.add_argument("--flight-record-root", type=Path, required=True)
    p.add_argument("--media-root", type=Path, action="append", required=True)
    p.add_argument("--telemetry-cache-dir", type=Path, required=True)
    p.add_argument("--visual-cache-dir", type=Path, required=True)
    p.add_argument("--visual-helper", type=Path, required=True)
    p.add_argument("--visual-fps", type=float, default=1.0)
    p.add_argument("--visual-width", type=int, default=256)
    p.add_argument("--min-duration", type=float, default=3.0)
    p.add_argument("--target-duration", type=float, default=12.0)
    p.add_argument("--max-duration", type=float, default=20.0)
    p.add_argument("--max-extension-each-side", type=float, default=5.0)
    p.add_argument("--transition-pad", type=float, default=0.50)
    p.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--fail-fast", action="store_true")
    p.add_argument(
        "--allow-failures",
        action="store_true",
        help=(
            "Record failed shards but return success after processing all others. "
            "Useful for resumable partial-corpus production."
        ),
    )
    p.add_argument("--limit", type=int)
    return p


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
