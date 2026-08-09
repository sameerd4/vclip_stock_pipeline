from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from vclip_pipeline.reconcile import ReconcileService
from vclip_pipeline.stockify.fcpxml import validate_fcpxml
from vclip_pipeline.workflow.review_shard import ReviewShardService


def _markets_path() -> Path:
    return (
        Path(__file__).parents[1]
        / "src"
        / "vclip_pipeline"
        / "data"
        / "workflow_markets.json"
    )


def test_review_shard_is_deterministic_and_preserves_clip_ids(pipeline_run):
    output = pipeline_run["tmp_path"] / "shards"
    report = ReviewShardService(pipeline_run["repository"]).run(
        review_xml=pipeline_run["output"],
        output_directory=output,
        markets_path=_markets_path(),
        group_by="none",
        representation="individual",
        max_projects=3,
        max_megabytes=None,
        include_scope_markers=True,
        include_compilations=False,
        overwrite=False,
        dry_run=False,
        report_path=output / "report.json",
    )
    assert report.shards_written >= 2
    index = json.loads((output / "review--shards.json").read_text())
    all_ids: list[str] = []
    for shard in index["shards"]:
        path = Path(shard["path"])
        root = ET.parse(path).getroot()
        validation = validate_fcpxml(root)
        assert validation.passed, validation.errors
        all_ids.extend(shard["stock_clip_ids"])
        project_names = [
            project.get("name", "")
            for project in root.findall("./library/event/project")
        ]
        assert any("Stock Compilation" in name for name in project_names)
        for project in root.findall("./library/event/project"):
            if "Stock Compilation" in project.get("name", ""):
                assert project.find("./sequence/spine/gap") is not None
    accepted = pipeline_run["repository"].candidates_for_run(
        pipeline_run["result"].stockify_run_id,
        accepted_only=True,
    )
    assert sorted(all_ids) == sorted(row["stock_clip_id"] for row in accepted)
    assert len(all_ids) == len(set(all_ids))


def test_scope_marker_preserves_partial_reconcile_scope(pipeline_run):
    output = pipeline_run["tmp_path"] / "scope-shard"
    ReviewShardService(pipeline_run["repository"]).run(
        review_xml=pipeline_run["output"],
        output_directory=output,
        markets_path=_markets_path(),
        group_by="none",
        representation="individual",
        max_projects=100,
        max_megabytes=None,
        include_scope_markers=True,
        include_compilations=False,
        overwrite=False,
        dry_run=False,
        report_path=None,
    )
    shard_path = next(output.glob("*.fcpxml"))
    manifest = json.loads(
        shard_path.with_name(f"{shard_path.stem}-shard-manifest.json").read_text()
    )
    target_source_project = manifest["projects"][0]["source_project_id"]
    target_ids = {
        clip_id
        for project in manifest["projects"]
        if project["source_project_id"] == target_source_project
        for clip_id in project["stock_clip_ids"]
    }

    tree = ET.parse(shard_path)
    root = tree.getroot()
    for event in root.findall("./library/event"):
        for project in list(event.findall("project")):
            if "Stock Compilation" in project.get("name", ""):
                continue
            metadata = project.find("./sequence/spine/*/metadata")
            source_project_id = None
            if metadata is not None:
                for md in metadata.findall("md"):
                    if md.get("key") == "com.vclip.source_project_id":
                        source_project_id = md.get("value")
                        break
            if source_project_id == target_source_project:
                event.remove(project)
    reviewed = output / "reviewed.fcpxml"
    tree.write(reviewed, encoding="utf-8", xml_declaration=True)

    report = ReconcileService(pipeline_run["repository"]).run(
        reviewed_xml=reviewed,
        run_id=pipeline_run["result"].stockify_run_id,
        authority="auto",
        scope="observed-projects",
        report_path=None,
        allow_conflicts=False,
    )
    rejected = {
        row["stock_clip_id"]
        for row in report.decisions
        if row["review_status"] == "rejected"
    }
    assert target_ids <= rejected
