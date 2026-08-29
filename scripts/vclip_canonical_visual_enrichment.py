#!/usr/bin/env python3
"""Canonical-master visual enrichment for VClip.

This runner is keyed by canonical identity instead of Stockify run identity:

    VCLIP stock_clip_id + canonical master SHA-256
    + provider/model/taxonomy/prompt/sampler versions

It reuses VClip's current:
- six-frame deterministic sampler
- controlled VisualTaxonomy
- OpenAI Responses API visual analyzer

Dry-run is default. Pass --write to call the provider and persist results.

The schema is additive and intentionally separate from legacy clip_visual_analysis.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import sqlite3
from collections import Counter, defaultdict, deque
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vclip_pipeline.util import sha256_file, stable_id
from vclip_pipeline.workflow.frames import FrameSampler, SAMPLER_VERSION
from vclip_pipeline.workflow.providers.openai import (
    OpenAIVisualAnalyzer,
    PROMPT_VERSION,
)
from vclip_pipeline.workflow.taxonomy import VisualTaxonomy


SCHEMA = r"""
CREATE TABLE IF NOT EXISTS canonical_visual_analysis_runs (
    id TEXT PRIMARY KEY,
    canonical_catalog_path TEXT NOT NULL,
    canonical_catalog_version TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    taxonomy_version INTEGER NOT NULL,
    prompt_version TEXT NOT NULL,
    sampler_version TEXT NOT NULL,
    selection_mode TEXT NOT NULL,
    requested_limit INTEGER,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','complete','failed')),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error_text TEXT
);

CREATE TABLE IF NOT EXISTS canonical_clip_visual_analysis (
    id TEXT PRIMARY KEY,
    analysis_key TEXT NOT NULL UNIQUE,
    analysis_run_id TEXT NOT NULL,
    stock_clip_id TEXT NOT NULL,
    canonical_master_sha256 TEXT NOT NULL,
    canonical_master_relative_path TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    taxonomy_version INTEGER NOT NULL,
    prompt_version TEXT NOT NULL,
    sampler_version TEXT NOT NULL,
    caption TEXT NOT NULL,
    result_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('complete','failed')),
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    total_tokens INTEGER,
    estimated_input_cost_usd REAL,
    estimated_output_cost_usd REAL,
    estimated_total_cost_usd REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_canonical_visual_clip
ON canonical_clip_visual_analysis(stock_clip_id, updated_at);

CREATE INDEX IF NOT EXISTS idx_canonical_visual_sha
ON canonical_clip_visual_analysis(canonical_master_sha256);

CREATE TABLE IF NOT EXISTS canonical_clip_visual_tags (
    analysis_key TEXT NOT NULL,
    stock_clip_id TEXT NOT NULL,
    tag_group TEXT NOT NULL,
    tag TEXT NOT NULL,
    strength TEXT NOT NULL,
    score REAL,
    frame_hits_json TEXT NOT NULL,
    rationale TEXT,
    PRIMARY KEY(analysis_key, tag_group, tag)
);

CREATE INDEX IF NOT EXISTS idx_canonical_visual_tags_lookup
ON canonical_clip_visual_tags(tag_group, tag, stock_clip_id);

CREATE TABLE IF NOT EXISTS canonical_clip_named_subjects (
    analysis_key TEXT NOT NULL,
    stock_clip_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    confidence TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0,
    canonical_entity_id TEXT,
    canonical_label TEXT,
    resolution_source TEXT,
    PRIMARY KEY(analysis_key, subject)
);

CREATE TABLE IF NOT EXISTS canonical_clip_search_documents (
    stock_clip_id TEXT PRIMARY KEY,
    canonical_master_sha256 TEXT NOT NULL,
    document_text TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_catalog(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def is_recovery(row: dict[str, str]) -> bool:
    text = (row.get("migration_action") or "").casefold()
    return "qc_recovery" in text or "geography_fix" in text


def deterministic_key(stock_clip_id: str) -> str:
    return hashlib.sha256(
        ("canonical-enrichment-calibration-v1|" + stock_clip_id).encode("utf-8")
    ).hexdigest()


def round_robin_sample(
    rows: list[dict[str, str]],
    limit: int,
) -> list[dict[str, str]]:
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        key = (
            row.get("capture_city") or "Unknown",
            row.get("capture_daypart") or "Unknown",
        )
        groups[key].append(row)

    queues: list[tuple[tuple[str, str], deque[dict[str, str]]]] = []
    for key, values in sorted(groups.items()):
        values.sort(key=lambda r: deterministic_key(r["stock_clip_id"]))
        queues.append((key, deque(values)))

    selected: list[dict[str, str]] = []
    while len(selected) < limit:
        progressed = False
        for _key, queue in queues:
            if len(selected) >= limit:
                break
            if queue:
                selected.append(queue.popleft())
                progressed = True
        if not progressed:
            break
    return selected


def calibration_sample(
    rows: list[dict[str, str]],
    limit: int,
) -> list[dict[str, str]]:
    recovery = [row for row in rows if is_recovery(row)]
    baseline = [row for row in rows if not is_recovery(row)]

    if limit >= len(rows):
        return sorted(rows, key=lambda row: row["stock_clip_id"])

    recovery_target = max(1, round(limit * len(recovery) / len(rows)))
    baseline_target = limit - recovery_target

    chosen = round_robin_sample(baseline, baseline_target)
    chosen.extend(round_robin_sample(recovery, recovery_target))

    if len(chosen) < limit:
        chosen_ids = {row["stock_clip_id"] for row in chosen}
        residue = [
            row
            for row in rows
            if row["stock_clip_id"] not in chosen_ids
        ]
        residue.sort(key=lambda row: deterministic_key(row["stock_clip_id"]))
        chosen.extend(residue[: limit - len(chosen)])

    return sorted(chosen, key=lambda row: deterministic_key(row["stock_clip_id"]))


def select_rows(
    rows: list[dict[str, str]],
    *,
    selection: str,
    limit: int | None,
) -> list[dict[str, str]]:
    if selection == "all":
        selected = sorted(rows, key=lambda row: row["stock_clip_id"])
        if limit is not None:
            selected = selected[:limit]
        return selected

    requested = limit if limit is not None else 24
    return calibration_sample(rows, min(requested, len(rows)))


def context_for(row: dict[str, str]) -> dict[str, Any]:
    return {
        "stock_clip_id": row["stock_clip_id"],
        "provenance_location": {
            "country": row.get("country") or None,
            "region": row.get("region") or None,
            "city": row.get("capture_city") or None,
            "area": row.get("canonical_area") or None,
        },
        "capture": {
            "date": row.get("capture_date") or None,
            "daypart": row.get("capture_daypart") or None,
        },
        "note": (
            "Location and capture fields are provenance context only. "
            "Do not infer that a named place or landmark is visibly present."
        ),
    }


def build_search_document(
    row: dict[str, str],
    caption: str,
    tags: list[dict[str, Any]],
    named_subjects: list[dict[str, Any]],
) -> str:
    parts: list[str] = [
        row["stock_clip_id"],
        caption,
        row.get("country") or "",
        row.get("region") or "",
        row.get("capture_city") or "",
        row.get("canonical_area") or "",
        row.get("capture_daypart") or "",
        row.get("capture_date") or "",
    ]
    for tag in tags:
        parts.extend(
            [
                str(tag.get("group") or ""),
                str(tag.get("tag") or ""),
                str(tag.get("strength") or ""),
            ]
        )
    for subject in named_subjects:
        parts.append(str(subject.get("canonical_label") or subject.get("name") or ""))
    return "\n".join(part for part in parts if part)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_html(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    title: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cards: list[str] = []
    for row in rows:
        frame_imgs = "".join(
            f'<img src="file://{html.escape(str(frame))}" '
            'style="width:160px;max-height:120px;object-fit:contain;margin:2px">'
            for frame in row.get("frame_paths", [])
        )
        tags = ", ".join(
            f"{tag['group']}:{tag['tag']}[{tag['strength']}]"
            for tag in row.get("tags", [])
        )
        subjects = ", ".join(
            subject.get("canonical_label") or subject.get("name") or ""
            for subject in row.get("named_subjects", [])
        )
        cards.append(
            "<section style='margin:24px 0;padding:16px;border:1px solid #aaa'>"
            f"<h3>{html.escape(row['stock_clip_id'])}</h3>"
            f"<p><b>{html.escape(row.get('location',''))}</b> · "
            f"{html.escape(row.get('daypart',''))}</p>"
            f"<div>{frame_imgs}</div>"
            f"<p><b>Caption:</b> {html.escape(row.get('caption',''))}</p>"
            f"<p><b>Tags:</b> {html.escape(tags)}</p>"
            f"<p><b>Named:</b> {html.escape(subjects)}</p>"
            "</section>"
        )
    path.write_text(
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title></head>"
        "<body style='font-family:-apple-system,BlinkMacSystemFont,sans-serif;"
        "max-width:1200px;margin:30px auto'>"
        f"<h1>{html.escape(title)}</h1>"
        + "".join(cards)
        + "</body></html>",
        encoding="utf-8",
    )


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


def existing_analysis(
    con: sqlite3.Connection,
    analysis_key: str,
) -> sqlite3.Row | None:
    return con.execute(
        """
        SELECT *
        FROM canonical_clip_visual_analysis
        WHERE analysis_key=?
        """,
        (analysis_key,),
    ).fetchone()


def latest_analysis_for_clip(
    con: sqlite3.Connection,
    stock_clip_id: str,
) -> sqlite3.Row | None:
    return con.execute(
        """
        SELECT *
        FROM canonical_clip_visual_analysis
        WHERE stock_clip_id=?
        ORDER BY updated_at DESC, rowid DESC
        LIMIT 1
        """,
        (stock_clip_id,),
    ).fetchone()


def persist_analysis(
    con: sqlite3.Connection,
    *,
    analysis_run_id: str,
    analysis_key: str,
    row: dict[str, str],
    master_sha256: str,
    master_relative_path: str,
    analyzer: OpenAIVisualAnalyzer,
    taxonomy: VisualTaxonomy,
    samples: Any,
    result: Any,
) -> None:
    created = utc_now()
    analysis = result.analysis
    usage = result.usage
    tags = [asdict(tag) for tag in analysis.tags]
    subjects = [asdict(subject) for subject in analysis.named_subjects]

    evidence = {
        "frame_cache_key": samples.cache_key,
        "frame_positions": list(samples.positions),
        "frame_paths": [str(path) for path in samples.frames],
        "duration_seconds": samples.duration_seconds,
        "canonical_context": context_for(row),
    }

    usage_values = (
        {
            "input_tokens": usage.input_tokens,
            "cached_input_tokens": usage.cached_input_tokens,
            "output_tokens": usage.output_tokens,
            "reasoning_tokens": usage.reasoning_tokens,
            "total_tokens": usage.total_tokens,
            "estimated_input_cost_usd": usage.estimated_input_cost_usd,
            "estimated_output_cost_usd": usage.estimated_output_cost_usd,
            "estimated_total_cost_usd": usage.estimated_total_cost_usd,
        }
        if usage is not None
        else {
            "input_tokens": None,
            "cached_input_tokens": None,
            "output_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": None,
            "estimated_input_cost_usd": None,
            "estimated_output_cost_usd": None,
            "estimated_total_cost_usd": None,
        }
    )

    analysis_id = stable_id(
        "CANONICALVISUAL",
        analysis_key,
    )

    con.execute(
        """
        INSERT INTO canonical_clip_visual_analysis(
            id,
            analysis_key,
            analysis_run_id,
            stock_clip_id,
            canonical_master_sha256,
            canonical_master_relative_path,
            provider,
            model,
            taxonomy_version,
            prompt_version,
            sampler_version,
            caption,
            result_json,
            evidence_json,
            status,
            input_tokens,
            cached_input_tokens,
            output_tokens,
            reasoning_tokens,
            total_tokens,
            estimated_input_cost_usd,
            estimated_output_cost_usd,
            estimated_total_cost_usd,
            created_at,
            updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(analysis_key) DO UPDATE SET
            analysis_run_id=excluded.analysis_run_id,
            caption=excluded.caption,
            result_json=excluded.result_json,
            evidence_json=excluded.evidence_json,
            status='complete',
            input_tokens=excluded.input_tokens,
            cached_input_tokens=excluded.cached_input_tokens,
            output_tokens=excluded.output_tokens,
            reasoning_tokens=excluded.reasoning_tokens,
            total_tokens=excluded.total_tokens,
            estimated_input_cost_usd=excluded.estimated_input_cost_usd,
            estimated_output_cost_usd=excluded.estimated_output_cost_usd,
            estimated_total_cost_usd=excluded.estimated_total_cost_usd,
            updated_at=excluded.updated_at
        """,
        (
            analysis_id,
            analysis_key,
            analysis_run_id,
            row["stock_clip_id"],
            master_sha256,
            master_relative_path,
            analyzer.provider_name,
            analyzer.model,
            taxonomy.version,
            PROMPT_VERSION,
            SAMPLER_VERSION,
            analysis.caption,
            json.dumps(
                {
                    "caption": analysis.caption,
                    "tags": tags,
                    "named_subjects": subjects,
                    "raw": analysis.raw,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            "complete",
            usage_values["input_tokens"],
            usage_values["cached_input_tokens"],
            usage_values["output_tokens"],
            usage_values["reasoning_tokens"],
            usage_values["total_tokens"],
            usage_values["estimated_input_cost_usd"],
            usage_values["estimated_output_cost_usd"],
            usage_values["estimated_total_cost_usd"],
            created,
            created,
        ),
    )

    con.execute(
        "DELETE FROM canonical_clip_visual_tags WHERE analysis_key=?",
        (analysis_key,),
    )
    con.executemany(
        """
        INSERT INTO canonical_clip_visual_tags(
            analysis_key,
            stock_clip_id,
            tag_group,
            tag,
            strength,
            score,
            frame_hits_json,
            rationale
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        [
            (
                analysis_key,
                row["stock_clip_id"],
                tag["group"],
                tag["tag"],
                tag["strength"],
                tag.get("score"),
                json.dumps(tag.get("frame_hits") or []),
                tag.get("rationale"),
            )
            for tag in tags
        ],
    )

    con.execute(
        "DELETE FROM canonical_clip_named_subjects WHERE analysis_key=?",
        (analysis_key,),
    )
    con.executemany(
        """
        INSERT INTO canonical_clip_named_subjects(
            analysis_key,
            stock_clip_id,
            subject,
            confidence,
            verified,
            canonical_entity_id,
            canonical_label,
            resolution_source
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        [
            (
                analysis_key,
                row["stock_clip_id"],
                subject["name"],
                subject["confidence"],
                1 if subject.get("verified") else 0,
                subject.get("canonical_entity_id"),
                subject.get("canonical_label"),
                subject.get("resolution_source"),
            )
            for subject in subjects
        ],
    )

    document = build_search_document(
        row,
        analysis.caption,
        tags,
        subjects,
    )
    con.execute(
        """
        INSERT INTO canonical_clip_search_documents(
            stock_clip_id,
            canonical_master_sha256,
            document_text,
            updated_at
        ) VALUES(?,?,?,?)
        ON CONFLICT(stock_clip_id) DO UPDATE SET
            canonical_master_sha256=excluded.canonical_master_sha256,
            document_text=excluded.document_text,
            updated_at=excluded.updated_at
        """,
        (
            row["stock_clip_id"],
            master_sha256,
            document,
            created,
        ),
    )


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--catalog", type=Path, required=True)
    p.add_argument("--canonical-root", type=Path, required=True)
    p.add_argument("--frame-cache", type=Path, required=True)
    p.add_argument("--report-root", type=Path, required=True)
    p.add_argument(
        "--selection",
        choices=("calibration", "all"),
        default="calibration",
    )
    p.add_argument("--limit", type=int)
    p.add_argument("--model", default="gpt-5-mini")
    p.add_argument("--force", action="store_true")
    p.add_argument("--write", action="store_true")
    return p


def main() -> int:
    args = parser().parse_args()

    db = args.db.expanduser().resolve()
    catalog_path = args.catalog.expanduser().resolve()
    canonical_root = args.canonical_root.expanduser().resolve()
    frame_cache = args.frame_cache.expanduser().resolve()
    report_root = args.report_root.expanduser().resolve()

    rows = read_catalog(catalog_path)
    if len(rows) != 390:
        raise SystemExit(
            f"Expected 390 canonical catalog rows, got {len(rows)}"
        )

    selected = select_rows(
        rows,
        selection=args.selection,
        limit=args.limit,
    )

    if not selected:
        raise SystemExit("No canonical clips selected.")

    masters: dict[str, Path] = {}
    missing: list[str] = []
    for row in selected:
        path = canonical_root / row["canonical_master_relative_path"]
        if not path.is_file():
            missing.append(f"{row['stock_clip_id']}: {path}")
        else:
            masters[row["stock_clip_id"]] = path

    if missing:
        print("MISSING CANONICAL MASTERS")
        for item in missing[:30]:
            print(item)
        return 2

    baseline_count = sum(not is_recovery(row) for row in selected)
    recovery_count = sum(is_recovery(row) for row in selected)
    cities = Counter(row.get("capture_city") or "Unknown" for row in selected)
    dayparts = Counter(row.get("capture_daypart") or "Unknown" for row in selected)

    taxonomy = VisualTaxonomy.default()

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    ensure_schema(con)
    con.commit()

    print("CANONICAL VISUAL ENRICHMENT PREFLIGHT")
    print("=====================================")
    print("canonical catalog :", len(rows))
    print("selected          :", len(selected))
    print("baseline selected :", baseline_count)
    print("recovery selected :", recovery_count)
    print("selection         :", args.selection)
    print("model             :", args.model)
    print("taxonomy version  :", taxonomy.version)
    print("prompt version    :", PROMPT_VERSION)
    print("sampler version   :", SAMPLER_VERSION)
    print("mode              :", "WRITE" if args.write else "DRY RUN")
    print()
    print("DAYPARTS")
    for label, count in dayparts.most_common():
        print(f"{count:4d}  {label}")
    print()
    print("TOP CITIES")
    for label, count in cities.most_common(20):
        print(f"{count:4d}  {label}")

    selection_csv = report_root / (
        f"canonical-{args.selection}-{len(selected)}-selection.csv"
    )
    write_csv(
        selection_csv,
        [
            {
                "stock_clip_id": row["stock_clip_id"],
                "canonical_master_relative_path": row["canonical_master_relative_path"],
                "capture_city": row.get("capture_city") or "",
                "canonical_area": row.get("canonical_area") or "",
                "capture_daypart": row.get("capture_daypart") or "",
                "capture_date": row.get("capture_date") or "",
                "recovery": "YES" if is_recovery(row) else "NO",
            }
            for row in selected
        ],
    )

    if not args.write:
        con.close()
        print()
        print("selection:", selection_csv)
        print("CANONICAL VISUAL ENRICHMENT PREFLIGHT: PASS")
        return 0

    analyzer = OpenAIVisualAnalyzer(
        taxonomy=taxonomy,
        model=args.model,
    )
    sampler = FrameSampler(frame_cache)

    run_id = stable_id(
        "CANONICALVISUALRUN",
        str(catalog_path),
        args.selection,
        str(len(selected)),
        analyzer.provider_name,
        analyzer.model,
        str(taxonomy.version),
        PROMPT_VERSION,
        SAMPLER_VERSION,
        utc_now(),
    )

    con.execute(
        """
        INSERT INTO canonical_visual_analysis_runs(
            id,
            canonical_catalog_path,
            canonical_catalog_version,
            provider,
            model,
            taxonomy_version,
            prompt_version,
            sampler_version,
            selection_mode,
            requested_limit,
            config_json,
            status,
            started_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            str(catalog_path),
            "canonical-master-plan-all-390-v2",
            analyzer.provider_name,
            analyzer.model,
            taxonomy.version,
            PROMPT_VERSION,
            SAMPLER_VERSION,
            args.selection,
            args.limit,
            json.dumps(
                {
                    "force": args.force,
                    "frame_cache": str(frame_cache),
                    "selected_count": len(selected),
                },
                sort_keys=True,
            ),
            "running",
            utc_now(),
        ),
    )
    con.commit()

    report_rows: list[dict[str, Any]] = []
    analyzed = 0
    cached = 0
    failed = 0
    estimated_cost = 0.0

    try:
        for index, row in enumerate(selected, 1):
            sid = row["stock_clip_id"]
            master = masters[sid]
            print(f"{index}/{len(selected)}  {sid}  {master.name}")

            try:
                checksum = sha256_file(master)
                analysis_key = stable_id(
                    "CANONICALANALYSISKEY",
                    checksum,
                    analyzer.provider_name,
                    analyzer.model,
                    taxonomy.version,
                    PROMPT_VERSION,
                    SAMPLER_VERSION,
                )

                prior = existing_analysis(con, analysis_key)
                if prior is not None and not args.force:
                    cached += 1
                    result_payload = json.loads(prior["result_json"])
                    evidence = json.loads(prior["evidence_json"])
                    report_rows.append(
                        {
                            "stock_clip_id": sid,
                            "status": "cached",
                            "location": (
                                f"{row.get('capture_city','')} / "
                                f"{row.get('canonical_area','')}"
                            ),
                            "daypart": row.get("capture_daypart") or "",
                            "caption": prior["caption"],
                            "tags": result_payload.get("tags", []),
                            "named_subjects": result_payload.get("named_subjects", []),
                            "frame_paths": evidence.get("frame_paths", []),
                            "estimated_cost_usd": prior["estimated_total_cost_usd"],
                            "analysis_key": analysis_key,
                        }
                    )
                    print("  cached")
                    continue

                samples = sampler.sample(
                    master,
                    export_sha256=checksum,
                    overwrite=False,
                )
                result = analyzer.analyze(
                    samples.frames,
                    context=context_for(row),
                )

                persist_analysis(
                    con,
                    analysis_run_id=run_id,
                    analysis_key=analysis_key,
                    row=row,
                    master_sha256=checksum,
                    master_relative_path=row["canonical_master_relative_path"],
                    analyzer=analyzer,
                    taxonomy=taxonomy,
                    samples=samples,
                    result=result,
                )
                con.commit()

                usage = result.usage
                cost = (
                    usage.estimated_total_cost_usd
                    if usage is not None
                    and usage.estimated_total_cost_usd is not None
                    else 0.0
                )
                estimated_cost += float(cost)
                analyzed += 1

                analysis = result.analysis
                report_rows.append(
                    {
                        "stock_clip_id": sid,
                        "status": "analyzed",
                        "location": (
                            f"{row.get('capture_city','')} / "
                            f"{row.get('canonical_area','')}"
                        ),
                        "daypart": row.get("capture_daypart") or "",
                        "caption": analysis.caption,
                        "tags": [asdict(tag) for tag in analysis.tags],
                        "named_subjects": [
                            asdict(subject)
                            for subject in analysis.named_subjects
                        ],
                        "frame_paths": [str(path) for path in samples.frames],
                        "estimated_cost_usd": (
                            usage.estimated_total_cost_usd
                            if usage is not None
                            else None
                        ),
                        "analysis_key": analysis_key,
                    }
                )
                print(
                    f"  analyzed tags={len(analysis.tags)} "
                    f"cost=${cost:.5f}"
                )

            except Exception as exc:
                failed += 1
                report_rows.append(
                    {
                        "stock_clip_id": sid,
                        "status": "failed",
                        "location": (
                            f"{row.get('capture_city','')} / "
                            f"{row.get('canonical_area','')}"
                        ),
                        "daypart": row.get("capture_daypart") or "",
                        "caption": "",
                        "tags": [],
                        "named_subjects": [],
                        "frame_paths": [],
                        "estimated_cost_usd": None,
                        "analysis_key": "",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                print(f"  FAILED: {type(exc).__name__}: {exc}")

        status = "complete" if failed == 0 else "failed"
        con.execute(
            """
            UPDATE canonical_visual_analysis_runs
            SET status=?,
                completed_at=?,
                error_text=?
            WHERE id=?
            """,
            (
                status,
                utc_now(),
                None if failed == 0 else f"{failed} clip(s) failed",
                run_id,
            ),
        )
        con.commit()

    except BaseException as exc:
        con.execute(
            """
            UPDATE canonical_visual_analysis_runs
            SET status='failed',
                completed_at=?,
                error_text=?
            WHERE id=?
            """,
            (utc_now(), f"{type(exc).__name__}: {exc}", run_id),
        )
        con.commit()
        raise
    finally:
        con.close()

    flat_rows: list[dict[str, Any]] = []
    for row in report_rows:
        tags = row.get("tags") or []
        subjects = row.get("named_subjects") or []
        flat_rows.append(
            {
                "stock_clip_id": row["stock_clip_id"],
                "status": row["status"],
                "location": row.get("location", ""),
                "daypart": row.get("daypart", ""),
                "caption": row.get("caption", ""),
                "tags": " | ".join(
                    f"{tag.get('group')}:{tag.get('tag')}:{tag.get('strength')}"
                    for tag in tags
                ),
                "named_subjects": " | ".join(
                    str(subject.get("canonical_label") or subject.get("name") or "")
                    for subject in subjects
                ),
                "estimated_cost_usd": row.get("estimated_cost_usd"),
                "analysis_key": row.get("analysis_key", ""),
                "error": row.get("error", ""),
            }
        )

    csv_path = report_root / (
        f"canonical-{args.selection}-{len(selected)}-visual-results.csv"
    )
    html_path = report_root / (
        f"canonical-{args.selection}-{len(selected)}-visual-report.html"
    )
    json_path = report_root / (
        f"canonical-{args.selection}-{len(selected)}-visual-report.json"
    )

    write_csv(csv_path, flat_rows)
    write_html(
        html_path,
        report_rows,
        title=(
            f"VClip Canonical Visual Enrichment — "
            f"{args.selection} {len(selected)}"
        ),
    )
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "selection": args.selection,
                "selected": len(selected),
                "analyzed": analyzed,
                "cached": cached,
                "failed": failed,
                "estimated_cost_usd": estimated_cost,
                "model": analyzer.model,
                "taxonomy_version": taxonomy.version,
                "prompt_version": PROMPT_VERSION,
                "sampler_version": SAMPLER_VERSION,
                "clips": report_rows,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    print()
    print("CANONICAL VISUAL ENRICHMENT")
    print("===========================")
    print("selected          :", len(selected))
    print("analyzed          :", analyzed)
    print("cached            :", cached)
    print("failed            :", failed)
    print(f"estimated cost    : ${estimated_cost:.4f}")
    print("csv               :", csv_path)
    print("html              :", html_path)
    print("json              :", json_path)

    if failed:
        print()
        print("CANONICAL VISUAL ENRICHMENT: FAILED")
        return 2

    print()
    print("CANONICAL VISUAL ENRICHMENT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
