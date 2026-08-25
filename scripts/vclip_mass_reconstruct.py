#!/usr/bin/env python3
"""Simple, resumable mass runner for VClip reconstructed shards.

This intentionally does ONE job:
    staged resolvable FCPXML shards -> reconstructed FCPXML shards

It does not preflight drives, stage media, dedupe, write the DB, or automate
Final Cut export. Those are separate steps after reconstruction is proven.

Defaults target the already-created available-now staging corpus:
  ~/Desktop/vclip-work/work/reconstruction-available-inputs-v1/
      review-shards-location-final/

The runner invokes scripts/vclip_reconstruct_shard.py using the known-good DJI
Python environment (~/.venvs/pydjirecord/bin/python), writes one log per shard,
continues past failures, and records resumable state.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REPO = Path("/Users/sameer/Downloads/vclip_stock_pipeline")
DEFAULT_WORK = Path("/Users/sameer/Desktop/vclip-work/work")
DEFAULT_PYTHON = Path.home() / ".venvs/pydjirecord/bin/python"
DEFAULT_INPUT = (
    DEFAULT_WORK
    / "reconstruction-available-inputs-v1"
    / "review-shards-location-final"
)
DEFAULT_OUTPUT = DEFAULT_WORK / "reconstructed-simple-v1"
DEFAULT_FLIGHTS = Path.home() / "Documents/Drone Flight Records"
DEFAULT_TELEMETRY_CACHE = DEFAULT_WORK / "telemetry-qc"
DEFAULT_VISUAL_CACHE = DEFAULT_WORK / "visual-coherence-cache-simple-v1"
DEFAULT_VISION_HELPER = DEFAULT_WORK / "bin/vclip-vision-featureprint"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_key(path: Path) -> str:
    stat = path.stat()
    payload = f"{path.resolve()}\n{stat.st_size}\n{stat.st_mtime_ns}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def load_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    tmp.replace(path)


def require_dji_python(python: Path) -> None:
    if not python.is_file():
        raise SystemExit(f"DJI Python not found: {python}")
    result = subprocess.run(
        [str(python), "-c", "import pydjirecord; print('OK')"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"{python} cannot import pydjirecord:\n{result.stderr.strip()}"
        )


def ensure_api_key(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    value = getpass.getpass("DJI App Key: ").strip()
    if not value:
        raise SystemExit("DJI App Key is required.")
    os.environ[name] = value
    return value


def tail(path: Path, lines: int = 35) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:])
    except Exception:
        return "(could not read log)"


def run(args: argparse.Namespace) -> int:
    repo = args.repo.expanduser().resolve()
    input_root = args.input_root.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    # IMPORTANT: do NOT Path.resolve() a virtualenv interpreter.
    # On macOS the venv's bin/python is a symlink to the Framework Python.
    # Resolving the symlink and executing the target bypasses the venv's
    # site-packages, which is where pydjirecord is installed.
    python = args.python.expanduser()
    compiler = (repo / "scripts/vclip_reconstruct_shard.py").resolve()

    require_dji_python(python)
    ensure_api_key(args.api_key_env)

    if not compiler.is_file():
        raise SystemExit(f"Missing compiler: {compiler}")
    if not input_root.is_dir():
        raise SystemExit(f"Missing staged input root: {input_root}")
    if not args.flight_record_root.expanduser().is_dir():
        raise SystemExit(f"Missing flight record root: {args.flight_record_root}")
    if not args.vision_helper.expanduser().is_file():
        raise SystemExit(f"Missing Vision helper: {args.vision_helper}")

    shards = sorted(input_root.rglob("*.fcpxml"))
    if args.contains:
        shards = [p for p in shards if args.contains.casefold() in str(p).casefold()]
    if args.start_after:
        marker = args.start_after.casefold()
        start_index = next(
            (i + 1 for i, p in enumerate(shards) if marker in str(p).casefold()),
            None,
        )
        if start_index is None:
            raise SystemExit(f"--start-after did not match any shard: {args.start_after}")
        shards = shards[start_index:]
    if args.limit is not None:
        shards = shards[: args.limit]

    if not shards:
        raise SystemExit("No staged FCPXML shards found.")

    raw_root = output_root / "raw"
    reports_root = output_root / "reports"
    logs_root = output_root / "logs"
    state_path = output_root / "state.json"
    summary_path = output_root / "summary.json"

    raw_root.mkdir(parents=True, exist_ok=True)
    reports_root.mkdir(parents=True, exist_ok=True)
    logs_root.mkdir(parents=True, exist_ok=True)

    state = load_json(state_path, {"version": 1, "shards": {}})
    state.setdefault("shards", {})

    env = os.environ.copy()
    env["PYTHONPATH"] = (
        f"{repo / 'src'}:{repo / 'scripts'}"
        + (f":{env['PYTHONPATH']}" if env.get("PYTHONPATH") else "")
    )

    print("VClip simple mass reconstruction")
    print("================================")
    print(f"Python:          {python}")
    print("pydjirecord:     import OK")
    print(f"Compiler:        {compiler}")
    print(f"Input root:      {input_root}")
    print(f"Shards selected: {len(shards)}")
    print(f"Output root:     {output_root}")
    print(f"Visual sampling: {args.visual_fps:g} fps @ {args.visual_width}px")
    print(f"Resume:          {not args.no_resume}")
    print()

    run_started = time.monotonic()
    completed = 0
    skipped = 0
    failed = 0

    for number, shard in enumerate(shards, 1):
        rel = shard.relative_to(input_root)
        key = rel.as_posix()
        fingerprint = stable_key(shard)

        out_xml = raw_root / rel.parent / f"{shard.stem}--reconstructed.fcpxml"
        report = reports_root / rel.parent / f"{shard.stem}--reconstruction.json"
        log = logs_root / rel.parent / f"{shard.stem}.log"

        prior = state["shards"].get(key, {})
        can_resume = (
            not args.no_resume
            and prior.get("status") == "complete"
            and prior.get("input_fingerprint") == fingerprint
            and out_xml.is_file()
            and report.is_file()
        )

        print(f"[{number:03d}/{len(shards):03d}] {rel}")
        if can_resume:
            print("    SKIP: already complete")
            skipped += 1
            continue

        out_xml.parent.mkdir(parents=True, exist_ok=True)
        report.parent.mkdir(parents=True, exist_ok=True)
        log.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            str(python),
            str(compiler),
            "--input", str(shard),
            "--output", str(out_xml),
            "--report", str(report),
            "--flight-record-root", str(args.flight_record_root.expanduser()),
            "--cache-dir", str(args.telemetry_cache_dir.expanduser()),
            "--visual-helper", str(args.vision_helper.expanduser()),
            "--visual-cache-dir", str(args.visual_cache_dir.expanduser()),
            "--visual-fps", str(args.visual_fps),
            "--visual-width", str(args.visual_width),
            "--min-duration", str(args.min_duration),
            "--target-duration", str(args.target_duration),
            "--max-duration", str(args.max_duration),
            "--max-extension-each-side", str(args.max_extension_each_side),
            "--transition-pad", str(args.transition_pad),
            "--media-root", "/Volumes",
        ]

        started = time.monotonic()
        with log.open("w", encoding="utf-8") as fh:
            fh.write("COMMAND\n=======\n")
            fh.write(" ".join(cmd) + "\n\n")
            fh.flush()
            proc = subprocess.run(
                cmd,
                stdout=fh,
                stderr=subprocess.STDOUT,
                env=env,
                text=True,
            )
        elapsed = time.monotonic() - started

        if proc.returncode == 0 and out_xml.is_file() and report.is_file():
            completed += 1
            state["shards"][key] = {
                "status": "complete",
                "input_fingerprint": fingerprint,
                "input": str(shard),
                "output": str(out_xml),
                "report": str(report),
                "log": str(log),
                "completed_at": utc_now(),
                "runtime_seconds": round(elapsed, 3),
            }
            print(f"    COMPLETE in {elapsed / 60:.1f} min")
        else:
            failed += 1
            state["shards"][key] = {
                "status": "failed",
                "input_fingerprint": fingerprint,
                "input": str(shard),
                "output": str(out_xml),
                "report": str(report),
                "log": str(log),
                "failed_at": utc_now(),
                "runtime_seconds": round(elapsed, 3),
                "returncode": proc.returncode,
            }
            print(f"    FAILED rc={proc.returncode} after {elapsed:.1f}s")
            print("    ---- log tail ----")
            for line in tail(log).splitlines():
                print(f"    {line}")
            print("    ------------------")
            if args.fail_fast:
                save_json(state_path, state)
                break

        save_json(state_path, state)
        print()

    runtime = time.monotonic() - run_started
    summary = {
        "generated_at": utc_now(),
        "input_root": str(input_root),
        "output_root": str(output_root),
        "selected_shards": len(shards),
        "completed_this_run": completed,
        "resumed_skipped": skipped,
        "failed_this_run": failed,
        "runtime_seconds": round(runtime, 3),
        "complete_total": sum(
            1 for row in state["shards"].values() if row.get("status") == "complete"
        ),
        "failed_total": sum(
            1 for row in state["shards"].values() if row.get("status") == "failed"
        ),
    }
    save_json(state_path, state)
    save_json(summary_path, summary)

    print("SUMMARY")
    print("=======")
    print(f"Completed this run: {completed}")
    print(f"Resumed/skipped:    {skipped}")
    print(f"Failed this run:     {failed}")
    print(f"Runtime:             {runtime / 60:.1f} min")
    print(f"State:               {state_path}")
    print(f"Summary:             {summary_path}")

    return 1 if failed and args.fail_fast else 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    p.add_argument("--input-root", type=Path, default=DEFAULT_INPUT)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--python", type=Path, default=DEFAULT_PYTHON)
    p.add_argument("--flight-record-root", type=Path, default=DEFAULT_FLIGHTS)
    p.add_argument("--telemetry-cache-dir", type=Path, default=DEFAULT_TELEMETRY_CACHE)
    p.add_argument("--visual-cache-dir", type=Path, default=DEFAULT_VISUAL_CACHE)
    p.add_argument("--vision-helper", type=Path, default=DEFAULT_VISION_HELPER)
    p.add_argument("--api-key-env", default="DJI_API_KEY")

    p.add_argument("--visual-fps", type=float, default=1.0)
    p.add_argument("--visual-width", type=int, default=256)
    p.add_argument("--min-duration", type=float, default=3.0)
    p.add_argument("--target-duration", type=float, default=12.0)
    p.add_argument("--max-duration", type=float, default=20.0)
    p.add_argument("--max-extension-each-side", type=float, default=5.0)
    p.add_argument("--transition-pad", type=float, default=0.5)

    p.add_argument("--limit", type=int)
    p.add_argument("--contains")
    p.add_argument("--start-after")
    p.add_argument("--fail-fast", action="store_true")
    p.add_argument("--no-resume", action="store_true")
    return p


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
