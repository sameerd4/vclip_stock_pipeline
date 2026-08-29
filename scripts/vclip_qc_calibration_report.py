#!/usr/bin/env python3
"""
Compare VClip OpenAI production-QC results against the user's manual
KEEP / MAYBE / REJECT proxy triage.

No API calls. No mutations.
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter, defaultdict
from pathlib import Path

VCLIP_RE = re.compile(r"VCLIP_[0-9A-F]{12,64}")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--results", type=Path, required=True)
    p.add_argument("--triage-root", type=Path, required=True)
    p.add_argument("--output-root", type=Path, required=True)
    return p.parse_args()


def main() -> int:
    a = parse_args()
    results_path = a.results.expanduser().resolve()
    triage_root = a.triage_root.expanduser().resolve()
    output_root = a.output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    labels: dict[str, str] = {}
    for label in ("KEEP", "MAYBE", "REJECT"):
        folder = triage_root / label
        if not folder.is_dir():
            raise SystemExit(f"Missing triage folder: {folder}")
        for path in folder.glob("*.mp4"):
            m = VCLIP_RE.search(path.name)
            if not m:
                continue
            sid = m.group(0)
            if sid in labels and labels[sid] != label:
                raise SystemExit(f"Duplicate human label for {sid}")
            labels[sid] = label

    with results_path.open(newline="", encoding="utf-8") as f:
        results = list(csv.DictReader(f))

    joined = []
    missing = []
    for row in results:
        sid = row["stock_clip_id"]
        human = labels.get(sid)
        if human is None:
            missing.append(sid)
            continue
        joined.append(
            {
                "stock_clip_id": sid,
                "human_label": human,
                "final_decision": row.get("final_decision", ""),
                "person_presence": row.get("person_presence", ""),
                "likely_operator_present": row.get("likely_operator_present", ""),
                "camera_repositioning": row.get("camera_repositioning", ""),
                "composition_discontinuity": row.get(
                    "composition_discontinuity", ""
                ),
                "stock_usability": row.get("stock_usability", ""),
                "confidence": row.get("confidence", ""),
                "reasons": row.get("reasons", ""),
                "notes": row.get("notes", ""),
            }
        )

    fields = list(joined[0].keys()) if joined else []
    with (output_root / "human-vs-openai.csv").open(
        "w", newline="", encoding="utf-8"
    ) as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(joined)

    cross = Counter((r["human_label"], r["final_decision"]) for r in joined)
    human_counts = Counter(r["human_label"] for r in joined)
    person_by_human = Counter(
        (r["human_label"], r["person_presence"]) for r in joined
    )
    reposition_by_human = Counter(
        (r["human_label"], r["camera_repositioning"]) for r in joined
    )

    print("VCLIP QC CALIBRATION")
    print("====================")
    print("human-labeled:", len(labels))
    print("model results:", len(results))
    print("joined       :", len(joined))
    print("missing label:", len(missing))
    print()
    print("HUMAN LABELS")
    print("------------")
    for label in ("KEEP", "MAYBE", "REJECT"):
        print(f"{human_counts[label]:4d}  {label}")

    print()
    print("HUMAN x FINAL DECISION")
    print("----------------------")
    for label in ("KEEP", "MAYBE", "REJECT"):
        print(label)
        rows = [(decision, n) for (h, decision), n in cross.items() if h == label]
        for decision, n in sorted(rows, key=lambda x: (-x[1], x[0])):
            print(f"  {n:4d}  {decision}")

    print()
    print("PERSON PRESENCE BY HUMAN LABEL")
    print("------------------------------")
    for label in ("KEEP", "MAYBE", "REJECT"):
        vals = [
            (value, n)
            for (h, value), n in person_by_human.items()
            if h == label
        ]
        print(label, dict(sorted(vals)))

    print()
    print("REPOSITIONING BY HUMAN LABEL")
    print("----------------------------")
    for label in ("KEEP", "MAYBE", "REJECT"):
        vals = [
            (value, n)
            for (h, value), n in reposition_by_human.items()
            if h == label
        ]
        print(label, dict(sorted(vals)))

    print()
    print("REJECT DETAILS")
    print("--------------")
    for row in joined:
        if row["human_label"] != "REJECT":
            continue
        print(
            f"{row['stock_clip_id']}  "
            f"decision={row['final_decision']}  "
            f"person={row['person_presence']}  "
            f"operator={row['likely_operator_present']}  "
            f"reposition={row['camera_repositioning']}  "
            f"reasons={row['reasons']}"
        )

    print()
    print("output:", output_root / "human-vs-openai.csv")
    print("VCLIP QC CALIBRATION: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
