from __future__ import annotations

import json
from pathlib import Path

from test_package_release import _seed_collection

from vclip_pipeline.publishing import (
    ContentReadinessService,
    PackageReleaseService,
)
from vclip_pipeline.publishing.paths import load_json, release_directory
from vclip_pipeline.util import json_dumps
from vclip_pipeline.workflow.models import VisualAnalysis, VisualTag


def build_content_release(pipeline_run, *, clip_count: int = 1):
    seeded = _seed_collection(pipeline_run, clip_count=clip_count)
    run_id = pipeline_run["result"].stockify_run_id
    analysis_run = seeded["catalog"].start_visual_run(
        provider="test",
        model="test",
        taxonomy_version=1,
        prompt_version="test",
        sampler_version="test",
        config={},
    )
    for index, item in enumerate(seeded["prepared"]):
        clip_id = item["candidate"]["stock_clip_id"]
        seeded["catalog"].upsert_visual_analysis(
            analysis_key=f"ANALYSIS_CONTENT_{clip_id}",
            analysis_run_id=analysis_run,
            stockify_run_id=run_id,
            stock_clip_id=clip_id,
            export_id=item["export"]["id"],
            export_sha256=item["digest"],
            provider="test",
            model="test",
            taxonomy_version=1,
            analysis=VisualAnalysis(
                caption=f"Aerial view of waterfront clip {index + 1}.",
                tags=(VisualTag("subject", "skyline", "primary", 0.9),),
            ),
            evidence={},
        )
        seeded["catalog"].upsert_tag(
            run_id=run_id,
            clip_id=clip_id,
            group="subject",
            tag="skyline",
            source="test",
        )
        seeded["catalog"].upsert_market(
            run_id=run_id,
            clip_id=clip_id,
            market_id="san-francisco",
            market_label="San Francisco",
        )

    root = pipeline_run["tmp_path"] / "release-root"
    PackageReleaseService(seeded["catalog"]).build(
        slug=seeded["slug"],
        version=seeded["version"],
        output_directory=root,
    )
    return {
        **seeded,
        "release_root": root,
        "release_dir": release_directory(root, seeded["slug"], seeded["version"]),
    }


def confirm_clean_review(built) -> None:
    service = ContentReadinessService(built["catalog"])
    service.review.confirm(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
        clip=1,
        reviewed_by="reviewer",
        recognizable_people="none",
        trademarks="none",
        copyrighted_artwork="none",
        identifiable_property="none",
        identifying_information="none",
        professional_event_content="none",
        capture_provenance="confirmed_by_operator",
    )


def test_public_metadata_stays_customer_safe_and_requires_content(pipeline_run) -> None:
    built = build_content_release(pipeline_run)
    result = ContentReadinessService(built["catalog"]).prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    public = load_json(Path(result["public_metadata_path"]))
    clip = public["clips"][0]
    assert clip["caption"]
    assert clip["tags"] == ["skyline"]
    assert clip["markets"] == ["San Francisco"]
    blob = json.dumps(public)
    for forbidden in (
        "master_path",
        "export_id",
        "stock_clip_id",
        "latitude",
        "longitude",
        "reviewed_by",
    ):
        assert forbidden not in blob


def test_content_ready_uses_review_validation(pipeline_run) -> None:
    built = build_content_release(pipeline_run)
    service = ContentReadinessService(built["catalog"])
    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    pending = service.validate(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    assert pending["status"] == "not_content_ready"
    assert pending["review_validation_ready"] is False

    confirm_clean_review(built)
    ready = service.validate(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    assert ready["status"] == "content_ready"
    assert ready["review_validation_ready"] is True
    package = load_json(built["release_dir"] / "package-release.json")
    assert package["status"] == "release_core_ready"


def test_public_metadata_exposes_only_confirmed_rights_notice(pipeline_run) -> None:
    built = build_content_release(pipeline_run)
    service = ContentReadinessService(built["catalog"])
    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    public_path = built["release_dir"] / "public-metadata.json"
    assert "rights" not in load_json(public_path)["clips"][0]

    service.review.confirm(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
        clip=1,
        reviewed_by="reviewer",
        recognizable_people="none",
        trademarks="incidental",
        copyrighted_artwork="none",
        identifiable_property="none",
        identifying_information="none",
        professional_event_content="none",
        capture_provenance="confirmed_by_operator",
    )
    service.review.validate(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    public = load_json(public_path)
    rights = public["clips"][0]["rights"]
    assert rights["classification"] == "standard_with_notice"
    assert rights["notices"]
    blob = json_dumps(public)
    assert "reviewer" not in blob
    assert "master_path" not in blob
