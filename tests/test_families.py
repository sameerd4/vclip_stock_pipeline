from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

from conftest import ProjectSpec, run_stockify
from vclip_pipeline.db import CatalogRepository, Database
from vclip_pipeline.stockify import StockifyService


BORDERLINE_BASE = dict(
    stamp="20260509190000",
    latitude=47.6253,
    longitude=-122.3377,
    clip_count=2,
    asset_key="borderline",
    graded=True,
)


def _output_project_names(output: Path) -> list[str]:
    root = ET.parse(output).getroot()
    library = root.find("library")
    assert library is not None
    names: list[str] = []
    for event in library.findall("event"):
        for project in event.findall("project"):
            name = project.get("name")
            if name:
                names.append(name)
    return names


def test_poly_insufficient_grading_emits_nothing_while_poly1_survives(tmp_path: Path):
    specs = [
        ProjectSpec(
            name="Poly",
            stamp="20260509190000",
            latitude=47.6253,
            longitude=-122.3377,
            asset_key="poly",
            clip_count=40,
            graded_clip_numbers=(1,),  # 2.5% graded
            mod_date="2024-01-01 10:00:00 +0000",
        ),
        ProjectSpec(
            name="Poly 1",
            stamp="20260509190000",
            latitude=47.6253,
            longitude=-122.3377,
            asset_key="poly",
            clip_count=40,
            graded=True,
            mod_date="2024-02-01 10:00:00 +0000",
        ),
    ]
    pipeline = run_stockify(tmp_path, specs)
    run_id = pipeline["result"].stockify_run_id
    projects = {
        project["source_name"]: project
        for project in pipeline["repository"].projects_for_run(run_id)
    }
    assert projects["Poly"]["family_role"] == "superseded"
    assert projects["Poly"]["family_selection_reason"] == "ungraded_variant"
    assert abs(projects["Poly"]["grading_coverage"] - 0.025) < 1e-9
    assert projects["Poly 1"]["family_role"] == "selected"
    assert projects["Poly 1"]["grading_coverage"] == 1.0

    accepted = [
        item
        for item in pipeline["repository"].candidates_for_run(run_id)
        if item["eligibility_status"] == "accepted"
    ]
    assert len(accepted) == 40
    assert all(item["source_project_id"] == projects["Poly 1"]["id"] for item in accepted)
    names = _output_project_names(pipeline["output"])
    assert any("Stock Compilation" in name for name in names)
    assert len([name for name in names if "Clip " in name]) == 40
    from vclip_pipeline.stockify.fcpxml import read_vclip_metadata

    root = ET.parse(pipeline["output"]).getroot()
    first_clip = (
        root.find("library")
        .findall("event")[0]
        .findall("project")[1]
        .find("./sequence/spine/asset-clip")
    )
    metadata = read_vclip_metadata(first_clip)
    assert metadata["com.vclip.source_project"] == "Poly 1"


def test_four_graded_duplicates_produce_one_winner(tmp_path: Path):
    specs = [
        ProjectSpec(name="Borderline", mod_date="2024-01-01 10:00:00 +0000", **BORDERLINE_BASE),
        ProjectSpec(name="Borderline 1", mod_date="2024-02-01 10:00:00 +0000", **BORDERLINE_BASE),
        ProjectSpec(name="Borderline 2", mod_date="2024-03-01 10:00:00 +0000", **BORDERLINE_BASE),
        ProjectSpec(
            name="Borderline Single",
            mod_date="2024-04-01 10:00:00 +0000",
            **BORDERLINE_BASE,
        ),
    ]
    pipeline = run_stockify(tmp_path, specs)
    run_id = pipeline["result"].stockify_run_id
    families = pipeline["repository"].project_families_for_run(run_id)
    assert len(families) == 1
    assert families[0]["member_count"] == 4
    projects = pipeline["repository"].projects_for_run(run_id)
    selected = [project for project in projects if project["family_role"] == "selected"]
    superseded = [project for project in projects if project["family_role"] == "superseded"]
    assert len(selected) == 1
    assert selected[0]["source_name"] == "Borderline Single"
    assert len(superseded) == 3
    assert {project["family_selection_reason"] for project in superseded} == {
        "superseded_graded_variant"
    }
    accepted = [
        item
        for item in pipeline["repository"].candidates_for_run(run_id)
        if item["eligibility_status"] == "accepted"
    ]
    assert len(accepted) == 2
    names = _output_project_names(pipeline["output"])
    assert len([name for name in names if "Stock Compilation" in name]) == 1
    assert len([name for name in names if "Clip " in name]) == 2


def test_near_duplicate_timelines_with_trims_still_form_one_family(tmp_path: Path):
    specs = [
        ProjectSpec(
            name="Borderline",
            graded=True,
            asset_key="borderline-trim",
            stamp="20260509190000",
            latitude=47.6253,
            longitude=-122.3377,
            clip_starts=(3, 12),
            clip_durations=(8, 8),
            mod_date="2024-01-01 10:00:00 +0000",
        ),
        ProjectSpec(
            name="Borderline 1",
            graded=True,
            asset_key="borderline-trim",
            stamp="20260509190000",
            latitude=47.6253,
            longitude=-122.3377,
            clip_starts=(4, 13),  # one-second trims; IoU stays high
            clip_durations=(8, 8),
            mod_date="2024-05-01 10:00:00 +0000",
        ),
    ]
    pipeline = run_stockify(tmp_path, specs)
    families = pipeline["repository"].project_families_for_run(
        pipeline["result"].stockify_run_id
    )
    assert len(families) == 1
    selected = [
        project
        for project in pipeline["repository"].projects_for_run(
            pipeline["result"].stockify_run_id
        )
        if project["family_role"] == "selected"
    ]
    assert len(selected) == 1
    assert selected[0]["source_name"] == "Borderline 1"


def test_duplicate_source_ranges_inside_winner_emit_once(tmp_path: Path):
    specs = [
        ProjectSpec(
            name="Borderline",
            graded=True,
            asset_key="borderline-dedupe",
            stamp="20260509190000",
            latitude=47.6253,
            longitude=-122.3377,
            clip_starts=(3, 3),
            clip_durations=(8, 10),  # second is longer overlapping duplicate
            mod_date="2024-01-01 10:00:00 +0000",
        )
    ]
    pipeline = run_stockify(tmp_path, specs)
    run_id = pipeline["result"].stockify_run_id
    accepted = [
        item
        for item in pipeline["repository"].candidates_for_run(run_id)
        if item["eligibility_status"] == "accepted"
    ]
    demoted = [
        item
        for item in pipeline["repository"].candidates_for_run(run_id)
        if item["rejection_reason"] == "duplicate_source_range"
    ]
    assert len(accepted) == 1
    assert len(demoted) == 1
    assert accepted[0]["original_duration_seconds"] == 10.0
    names = _output_project_names(pipeline["output"])
    assert len([name for name in names if "Clip " in name]) == 1


def test_genuinely_different_shots_remain_separate(tmp_path: Path):
    specs = [
        ProjectSpec(
            name="Borderline",
            graded=True,
            stamp="20260509190000",
            latitude=47.6253,
            longitude=-122.3377,
            asset_key="borderline-a",
            clip_starts=(3, 6),
        ),
        ProjectSpec(
            name="Borderline 1",
            graded=True,
            stamp="20260509190000",
            latitude=47.6253,
            longitude=-122.3377,
            asset_key="borderline-a",
            clip_starts=(30, 40),
        ),
    ]
    pipeline = run_stockify(tmp_path, specs)
    run_id = pipeline["result"].stockify_run_id
    assert pipeline["repository"].project_families_for_run(run_id) == []
    projects = pipeline["repository"].projects_for_run(run_id)
    assert {project["family_role"] for project in projects} == {"standalone"}
    accepted = [
        item
        for item in pipeline["repository"].candidates_for_run(run_id)
        if item["eligibility_status"] == "accepted"
    ]
    assert len(accepted) == 4


def test_ungraded_standalone_is_withheld_but_auditable(tmp_path: Path):
    specs = [
        ProjectSpec(
            name="Natural Only",
            graded=False,
            stamp="20260509190000",
            latitude=47.6253,
            longitude=-122.3377,
            clip_count=2,
        )
    ]
    pipeline = run_stockify(tmp_path, specs)
    run_id = pipeline["result"].stockify_run_id
    projects = pipeline["repository"].projects_for_run(run_id)
    assert len(projects) == 1
    assert projects[0]["family_role"] == "withheld"
    assert projects[0]["family_selection_reason"] == "insufficient_grading"
    accepted = [
        item
        for item in pipeline["repository"].candidates_for_run(run_id)
        if item["eligibility_status"] == "accepted"
    ]
    assert accepted == []
    demoted = [
        item
        for item in pipeline["repository"].candidates_for_run(run_id)
        if item["rejection_reason"] == "insufficient_grading"
    ]
    assert len(demoted) == 2
    assert _output_project_names(pipeline["output"]) == []


def test_multiple_graded_duplicates_are_deterministic(tmp_path: Path):
    specs = [
        ProjectSpec(
            name="Borderline",
            mod_date="2024-01-01 10:00:00 +0000",
            **BORDERLINE_BASE,
        ),
        ProjectSpec(
            name="Borderline 1",
            mod_date="2024-06-01 10:00:00 +0000",
            **BORDERLINE_BASE,
        ),
        ProjectSpec(
            name="Borderline 2",
            mod_date="2024-03-01 10:00:00 +0000",
            **BORDERLINE_BASE,
        ),
    ]
    first = run_stockify(tmp_path / "first", specs)
    second_options = replace(
        first["options"],
        output_path=tmp_path / "second" / "review.fcpxml",
        report_path=tmp_path / "second" / "report.json",
        manifest_path=tmp_path / "second" / "manifest.json",
        database_path=tmp_path / "second" / "vclip.sqlite3",
    )
    second_options.output_path.parent.mkdir(parents=True)
    database = Database(second_options.database_path)
    database.migrate()
    second = StockifyService(CatalogRepository(database), first["resolver"]).run(
        second_options
    )

    def selected_name(run_id, repository):
        selected = [
            project
            for project in repository.projects_for_run(run_id)
            if project["family_role"] == "selected"
        ]
        assert len(selected) == 1
        return selected[0]["source_name"]

    assert selected_name(first["result"].stockify_run_id, first["repository"]) == "Borderline 1"
    assert selected_name(second.stockify_run_id, CatalogRepository(database)) == "Borderline 1"
