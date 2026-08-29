#!/usr/bin/env python3
"""
Finalize OpenAI production-QC results for the structurally-clear VClip
MASTER REVIEW tranche.

This script makes NO API calls and mutates nothing. It joins:
  1. OpenAI results.csv
  2. the structurally-clear candidate CSV
  3. master-review-all.csv (authoritative telemetry/operator/visual facts)

It emits:
  - promotion-ready.csv
  - human-review.csv
  - rejected-person.csv
  - all-finalized.csv
  - promotion-ids.txt
  - summary.json

Promotion requires BOTH:
  - the deterministic strong-candidate facts, and
  - OpenAI visual clearance from obvious production blockers.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path


PROMOTE_DECISIONS = {
    "PROMOTE_AFTER_SPOTCHECK",
    "OPENAI_VISUAL_CLEAR",
}

REVIEW_DECISIONS = {
    "REVIEW_PERSON",
    "REVIEW_VISUAL_OBSTRUCTION",
    "REVIEW_OTHER_VISUAL",
    "REVIEW_REPOSITIONING",
    "REVIEW_MOVEMENT",
    "REVIEW_SOURCE_OVERLAP",
    "PROMOTE_AFTER_HUMAN_SPOTCHECK",
    "REVIEW",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def f(value: str | None) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def truthy(value: str | None) -> bool:
    return (value or "").strip().upper() in {"YES", "TRUE", "1"}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--results", type=Path, required=True)
    p.add_argument("--strong", type=Path, required=True)
    p.add_argument("--master-review", type=Path, required=True)
    p.add_argument("--output-root", type=Path, required=True)
    return p


def main() -> int:
    args = parser().parse_args()

    results_path = args.results.expanduser().resolve()
    strong_path = args.strong.expanduser().resolve()
    master_path = args.master_review.expanduser().resolve()
    out = args.output_root.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    results = read_csv(results_path)
    strong = read_csv(strong_path)
    masters = read_csv(master_path)

    result_by_id = {r["stock_clip_id"]: r for r in results}
    strong_by_id = {r["stock_clip_id"]: r for r in strong}
    master_by_id = {r["stock_clip_id"]: r for r in masters}

    if len(result_by_id) != len(results):
        raise SystemExit("Duplicate stock_clip_id in OpenAI results")
    if len(strong_by_id) != len(strong):
        raise SystemExit("Duplicate stock_clip_id in strong candidate CSV")
    if len(master_by_id) != len(masters):
        raise SystemExit("Duplicate stock_clip_id in master-review CSV")

    missing_results = sorted(set(strong_by_id) - set(result_by_id))
    extra_results = sorted(set(result_by_id) - set(strong_by_id))
    missing_master = sorted(set(strong_by_id) - set(master_by_id))

    if missing_results:
        raise SystemExit(
            f"Missing OpenAI results for {len(missing_results)} strong IDs: "
            + ", ".join(missing_results[:20])
        )
    if extra_results:
        raise SystemExit(
            f"OpenAI results include {len(extra_results)} IDs outside strong set: "
            + ", ".join(extra_results[:20])
        )
    if missing_master:
        raise SystemExit(
            f"Missing master-review rows for {len(missing_master)} IDs: "
            + ", ".join(missing_master[:20])
        )

    finalized: list[dict[str, str]] = []

    for sid in sorted(strong_by_id):
        s = strong_by_id[sid]
        r = result_by_id[sid]
        m = master_by_id[sid]

        deterministic_reasons: list[str] = []

        if (m.get("qc_status") or "").upper() != "PASS":
            deterministic_reasons.append("telemetry_not_pass")

        if (m.get("operator_status") or "").upper() != "CLEAN":
            deterministic_reasons.append("operator_not_clean")

        if (m.get("visual_status") or "").upper() not in {
            "ADVISORY",
            "NO_VISUAL",
            "COHERENT",
        }:
            deterministic_reasons.append("visual_transition_or_unknown")

        if f(m.get("duration_s")) < 5.0:
            deterministic_reasons.append("duration_under_5s")

        relation = (
            s.get("best_existing_relation")
            or s.get("best_ready_relation")
            or m.get("best_ready_relation")
            or ""
        ).upper()

        if relation not in {
            "DISJOINT",
            "NO_EXISTING_ON_SOURCE",
            "NO_READY_ON_SOURCE",
        }:
            deterministic_reasons.append("not_source_additive")

        if not truthy(s.get("treatment_matches_parent")):
            deterministic_reasons.append("treatment_not_confirmed")

        if f(s.get("custom_lut_count")) <= 0:
            deterministic_reasons.append("custom_lut_missing")

        decision = (r.get("final_decision") or "").upper()
        stock_usability = (r.get("stock_usability") or "").lower()
        person_presence = (r.get("person_presence") or "").lower()
        obstruction = truthy(r.get("visual_obstruction"))
        discontinuity = truthy(r.get("composition_discontinuity"))

        if deterministic_reasons:
            final_bucket = "HOLD_DETERMINISTIC_MISMATCH"

        elif decision == "REJECT_PERSON":
            final_bucket = "REJECT_PERSON"

        elif decision in REVIEW_DECISIONS:
            final_bucket = "HUMAN_REVIEW"

        elif decision in PROMOTE_DECISIONS:
            # Conservative second check on the raw OpenAI fields.
            if (
                stock_usability == "pass"
                and person_presence in {"none", "tiny_background"}
                and not obstruction
                and not discontinuity
            ):
                final_bucket = "PROMOTION_READY"
            else:
                final_bucket = "HUMAN_REVIEW"

        else:
            final_bucket = "HUMAN_REVIEW"

        finalized.append(
            {
                "stock_clip_id": sid,
                "final_bucket": final_bucket,
                "openai_decision": r.get("final_decision", ""),
                "scope": s.get("scope", ""),
                "project_name": s.get("project_name", ""),
                "source_name": s.get("source_name", ""),
                "start_s": s.get("start_s", ""),
                "duration_s": s.get("duration_s", ""),
                "best_existing_relation": relation,
                "qc_status": m.get("qc_status", ""),
                "operator_status": m.get("operator_status", ""),
                "visual_status": m.get("visual_status", ""),
                "custom_lut_count": s.get("custom_lut_count", ""),
                "treatment_matches_parent": s.get(
                    "treatment_matches_parent", ""
                ),
                "person_presence": r.get("person_presence", ""),
                "person_frame_hits": r.get("person_frame_hits", ""),
                "likely_operator_present": r.get(
                    "likely_operator_present", ""
                ),
                "camera_repositioning": r.get(
                    "camera_repositioning", ""
                ),
                "composition_discontinuity": r.get(
                    "composition_discontinuity", ""
                ),
                "visual_obstruction": r.get(
                    "visual_obstruction", ""
                ),
                "stock_usability": r.get("stock_usability", ""),
                "confidence": r.get("confidence", ""),
                "reasons": r.get("reasons", ""),
                "notes": r.get("notes", ""),
                "deterministic_mismatch_reasons": "|".join(
                    deterministic_reasons
                ),
                "xml_path": s.get("xml_path", ""),
                "event_name": s.get("event_name", ""),
            }
        )

    fields = list(finalized[0].keys())

    def write_subset(name: str, rows: list[dict[str, str]]) -> None:
        path = out / name
        with path.open("w", newline="", encoding="utf-8") as fobj:
            writer = csv.DictWriter(fobj, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    write_subset("all-finalized.csv", finalized)

    promotion = [
        row for row in finalized if row["final_bucket"] == "PROMOTION_READY"
    ]
    review = [
        row for row in finalized if row["final_bucket"] == "HUMAN_REVIEW"
    ]
    rejected = [
        row for row in finalized if row["final_bucket"] == "REJECT_PERSON"
    ]
    mismatch = [
        row
        for row in finalized
        if row["final_bucket"] == "HOLD_DETERMINISTIC_MISMATCH"
    ]

    write_subset("promotion-ready.csv", promotion)
    write_subset("human-review.csv", review)
    write_subset("rejected-person.csv", rejected)
    write_subset("deterministic-holds.csv", mismatch)

    (out / "promotion-ids.txt").write_text(
        "".join(row["stock_clip_id"] + "\n" for row in promotion),
        encoding="utf-8",
    )

    bucket_counts = Counter(row["final_bucket"] for row in finalized)
    person_counts = Counter(row["person_presence"] for row in finalized)

    summary = {
        "input_candidates": len(strong),
        "openai_results": len(results),
        "final_buckets": dict(bucket_counts),
        "person_presence": dict(person_counts),
        "promotion_ids": len(promotion),
        "review_ids": len(review),
        "rejected_person_ids": len(rejected),
        "deterministic_holds": len(mismatch),
    }
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )

    print("VCLIP OPENAI PROMOTION MANIFEST")
    print("===============================")
    print("strong candidates   :", len(strong))
    print("OpenAI results      :", len(results))
    print()
    print("FINAL BUCKETS")
    print("-------------")
    for bucket, count in bucket_counts.most_common():
        print(f"{count:4d}  {bucket}")
    print()
    print("PERSON PRESENCE")
    print("---------------")
    for value, count in person_counts.most_common():
        print(f"{count:4d}  {value}")
    print()
    print("promotion :", out / "promotion-ready.csv")
    print("review    :", out / "human-review.csv")
    print("rejected  :", out / "rejected-person.csv")
    print("ids       :", out / "promotion-ids.txt")
    print("summary   :", out / "summary.json")
    print()
    print("VCLIP OPENAI PROMOTION MANIFEST: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
