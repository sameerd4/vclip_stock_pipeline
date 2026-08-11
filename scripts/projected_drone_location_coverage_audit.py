#!/usr/bin/env python3
"""Read-only Projected Drone Location Coverage Audit for a review-shard corpus."""

from __future__ import annotations

import argparse
from pathlib import Path

from vclip_pipeline.util import json_dumps
from vclip_pipeline.workflow.projected_location_coverage import (
    build_audit,
    format_text,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-root",
        type=Path,
        default=Path("/Users/sameer/Desktop/vclip-work/work/review-shards-t9-recovery"),
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("/Users/sameer/Desktop/vclip-work/work/vclip.sqlite3"),
    )
    parser.add_argument(
        "--forensic-json",
        type=Path,
        default=Path(
            "/Users/sameer/Desktop/vclip-work/work/library-audits/jpg-exif-forensic.json"
        ),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(
            "/Users/sameer/Desktop/vclip-work/work/library-audits/"
            "projected-drone-location-coverage.json"
        ),
    )
    parser.add_argument(
        "--text-report",
        type=Path,
        default=Path(
            "/Users/sameer/Desktop/vclip-work/work/library-audits/"
            "projected-drone-location-coverage.txt"
        ),
    )
    args = parser.parse_args()
    audit = build_audit(
        input_root=args.input_root.expanduser().resolve(),
        db_path=args.db.expanduser().resolve(),
        forensic_path=args.forensic_json.expanduser().resolve(),
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json_dumps(audit), encoding="utf-8")
    args.text_report.write_text(format_text(audit), encoding="utf-8")
    print(f"JSON report: {args.report}")
    print(f"Text report: {args.text_report}")
    print(
        "Projected drone unresolved:",
        audit["drone_only"]["by_projected_state"].get("accepted_unresolved_drone"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
