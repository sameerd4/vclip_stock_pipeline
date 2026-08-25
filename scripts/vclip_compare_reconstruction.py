#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_BASE = Path(
    "/Users/sameer/Desktop/vclip-work/work/reconstructed-simple-v1/"
    "reports/may-2026-california/"
    "may-2026-california-restockified-review--california-pleasure-point--01--reconstruction.json"
)
DEFAULT_FAST = Path(
    "/Users/sameer/Desktop/vclip-work/work/reconstructed-lrf-fast-test/"
    "reports/may-2026-california/"
    "may-2026-california-restockified-review--california-pleasure-point--01--reconstruction.json"
)


COUNT_KEYS = [
    "anchors_total",
    "known_non_drone_excluded",
    "source_video_count",
    "source_with_flight_telemetry",
    "historical_original_count",
    "ready_project_count",
    "extended_master_count",
    "repair_candidate_count",
    "qc_review_project_count",
]


def load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(f"Missing report: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def norm_ready(rows: list[dict[str, Any]]) -> list[tuple]:
    # Compare product decisions, not implementation diagnostics such as raw
    # Vision distances or cache paths.
    out = []
    for r in rows:
        out.append(
            (
                str(r.get("parent_id", "")),
                str(r.get("bucket", "")),
                str(r.get("action", "")),
                str(r.get("source_name", "")),
                round(float(r.get("start_s", 0.0)), 4),
                round(float(r.get("duration_s", 0.0)), 4),
                str(r.get("qc_status", "")),
                str(r.get("operator_status", "")),
                str(r.get("visual_status", "")),
                str(r.get("readiness_basis", "")),
            )
        )
    return sorted(out)


def norm_masters(rows: list[dict[str, Any]]) -> list[tuple]:
    out = []
    for r in rows:
        out.append(
            (
                str(r.get("source_name", "")),
                round(float(r.get("start_s", 0.0)), 4),
                round(float(r.get("duration_s", 0.0)), 4),
                bool(r.get("promoted", False)),
                str(r.get("telemetry_status", "")),
                str(r.get("operator_status", "")),
                str(r.get("visual_status", "")),
                str(r.get("readiness_basis", "")),
                tuple(sorted(str(x) for x in (r.get("parent_ids") or []))),
            )
        )
    return sorted(out)


def print_diff(label: str, a: list[tuple], b: list[tuple], limit: int = 20) -> bool:
    sa, sb = set(a), set(b)
    only_a = sorted(sa - sb)
    only_b = sorted(sb - sa)
    if not only_a and not only_b and len(a) == len(b):
        print(f"{label}: IDENTICAL ({len(a)})")
        return True

    print(f"{label}: DIFFERENT")
    print(f"  baseline rows: {len(a)}")
    print(f"  LRF rows:      {len(b)}")
    if only_a:
        print("  only baseline:")
        for row in only_a[:limit]:
            print("   ", row)
    if only_b:
        print("  only LRF:")
        for row in only_b[:limit]:
            print("   ", row)
    return False


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--baseline", type=Path, default=DEFAULT_BASE)
    p.add_argument("--fast", type=Path, default=DEFAULT_FAST)
    args = p.parse_args()

    base = load(args.baseline)
    fast = load(args.fast)

    print("Pleasure Point reconstruction parity")
    print("====================================")
    counts_ok = True
    for key in COUNT_KEYS:
        a, b = base.get(key), fast.get(key)
        ok = a == b
        counts_ok &= ok
        print(f"{key:30s} {str(a):>8s}  {str(b):>8s}  {'OK' if ok else 'DIFF'}")

    print()
    ready_ok = print_diff(
        "Ready/QC/repair decisions",
        norm_ready(base.get("ready_variants", [])),
        norm_ready(fast.get("ready_variants", [])),
    )
    master_ok = print_diff(
        "Extended-master decisions",
        norm_masters(base.get("reconstructed_shots", [])),
        norm_masters(fast.get("reconstructed_shots", [])),
    )

    print()
    overall = counts_ok and ready_ok and master_ok
    print("DECISION PARITY:", "PASS" if overall else "FAIL")
    if overall:
        print("Alternate visual compute path changed implementation without changing product decisions.")
    else:
        print("Do not launch the full corpus yet; inspect the listed decision differences.")
    return 0 if overall else 2


if __name__ == "__main__":
    raise SystemExit(main())
