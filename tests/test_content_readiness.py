from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_package_release import TITLE, _seed_collection

from vclip_pipeline.errors import VClipError
from vclip_pipeline.publishing import (
    ContentReadinessService,
    PackageReleaseService,
)
from vclip_pipeline.publishing.paths import (
    content_validation_path,
    load_json,
    release_directory,
    rights_review_path,
    write_json,
)
from vclip_pipeline.publishing.rights_review import reconcile_rights_review
from vclip_pipeline.workflow import cli as workflow_cli
from vclip_pipeline.workflow.cli import build_parser, main
from vclip_pipeline.workflow.models import VisualAnalysis, VisualTag


def _enrich_clip(
    catalog,
    *,
    analysis_run_id: str,
    run_id: str,
    clip_id: str,
    export_id: str,
    export_sha256: str,
    caption: str,
    tags: list[tuple[str, str]],
    market_id: str = "san-francisco",
    market_label: str = "San Francisco",
) -> None:
    catalog.upsert_visual_analysis(
        analysis_key=f"ANALYSIS_{clip_id}",
        analysis_run_id=analysis_run_id,
        stockify_run_id=run_id,
        stock_clip_id=clip_id,
        export_id=export_id,
        export_sha256=export_sha256,
        provider="test",
        model="test",
        taxonomy_version=1,
        analysis=VisualAnalysis(
            caption=caption,
            tags=tuple(
                VisualTag(group, tag, "primary", 0.9) for group, tag in tags
            ),
        ),
        evidence={},
    )
    catalog.upsert_market(
        run_id=run_id,
        clip_id=clip_id,
        market_id=market_id,
        market_label=market_label,
    )
    for group, tag in tags:
        catalog.upsert_tag(
            run_id=run_id,
            clip_id=clip_id,
            group=group,
            tag=tag,
            source="test",
            strength="primary",
            score=0.9,
        )


def _build_release(pipeline_run, *, clip_count: int = 2, enrich: bool = True):
    seeded = _seed_collection(pipeline_run, clip_count=clip_count)
    if enrich:
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
            _enrich_clip(
                seeded["catalog"],
                analysis_run_id=analysis_run,
                run_id=run_id,
                clip_id=item["candidate"]["stock_clip_id"],
                export_id=item["export"]["id"],
                export_sha256=item["digest"],
                caption=f"Aerial view of clip {index + 1} over the waterfront.",
                tags=[
                    ("subject", "skyline"),
                    ("scene", "city_urban" if index == 0 else "coastal"),
                ],
            )
    release_root = pipeline_run["tmp_path"] / "release-root"
    PackageReleaseService(seeded["catalog"]).build(
        slug=seeded["slug"],
        version=seeded["version"],
        output_directory=release_root,
        overwrite=True,
    )
    return {
        **seeded,
        "release_root": release_root,
        "release_dir": release_directory(
            release_root, seeded["slug"], seeded["version"]
        ),
    }


def _approve_rights(path: Path) -> dict[str, Any]:
    document = load_json(path)
    for clip in document["clips"]:
        clip["review_status"] = "approved"
        clip["people"] = "none_visible"
        clip["logos_trademarks"] = "reviewed"
        clip["artwork_property"] = "none_visible"
        clip["license_plates"] = "none_visible"
        clip["reviewed_by"] = "reviewer@vclip.test"
        clip["reviewed_at"] = "2026-08-20T12:00:00+00:00"
        clip["notes"] = "ok"
    write_json(path, document)
    return document


def test_public_metadata_is_customer_safe_and_ordered(pipeline_run) -> None:
    built = _build_release(pipeline_run, clip_count=2)
    service = ContentReadinessService(built["catalog"])
    result = service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    public = load_json(Path(result["public_metadata_path"]))
    assert public["title"] == TITLE
    assert public["collection_slug"] == built["slug"]
    assert public["clip_count"] == 2
    assert "formats" in public
    assert [clip["sort_order"] for clip in public["clips"]] == [1, 2]
    assert public["clips"][0]["customer_filename"] == f"{TITLE} — Clip 01.mp4"
    assert public["clips"][0]["caption"].startswith("Aerial view of clip 1")
    assert public["clips"][0]["tags"] == ["city_urban", "skyline"]
    assert public["clips"][0]["markets"] == ["San Francisco"]
    assert public["clips"][1]["tags"] == ["coastal", "skyline"]

    blob = json.dumps(public)
    assert "master_path" not in blob
    assert "exported_path" not in blob
    assert "latitude" not in blob
    assert "longitude" not in blob
    assert "stock_clip_id" not in blob
    assert "export_id" not in blob
    assert "rationale" not in blob


def test_public_metadata_missing_caption_blocked(pipeline_run) -> None:
    built = _build_release(pipeline_run, clip_count=1, enrich=False)
    run_id = pipeline_run["result"].stockify_run_id
    item = built["prepared"][0]
    built["catalog"].upsert_market(
        run_id=run_id,
        clip_id=item["candidate"]["stock_clip_id"],
        market_id="san-francisco",
        market_label="San Francisco",
    )
    built["catalog"].upsert_tag(
        run_id=run_id,
        clip_id=item["candidate"]["stock_clip_id"],
        group="subject",
        tag="skyline",
        source="test",
    )
    with pytest.raises(VClipError, match="missing caption"):
        ContentReadinessService(built["catalog"]).prepare(
            slug=built["slug"],
            version=built["version"],
            release_root=built["release_root"],
        )


def test_public_metadata_missing_tags_blocked(pipeline_run) -> None:
    built = _build_release(pipeline_run, clip_count=1, enrich=False)
    run_id = pipeline_run["result"].stockify_run_id
    item = built["prepared"][0]
    analysis_run = built["catalog"].start_visual_run(
        provider="test",
        model="test",
        taxonomy_version=1,
        prompt_version="test",
        sampler_version="test",
        config={},
    )
    built["catalog"].upsert_visual_analysis(
        analysis_key="ANALYSIS_NOTAGS",
        analysis_run_id=analysis_run,
        stockify_run_id=run_id,
        stock_clip_id=item["candidate"]["stock_clip_id"],
        export_id=item["export"]["id"],
        export_sha256=item["digest"],
        provider="test",
        model="test",
        taxonomy_version=1,
        analysis=VisualAnalysis(caption="A caption with no tags.", tags=()),
        evidence={},
    )
    built["catalog"].upsert_market(
        run_id=run_id,
        clip_id=item["candidate"]["stock_clip_id"],
        market_id="san-francisco",
        market_label="San Francisco",
    )
    with pytest.raises(VClipError, match="missing public tags"):
        ContentReadinessService(built["catalog"]).prepare(
            slug=built["slug"],
            version=built["version"],
            release_root=built["release_root"],
        )


def test_public_metadata_missing_market_blocked(pipeline_run) -> None:
    built = _build_release(pipeline_run, clip_count=1, enrich=False)
    run_id = pipeline_run["result"].stockify_run_id
    item = built["prepared"][0]
    analysis_run = built["catalog"].start_visual_run(
        provider="test",
        model="test",
        taxonomy_version=1,
        prompt_version="test",
        sampler_version="test",
        config={},
    )
    built["catalog"].upsert_visual_analysis(
        analysis_key="ANALYSIS_NOMARKET",
        analysis_run_id=analysis_run,
        stockify_run_id=run_id,
        stock_clip_id=item["candidate"]["stock_clip_id"],
        export_id=item["export"]["id"],
        export_sha256=item["digest"],
        provider="test",
        model="test",
        taxonomy_version=1,
        analysis=VisualAnalysis(
            caption="Caption present.",
            tags=(VisualTag("subject", "skyline", "primary", 0.9),),
        ),
        evidence={},
    )
    built["catalog"].upsert_tag(
        run_id=run_id,
        clip_id=item["candidate"]["stock_clip_id"],
        group="subject",
        tag="skyline",
        source="test",
    )
    with pytest.raises(VClipError, match="missing markets"):
        ContentReadinessService(built["catalog"]).prepare(
            slug=built["slug"],
            version=built["version"],
            release_root=built["release_root"],
        )


def test_rights_review_defaults_and_preserves_decisions(pipeline_run) -> None:
    built = _build_release(pipeline_run, clip_count=2)
    service = ContentReadinessService(built["catalog"])
    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    rights_path = rights_review_path(built["release_dir"])
    document = load_json(rights_path)
    assert len(document["clips"]) == 2
    assert document["clip_count"] == 2
    first = document["clips"][0]
    assert first["review_status"] == "pending"
    assert first["people"] == "unchecked"
    assert first["logos_trademarks"] == "unchecked"
    assert first["artwork_property"] == "unchecked"
    assert first["license_plates"] == "unchecked"
    assert first["reviewed_by"] == ""
    assert first["reviewed_at"] == ""

    first["review_status"] = "approved"
    first["people"] = "none_visible"
    first["logos_trademarks"] = "reviewed"
    first["artwork_property"] = "none_visible"
    first["license_plates"] = "none_visible"
    first["reviewed_by"] = "alice"
    first["reviewed_at"] = "2026-08-20T01:00:00+00:00"
    first["notes"] = "keep me"
    write_json(rights_path, document)

    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    rerun = load_json(rights_path)
    preserved = next(
        item
        for item in rerun["clips"]
        if item["stock_clip_id"] == first["stock_clip_id"]
    )
    assert preserved["review_status"] == "approved"
    assert preserved["reviewed_by"] == "alice"
    assert preserved["notes"] == "keep me"
    assert preserved["people"] == "none_visible"


def test_rights_review_adds_missing_entries(pipeline_run) -> None:
    built = _build_release(pipeline_run, clip_count=2)
    service = ContentReadinessService(built["catalog"])
    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    rights_path = rights_review_path(built["release_dir"])
    document = load_json(rights_path)
    kept = document["clips"][0]
    document["clips"] = [kept]
    write_json(rights_path, document)

    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    restored = load_json(rights_path)
    assert len(restored["clips"]) == 2
    assert restored["clip_count"] == 2
    assert any(
        item["stock_clip_id"] == kept["stock_clip_id"]
        and item["review_status"] == kept["review_status"]
        for item in restored["clips"]
    )


def test_rights_review_clip_count_written_and_updated(pipeline_run) -> None:
    built = _build_release(pipeline_run, clip_count=2)
    service = ContentReadinessService(built["catalog"])
    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    rights_path = rights_review_path(built["release_dir"])
    document = load_json(rights_path)
    assert document["clip_count"] == 2
    assert document["clip_count"] == len(document["clips"])

    # Simulate a stale/wrong count, then reconcile by re-prepare.
    document["clip_count"] = 99
    document["clips"][0]["notes"] = "preserve-me"
    write_json(rights_path, document)
    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    updated = load_json(rights_path)
    assert updated["clip_count"] == 2
    assert updated["clip_count"] == len(updated["clips"])
    assert updated["clips"][0]["notes"] == "preserve-me"


def test_rights_review_validation_rejects_mismatched_clip_count(pipeline_run) -> None:
    built = _build_release(pipeline_run, clip_count=1)
    service = ContentReadinessService(built["catalog"])
    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    rights_path = rights_review_path(built["release_dir"])
    _approve_rights(rights_path)
    document = load_json(rights_path)
    document["clip_count"] = 0
    write_json(rights_path, document)

    from vclip_pipeline.publishing.rights_review import validate_rights_review

    package = load_json(built["release_dir"] / "package-release.json")
    failures = validate_rights_review(document, package)
    assert any("clip_count 0 does not match clips length 1" in item for item in failures)

    result = service.validate(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    assert result["status"] == "not_content_ready"
    assert any("clip_count" in item for item in result["failures"])


def test_rights_review_identity_conflict_fails(pipeline_run) -> None:
    built = _build_release(pipeline_run, clip_count=1)
    package = load_json(built["release_dir"] / "package-release.json")
    existing = {
        "document_version": 1,
        "clips": [
            {
                "sort_order": 1,
                "stock_clip_id": package["clips"][0]["stock_clip_id"],
                "export_id": "EXPORT_OTHER",
                "customer_filename": "x.mp4",
                "review_status": "approved",
                "people": "none_visible",
                "logos_trademarks": "none_visible",
                "artwork_property": "none_visible",
                "license_plates": "none_visible",
                "notes": "",
                "reviewed_by": "bob",
                "reviewed_at": "2026-08-20T00:00:00+00:00",
            }
        ],
    }
    with pytest.raises(VClipError, match="identity conflict"):
        reconcile_rights_review(existing, package)


def test_pending_rights_prevent_content_ready(pipeline_run) -> None:
    built = _build_release(pipeline_run, clip_count=1)
    service = ContentReadinessService(built["catalog"])
    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    result = service.validate(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    assert result["status"] == "not_content_ready"
    assert result["public_metadata_ready"] is True
    assert result["rights_review_ready"] is False
    assert any("need approved" in item for item in result["failures"])
    assert (built["release_dir"] / "package-release.json").is_file()
    package = load_json(built["release_dir"] / "package-release.json")
    assert package["status"] == "release_core_ready"


def test_blocked_rights_prevent_content_ready(pipeline_run) -> None:
    built = _build_release(pipeline_run, clip_count=1)
    service = ContentReadinessService(built["catalog"])
    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    rights_path = rights_review_path(built["release_dir"])
    document = load_json(rights_path)
    document["clips"][0]["review_status"] = "blocked"
    document["clips"][0]["people"] = "reviewed"
    document["clips"][0]["logos_trademarks"] = "reviewed"
    document["clips"][0]["artwork_property"] = "reviewed"
    document["clips"][0]["license_plates"] = "reviewed"
    document["clips"][0]["reviewed_by"] = "bob"
    document["clips"][0]["reviewed_at"] = "2026-08-20T00:00:00+00:00"
    write_json(rights_path, document)

    result = service.validate(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    assert result["status"] == "not_content_ready"
    assert any("blocked" in item for item in result["failures"])


def test_fully_reviewed_content_ready(pipeline_run) -> None:
    built = _build_release(pipeline_run, clip_count=2)
    service = ContentReadinessService(built["catalog"])
    service.prepare(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    _approve_rights(rights_review_path(built["release_dir"]))
    result = service.validate(
        slug=built["slug"],
        version=built["version"],
        release_root=built["release_root"],
    )
    assert result["status"] == "content_ready"
    assert result["public_metadata_ready"] is True
    assert result["rights_review_ready"] is True
    assert result["failures"] == []
    stored = load_json(content_validation_path(built["release_dir"]))
    assert stored["status"] == "content_ready"
    package = load_json(built["release_dir"] / "package-release.json")
    assert package["status"] == "release_core_ready"


def test_publish_content_cli_parser_wiring() -> None:
    parser = build_parser()
    prepare_args = parser.parse_args(
        [
            "publish",
            "content",
            "prepare",
            "demo-slug",
            "--db",
            "vclip.sqlite3",
            "--version",
            "3",
            "--release-root",
            "/tmp/releases",
        ]
    )
    assert prepare_args.handler is workflow_cli._run_publish_content_prepare
    assert prepare_args.slug == "demo-slug"
    assert prepare_args.version == 3

    validate_args = parser.parse_args(
        [
            "publish",
            "content",
            "validate",
            "demo-slug",
            "--db",
            "vclip.sqlite3",
            "--release-root",
            "/tmp/releases",
        ]
    )
    assert validate_args.handler is workflow_cli._run_publish_content_validate
    assert validate_args.version is None


def test_publish_content_prepare_cli(monkeypatch, capsys, tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    class FakeService:
        def __init__(self, catalog) -> None:
            self.catalog = catalog

        def prepare(self, **kwargs):
            calls.append(kwargs)
            return {
                "collection_slug": kwargs["slug"],
                "collection_version": 1,
                "clip_count": 2,
                "public_metadata_path": str(tmp_path / "public-metadata.json"),
                "rights_review_path": str(tmp_path / "rights-review.json"),
                "status": "prepared",
                "note": "note",
            }

    monkeypatch.setattr(workflow_cli, "_catalog", lambda _db: (None, object()))
    monkeypatch.setattr(workflow_cli, "ContentReadinessService", FakeService)
    code = main(
        [
            "publish",
            "content",
            "prepare",
            "sf-golden",
            "--db",
            str(tmp_path / "db.sqlite3"),
            "--version",
            "1",
            "--release-root",
            str(tmp_path / "releases"),
        ]
    )
    assert code == 0
    assert calls[0]["slug"] == "sf-golden"
    assert calls[0]["version"] == 1
    out = capsys.readouterr().out
    assert "Package content prepared" in out
    assert "public-metadata.json" in out


def test_publish_content_validate_cli(monkeypatch, capsys, tmp_path: Path) -> None:
    class FakeService:
        def __init__(self, catalog) -> None:
            self.catalog = catalog

        def validate(self, **kwargs):
            return {
                "collection_slug": kwargs["slug"],
                "collection_version": 1,
                "clip_count": 1,
                "public_metadata_ready": False,
                "rights_review_ready": False,
                "status": "not_content_ready",
                "path": str(tmp_path / "content-validation.json"),
                "failures": ["clip sort_order=1: missing caption"],
            }

    monkeypatch.setattr(workflow_cli, "_catalog", lambda _db: (None, object()))
    monkeypatch.setattr(workflow_cli, "ContentReadinessService", FakeService)
    code = main(
        [
            "publish",
            "content",
            "validate",
            "sf-golden",
            "--db",
            str(tmp_path / "db.sqlite3"),
            "--release-root",
            str(tmp_path / "releases"),
        ]
    )
    assert code == 2
    out = capsys.readouterr().out
    assert "not_content_ready" in out
    assert "missing caption" in out
