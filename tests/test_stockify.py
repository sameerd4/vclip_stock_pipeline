from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import replace

from vclip_pipeline.stockify import StockifyService
from vclip_pipeline.stockify.fcpxml import read_vclip_metadata


def test_stockify_reorganizes_events_and_persists_analysis(pipeline_run):
    output = pipeline_run["output"]
    repository = pipeline_run["repository"]
    run_id = pipeline_run["result"].stockify_run_id
    assert run_id

    root = ET.parse(output).getroot()
    events = root.find("library").findall("event")
    assert [event.get("name") for event in events] == [
        "Capitol Hill, Seattle — 2025-12-09",
        "Downtown Seattle — 2026-05-02",
        "South Lake Union, Seattle — 2026-05-09",
    ]

    slu = events[2]
    project_names = [project.get("name") for project in slu.findall("project")]
    assert project_names == [
        "South Lake Union Evening — Graded 1 — Stock Compilation",
        "South Lake Union Evening — Graded 1 — Clip 01",
        "South Lake Union Evening — Graded 1 — Clip 02",
        "South Lake Union Evening — Graded 2 — Stock Compilation",
        "South Lake Union Evening — Graded 2 — Clip 01",
        "South Lake Union Evening — Graded 2 — Clip 02",
    ]

    candidates = repository.candidates_for_run(run_id)
    accepted = [item for item in candidates if item["eligibility_status"] == "accepted"]
    rejected = [item for item in candidates if item["eligibility_status"] == "rejected"]
    assert len(accepted) == 7
    assert len(rejected) == 1
    assert rejected[0]["rejection_reason"] == "unsupported_retime"
    assert len(repository.sessions_for_run(run_id)) == 3

    first_individual = slu.findall("project")[1].find("./sequence/spine/asset-clip")
    metadata = read_vclip_metadata(first_individual)
    assert metadata["com.vclip.stock_clip_id"].startswith("VCLIP_")
    assert metadata["com.vclip.representation"] == "individual"
    assert metadata["com.vclip.session_id"]
    assert metadata["com.vclip.source_project"] == "Hot Gunna Thug"


def test_stock_clip_ids_survive_stockify_reruns(pipeline_run):
    repository = pipeline_run["repository"]
    first_run_id = pipeline_run["result"].stockify_run_id
    first_ids = {
        row["stock_clip_id"]
        for row in repository.candidates_for_run(first_run_id)
    }

    second_options = replace(
        pipeline_run["options"],
        output_path=pipeline_run["tmp_path"] / "review-second.fcpxml",
        report_path=pipeline_run["tmp_path"] / "stockify-second-report.json",
        manifest_path=pipeline_run["tmp_path"] / "manifest-second.json",
    )
    second = StockifyService(repository, pipeline_run["resolver"]).run(second_options)
    second_ids = {
        row["stock_clip_id"]
        for row in repository.candidates_for_run(second.stockify_run_id)
    }
    assert second_ids == first_ids
