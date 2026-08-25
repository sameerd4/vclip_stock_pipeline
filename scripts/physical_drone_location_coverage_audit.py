#!/usr/bin/env python3
"""Physical manifest location coverage audit (no forensic overlay)."""

from __future__ import annotations

import argparse
from pathlib import Path

from vclip_pipeline.workflow.physical_location_coverage import (
    build_physical_audit,
    write_physical_audit,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-root",
        type=Path,
        default=Path(
            "/Users/sameer/Desktop/vclip-work/work/review-shards-location-final"
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("/Users/sameer/Desktop/vclip-work/work/vclip.sqlite3"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(
            "/Users/sameer/Desktop/vclip-work/work/library-audits/"
            "physical-drone-location-coverage.json"
        ),
    )
    parser.add_argument(
        "--text-report",
        type=Path,
        default=Path(
            "/Users/sameer/Desktop/vclip-work/work/library-audits/"
            "physical-drone-location-coverage.txt"
        ),
    )
    args = parser.parse_args()
    audit = build_physical_audit(
        input_root=args.input_root.expanduser().resolve(),
        db_path=args.db.expanduser().resolve(),
    )
    write_physical_audit(
        audit,
        report=args.report.expanduser().resolve(),
        text_report=args.text_report.expanduser().resolve(),
    )
    phys = audit["physical_shard_state"]
    print(f"JSON report: {args.report}")
    print(f"Text report: {args.text_report}")
    print(
        f"Physical known/unknown: {phys['known_location']}/{phys['unknown_location']} "
        f"(unresolved drone={phys['accepted_unresolved_drone']}, "
        f"oos={phys['out_of_scope_non_drone']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
