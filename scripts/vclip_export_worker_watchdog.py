#!/usr/bin/env python3
"""
Drop-in resilient wrapper for scripts/vclip_export_worker.py.

The existing worker launches the Swift AX helper with capture_output=True.
That means a wedged Final Cut Share interaction looks like:

    driving Final Cut...
    <nothing forever>

until a human hits Ctrl-C.

This wrapper automates exactly that recovery. It runs ONE underlying batch at a
time, watches the worker's live output, and restarts the batch if:

- the AX/share phase does not return within --ax-timeout seconds, or
- Share claims to have started but Final Cut produces zero output for
  --zero-output-timeout seconds.

Completed receipts remain resumable. Actual render/ffprobe failures still fail
closed rather than being silently accepted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v"}
PROGRESS_RE = re.compile(r"exports present\s+(\d+)/(\d+)")


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def receipt_valid(path: Path) -> bool:
    data = read_json(path)
    if not data or data.get("status") != "complete":
        return False
    files = data.get("files") or []
    return bool(files) and all(
        item.get("path") and Path(item["path"]).is_file()
        for item in files
    )


def output_videos(path: Path) -> list[Path]:
    if not path.is_dir():
        return []
    return sorted(
        child
        for child in path.iterdir()
        if child.is_file() and child.suffix.lower() in VIDEO_EXTENSIONS
    )


def stop_process_group(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGINT)
    except ProcessLookupError:
        return

    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def reset_final_cut_ui(delay: float) -> None:
    subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "Final Cut Pro" to activate',
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Best-effort Escape. Failure here is harmless; the important reset is
    # killing the stuck AX helper and starting a fresh worker process.
    subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "System Events" to key code 53',
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(delay)


def run_underlying_attempt(
    *,
    command: list[str],
    batch: dict[str, Any],
    ax_timeout: float,
    zero_output_timeout: float,
) -> tuple[int | None, str | None]:
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
        env=os.environ.copy(),
    )
    assert proc.stdout is not None

    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)

    ax_started_at: float | None = None
    render_started_at: float | None = None
    first_zero_at: float | None = None

    try:
        while True:
            now = time.monotonic()

            if (
                ax_started_at is not None
                and render_started_at is None
                and now - ax_started_at > ax_timeout
            ):
                reason = (
                    f"AX/share phase stalled for >{ax_timeout:.0f}s "
                    f"on {batch['batch_id']}"
                )
                stop_process_group(proc)
                return None, reason

            if (
                render_started_at is not None
                and first_zero_at is not None
                and now - first_zero_at > zero_output_timeout
            ):
                reason = (
                    f"Share returned but no output appeared for "
                    f">{zero_output_timeout:.0f}s on {batch['batch_id']}"
                )
                stop_process_group(proc)
                return None, reason

            events = selector.select(timeout=0.5)

            for key, _mask in events:
                line = key.fileobj.readline()
                if not line:
                    continue

                print(line, end="", flush=True)

                if "driving Final Cut..." in line:
                    ax_started_at = time.monotonic()
                    render_started_at = None
                    first_zero_at = None

                if "waiting for Final Cut renders..." in line:
                    render_started_at = time.monotonic()
                    ax_started_at = None
                    first_zero_at = None

                match = PROGRESS_RE.search(line)
                if match:
                    present = int(match.group(1))
                    if present == 0:
                        if first_zero_at is None:
                            first_zero_at = time.monotonic()
                    else:
                        first_zero_at = None

            code = proc.poll()
            if code is not None:
                # Drain any buffered tail.
                for line in proc.stdout:
                    print(line, end="", flush=True)
                return code, None
    finally:
        selector.close()
        if proc.poll() is None:
            stop_process_group(proc)


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
    p.add_argument("--recovery-timeout", type=float, default=120)
    p.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--start-batch", type=int)
    p.add_argument("--limit-batches", type=int)

    p.add_argument(
        "--real-worker",
        type=Path,
        help=(
            "Underlying stock worker. Defaults to vclip_export_worker.py "
            "beside this wrapper if present, otherwise the repo scripts path."
        ),
    )
    p.add_argument("--share-attempts", type=int, default=4)
    p.add_argument(
        "--ax-timeout",
        type=float,
        default=180,
        help="Seconds allowed between 'driving Final Cut' and render wait.",
    )
    p.add_argument(
        "--zero-output-timeout",
        type=float,
        default=180,
        help="Seconds allowed after Share before the first output appears.",
    )
    p.add_argument("--retry-delay", type=float, default=4)
    return p


def main() -> int:
    args = parser().parse_args()

    manifest_path = args.manifest.expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    batches = sorted(
        manifest["batches"],
        key=lambda b: int(b["batch_index"]),
    )
    if args.start_batch:
        batches = [
            b
            for b in batches
            if int(b["batch_index"]) >= args.start_batch
        ]
    if args.limit_batches:
        batches = batches[: args.limit_batches]

    if not batches:
        print("No batches selected.")
        return 0

    if args.real_worker:
        real_worker = args.real_worker.expanduser().resolve()
    else:
        here = Path(__file__).resolve().parent
        beside = here / "vclip_export_worker.py"
        repo_candidate = Path.cwd() / "scripts" / "vclip_export_worker.py"
        if beside.is_file():
            real_worker = beside
        elif repo_candidate.is_file():
            real_worker = repo_candidate.resolve()
        else:
            raise SystemExit(
                "Could not locate vclip_export_worker.py; pass --real-worker."
            )

    print("VClip resilient export worker")
    print("============================")
    print("Plan:          ", manifest["plan_id"])
    print("Batches:       ", len(batches))
    print("Share attempts:", args.share_attempts)
    print("AX timeout:    ", f"{args.ax_timeout:.0f}s")
    print("Zero output:   ", f"{args.zero_output_timeout:.0f}s")
    print("Real worker:   ", real_worker)
    print()

    for ordinal, batch in enumerate(batches, 1):
        receipt_path = Path(batch["receipt_path"])

        if args.resume and receipt_valid(receipt_path):
            print(
                f"[{ordinal:03d}/{len(batches):03d}] "
                f"{batch['batch_id']} receipt already complete"
            )
            continue

        last_reason = ""

        for attempt in range(1, args.share_attempts + 1):
            print(
                f"[{ordinal:03d}/{len(batches):03d}] "
                f"{batch['batch_id']} watchdog attempt "
                f"{attempt}/{args.share_attempts}",
                flush=True,
            )

            command = [
                sys.executable,
                str(real_worker),
                "--manifest",
                str(manifest_path),
                "--ax-binary",
                str(args.ax_binary.expanduser().resolve()),
                "--start-batch",
                str(batch["batch_index"]),
                "--limit-batches",
                "1",
                "--ffprobe",
                args.ffprobe,
                "--timeout",
                str(args.timeout),
                "--stable-seconds",
                str(args.stable_seconds),
                "--poll-seconds",
                str(args.poll_seconds),
                "--duration-tolerance",
                str(args.duration_tolerance),
                "--recovery-timeout",
                str(args.recovery_timeout),
            ]

            if args.db:
                command.extend(
                    ["--db", str(args.db.expanduser().resolve())]
                )

            if not args.resume:
                command.append("--no-resume")

            code, watchdog_reason = run_underlying_attempt(
                command=command,
                batch=batch,
                ax_timeout=args.ax_timeout,
                zero_output_timeout=args.zero_output_timeout,
            )

            if code == 0 and receipt_valid(receipt_path):
                print(
                    f"    watchdog PASS: {batch['batch_id']}",
                    flush=True,
                )
                last_reason = ""
                break

            if watchdog_reason:
                last_reason = watchdog_reason
            else:
                last_reason = (
                    f"underlying worker exited {code} without a complete receipt"
                )

            videos = output_videos(Path(batch["output_directory"]))

            print(
                f"    watchdog retry: {last_reason}; "
                f"existing outputs={len(videos)}",
                flush=True,
            )

            if attempt >= args.share_attempts:
                break

            reset_final_cut_ui(args.retry_delay)

        if last_reason:
            print()
            print(
                f"RESILIENT WORKER FAILED: {batch['batch_id']}: "
                f"{last_reason}",
                flush=True,
            )
            print(
                "Staging library and any rendered files are preserved.",
                flush=True,
            )
            return 2

    print()
    print(f"Resilient batches complete: {len(batches)}/{len(batches)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
