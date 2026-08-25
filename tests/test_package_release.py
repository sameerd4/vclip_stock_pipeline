from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from vclip_pipeline.errors import VClipError
from vclip_pipeline.publishing import PackageReleaseService
from vclip_pipeline.reconcile import ReconcileService
from vclip_pipeline.util import sha256_file, stable_id
from vclip_pipeline.workflow import cli as workflow_cli
from vclip_pipeline.workflow.catalog import WorkflowCatalog
from vclip_pipeline.workflow.cli import build_parser, main
from vclip_pipeline.workflow.collections import CollectionService

TITLE = "San Francisco Golden Hour Vertical"


def _reconcile(pipeline_run) -> None:
    ReconcileService(pipeline_run["repository"]).run(
        reviewed_xml=pipeline_run["output"],
        run_id=pipeline_run["result"].stockify_run_id,
        authority="auto",
        scope="full-run",
        report_path=None,
        allow_conflicts=False,
    )


def _seed_collection(
    pipeline_run,
    *,
    title: str = TITLE,
    clip_count: int = 2,
    durations: list[float] | None = None,
    widths: list[int] | None = None,
    heights: list[int] | None = None,
    frame_rates: list[float] | None = None,
    codecs: list[str] | None = None,
) -> dict[str, Any]:
    _reconcile(pipeline_run)
    run_id = pipeline_run["result"].stockify_run_id
    candidates = pipeline_run["repository"].candidates_for_run(
        run_id,
        accepted_only=True,
        approved_only=True,
    )[:clip_count]
    assert len(candidates) >= clip_count

    exports_dir = pipeline_run["tmp_path"] / "release-exports"
    exports_dir.mkdir(exist_ok=True)
    catalog = WorkflowCatalog(pipeline_run["database"])

    prepared: list[dict[str, Any]] = []
    clips: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        payload = f"master-bytes-{index}-{candidate['stock_clip_id']}".encode()
        path = exports_dir / f"{candidate['expected_export_basename']}.mp4"
        path.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        duration = (durations or [7.333333, 10.0])[index]
        width = (widths or [2160, 3840])[index]
        height = (heights or [3840, 2160])[index]
        frame_rate = (frame_rates or [60.0, 30.0])[index]
        codec = (codecs or ["h264", "hevc"])[index]
        export = pipeline_run["repository"].upsert_export(
            {
                "id": stable_id("EXPORT", run_id, candidate["stock_clip_id"]),
                "stockify_run_id": run_id,
                "stock_clip_id": candidate["stock_clip_id"],
                "exported_filename": path.name,
                "exported_path": str(path.resolve()),
                "match_method": "exact_project_name",
                "match_confidence": "high",
                "file_size_bytes": path.stat().st_size,
                "duration_seconds": duration,
                "sha256": digest,
                "reconciled_at": "2026-01-01T00:00:00+00:00",
            }
        )
        catalog.upsert_export_media(
            export_id=str(export["id"]),
            width=width,
            height=height,
            codec_name=codec,
            frame_rate=frame_rate,
            probe={
                "width": width,
                "height": height,
                "codec_name": codec,
                "frame_rate": frame_rate,
                "duration_seconds": duration,
            },
        )
        prepared.append(
            {
                "candidate": candidate,
                "export": export,
                "path": path,
                "digest": digest,
                "duration": duration,
            }
        )
        clips.append(
            {
                "stockify_run_id": run_id,
                "stock_clip_id": candidate["stock_clip_id"],
                "export_id": export["id"],
                "score": 1.0,
                "rationale": {},
            }
        )

    slug = "san-francisco-golden-hour-vertical"
    collection_id = catalog.save_collection_definition(
        slug=slug,
        title=title,
        description="Golden hour vertical aerials.",
        rule={"markets": ["san-francisco"]},
    )
    published = catalog.publish_collection_version(
        collection_id=collection_id,
        rule={"markets": ["san-francisco"]},
        metadata={"title": title, "slug": slug, "selected_count": clip_count},
        clips=clips,
    )
    return {
        "catalog": catalog,
        "published": published,
        "prepared": prepared,
        "slug": slug,
        "version": published["version"],
        "collection_version_id": published["collection_version_id"],
    }


def _patch_export(pipeline_run, export_id: str, **fields: Any) -> None:
    assignments = ", ".join(f"{key}=?" for key in fields)
    with pipeline_run["database"].transaction() as connection:
        connection.execute(
            f"UPDATE exports SET {assignments} WHERE id=?",
            (*fields.values(), export_id),
        )


def test_hardlink_failure_does_not_fallback_to_copy(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.mp4"
    destination = tmp_path / "dest.mp4"
    source.write_bytes(b"master")

    def fail_link(src: str, dst: str) -> None:
        raise OSError(18, "Invalid cross-device link")

    copy_calls: list[tuple[Any, ...]] = []

    def spy_copy2(*args: Any, **kwargs: Any) -> None:
        copy_calls.append(args)

    monkeypatch.setattr("vclip_pipeline.workflow.collections.os.link", fail_link)
    monkeypatch.setattr("vclip_pipeline.workflow.collections.shutil.copy2", spy_copy2)

    with pytest.raises(VClipError, match="Hardlinking failed"):
        CollectionService._transfer(source, destination, "hardlink")

    assert copy_calls == []
    assert not destination.exists()


def test_package_release_builds_manifest_and_aggregates(pipeline_run) -> None:
    seeded = _seed_collection(pipeline_run, clip_count=2)
    output = pipeline_run["tmp_path"] / "releases"
    service = PackageReleaseService(seeded["catalog"])
    manifest = service.build(
        slug=seeded["slug"],
        version=seeded["version"],
        output_directory=output,
        overwrite=False,
        dry_run=False,
    )

    assert manifest["status"] == "release_core_ready"
    assert manifest["package_revision"] == 1
    assert manifest["package_id"] == stable_id("PACKAGE", seeded["collection_version_id"])
    assert manifest["clip_count"] == 2
    assert manifest["title"] == TITLE
    assert manifest["clips"][0]["customer_filename"] == f"{TITLE} — Clip 01.mp4"
    assert manifest["clips"][1]["customer_filename"] == f"{TITLE} — Clip 02.mp4"
    assert manifest["total_duration_seconds"] == pytest.approx(17.333333)
    assert manifest["total_size_bytes"] == sum(
        item["path"].stat().st_size for item in seeded["prepared"]
    )
    assert manifest["formats"]["orientations"] == ["landscape", "vertical"]
    assert manifest["formats"]["resolutions"] == ["2160x3840", "3840x2160"]
    assert manifest["formats"]["frame_rates"] == [30.0, 60.0]
    assert manifest["formats"]["codecs"] == ["h264", "hevc"]

    path = Path(manifest["manifest_path"])
    assert path.is_file()
    assert path.name == "package-release.json"
    assert path.parent.name == f"v{seeded['version']}"
    assert path.parent.parent.name == seeded["slug"]
    assert list(path.parent.rglob("*.mp4")) == []

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert "manifest_path" not in stored
    assert stored["clips"][0]["master_sha256"] == seeded["prepared"][0]["digest"]
    assert stored["clips"][0]["export_id"] == seeded["prepared"][0]["export"]["id"]


def test_package_release_dry_run_writes_nothing(pipeline_run) -> None:
    seeded = _seed_collection(pipeline_run, clip_count=1)
    output = pipeline_run["tmp_path"] / "releases-dry"
    service = PackageReleaseService(seeded["catalog"])
    manifest = service.build(
        slug=seeded["slug"],
        version=None,
        output_directory=output,
        dry_run=True,
    )
    assert manifest["clip_count"] == 1
    assert manifest["status"] == "release_core_ready"
    assert "manifest_path" not in manifest
    assert not output.exists()


def test_package_release_overwrite_behavior(pipeline_run) -> None:
    seeded = _seed_collection(pipeline_run, clip_count=1)
    output = pipeline_run["tmp_path"] / "releases-ow"
    service = PackageReleaseService(seeded["catalog"])
    first = service.build(
        slug=seeded["slug"],
        version=seeded["version"],
        output_directory=output,
    )
    path = Path(first["manifest_path"])

    with pytest.raises(VClipError, match="already exists"):
        service.build(
            slug=seeded["slug"],
            version=seeded["version"],
            output_directory=output,
            overwrite=False,
        )

    second = service.build(
        slug=seeded["slug"],
        version=seeded["version"],
        output_directory=output,
        overwrite=True,
    )
    assert Path(second["manifest_path"]) == path
    assert path.is_file()


def test_package_release_missing_master(pipeline_run) -> None:
    seeded = _seed_collection(pipeline_run, clip_count=1)
    seeded["prepared"][0]["path"].unlink()
    service = PackageReleaseService(seeded["catalog"])
    with pytest.raises(VClipError, match="Master export is missing"):
        service.build(
            slug=seeded["slug"],
            version=seeded["version"],
            output_directory=pipeline_run["tmp_path"] / "out",
            dry_run=True,
        )


def test_package_release_missing_hash(pipeline_run) -> None:
    seeded = _seed_collection(pipeline_run, clip_count=1)
    _patch_export(pipeline_run, seeded["prepared"][0]["export"]["id"], sha256=None)
    service = PackageReleaseService(seeded["catalog"])
    with pytest.raises(VClipError, match="missing sha256"):
        service.build(
            slug=seeded["slug"],
            version=seeded["version"],
            output_directory=pipeline_run["tmp_path"] / "out",
            dry_run=True,
        )


def test_package_release_missing_duration(pipeline_run) -> None:
    seeded = _seed_collection(pipeline_run, clip_count=1)
    _patch_export(
        pipeline_run,
        seeded["prepared"][0]["export"]["id"],
        duration_seconds=None,
    )
    service = PackageReleaseService(seeded["catalog"])
    with pytest.raises(VClipError, match="missing duration_seconds"):
        service.build(
            slug=seeded["slug"],
            version=seeded["version"],
            output_directory=pipeline_run["tmp_path"] / "out",
            dry_run=True,
        )


def test_package_release_missing_media_metadata(pipeline_run) -> None:
    seeded = _seed_collection(pipeline_run, clip_count=1)
    with pipeline_run["database"].transaction() as connection:
        connection.execute(
            "DELETE FROM export_media_metadata WHERE export_id=?",
            (seeded["prepared"][0]["export"]["id"],),
        )
    service = PackageReleaseService(seeded["catalog"])
    with pytest.raises(VClipError, match="export_media_metadata"):
        service.build(
            slug=seeded["slug"],
            version=seeded["version"],
            output_directory=pipeline_run["tmp_path"] / "out",
            dry_run=True,
        )


def test_package_release_hash_mismatch(pipeline_run) -> None:
    seeded = _seed_collection(pipeline_run, clip_count=1)
    path = seeded["prepared"][0]["path"]
    path.write_bytes(b"tampered-master-bytes")
    service = PackageReleaseService(seeded["catalog"])
    with pytest.raises(VClipError, match="SHA-256 mismatch"):
        service.build(
            slug=seeded["slug"],
            version=seeded["version"],
            output_directory=pipeline_run["tmp_path"] / "out",
            dry_run=True,
        )


def test_package_release_does_not_transfer_masters(pipeline_run, monkeypatch) -> None:
    seeded = _seed_collection(pipeline_run, clip_count=1)
    forbidden = MagicMock(side_effect=AssertionError("must not transfer masters"))
    monkeypatch.setattr(shutil, "copy", forbidden)
    monkeypatch.setattr(shutil, "copy2", forbidden)
    monkeypatch.setattr(shutil, "move", forbidden)
    monkeypatch.setattr("os.link", forbidden)
    monkeypatch.setattr(Path, "symlink_to", forbidden)

    service = PackageReleaseService(seeded["catalog"])
    manifest = service.build(
        slug=seeded["slug"],
        version=seeded["version"],
        output_directory=pipeline_run["tmp_path"] / "releases-safe",
        dry_run=False,
    )
    assert manifest["clip_count"] == 1
    assert sha256_file(seeded["prepared"][0]["path"]) == manifest["clips"][0]["master_sha256"]


def test_package_release_rejects_non_positive_duration(pipeline_run) -> None:
    seeded = _seed_collection(pipeline_run, clip_count=1, durations=[0.0])
    service = PackageReleaseService(seeded["catalog"])
    with pytest.raises(VClipError, match="non-positive"):
        service.build(
            slug=seeded["slug"],
            version=seeded["version"],
            output_directory=pipeline_run["tmp_path"] / "out",
            dry_run=True,
        )


def _fake_release_manifest(**overrides: Any) -> dict[str, Any]:
    payload = {
        "package_id": "PACKAGE_test",
        "collection_slug": "san-francisco-golden-hour-vertical",
        "collection_version": 1,
        "clip_count": 2,
        "total_duration_seconds": 17.333333,
        "total_size_bytes": 4096,
        "status": "release_core_ready",
    }
    payload.update(overrides)
    return payload


def test_publish_release_parser_wiring() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "publish",
            "release",
            "--db",
            "catalog.sqlite3",
            "--version",
            "3",
            "--output",
            "/tmp/releases",
            "--overwrite",
            "--dry-run",
            "sf-golden-hour",
        ]
    )
    assert args.command == "publish"
    assert args.publish_command == "release"
    assert args.slug == "sf-golden-hour"
    assert args.db == Path("catalog.sqlite3")
    assert args.version == 3
    assert args.output == Path("/tmp/releases")
    assert args.overwrite is True
    assert args.dry_run is True
    assert args.handler is workflow_cli._run_publish_release

    defaults = parser.parse_args(
        [
            "publish",
            "release",
            "--output",
            "/tmp/releases",
            "sf-golden-hour",
        ]
    )
    assert defaults.version is None
    assert defaults.overwrite is False
    assert defaults.dry_run is False


def test_publish_release_cli_dry_run(monkeypatch, capsys, tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    class FakeService:
        def __init__(self, catalog: Any) -> None:
            self.catalog = catalog

        def build(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return _fake_release_manifest()

    monkeypatch.setattr(workflow_cli, "_catalog", lambda _db: (None, object()))
    monkeypatch.setattr(workflow_cli, "PackageReleaseService", FakeService)

    code = main(
        [
            "publish",
            "release",
            "--db",
            str(tmp_path / "vclip.sqlite3"),
            "--output",
            str(tmp_path / "releases"),
            "--dry-run",
            "san-francisco-golden-hour-vertical",
        ]
    )
    assert code == 0
    assert len(calls) == 1
    assert calls[0]["slug"] == "san-francisco-golden-hour-vertical"
    assert calls[0]["version"] is None
    assert calls[0]["output_directory"] == tmp_path / "releases"
    assert calls[0]["overwrite"] is False
    assert calls[0]["dry_run"] is True

    out = capsys.readouterr().out
    assert "Package release core ready" in out
    assert "Package ID:  PACKAGE_test" in out
    assert "Collection:  san-francisco-golden-hour-vertical" in out
    assert "Version:     1" in out
    assert "Clips:       2" in out
    assert "Duration:    17.333333" in out
    assert "Size:        4096" in out
    assert "Status:      release_core_ready" in out
    assert "no files were written" in out


def test_publish_release_cli_normal(monkeypatch, capsys, tmp_path: Path) -> None:
    manifest_path = (
        tmp_path / "releases" / "san-francisco-golden-hour-vertical" / "v1" / "package-release.json"
    )
    calls: list[dict[str, Any]] = []

    class FakeService:
        def __init__(self, catalog: Any) -> None:
            self.catalog = catalog

        def build(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return _fake_release_manifest(manifest_path=str(manifest_path))

    monkeypatch.setattr(workflow_cli, "_catalog", lambda _db: (None, object()))
    monkeypatch.setattr(workflow_cli, "PackageReleaseService", FakeService)

    code = main(
        [
            "publish",
            "release",
            "--db",
            str(tmp_path / "vclip.sqlite3"),
            "--version",
            "1",
            "--output",
            str(tmp_path / "releases"),
            "san-francisco-golden-hour-vertical",
        ]
    )
    assert code == 0
    assert calls[0]["version"] == 1
    assert calls[0]["dry_run"] is False
    assert calls[0]["overwrite"] is False

    out = capsys.readouterr().out
    assert "Package release core ready" in out
    assert f"Output:      {manifest_path}" in out
    assert "no files were written" not in out


def test_publish_release_cli_overwrite_propagation(monkeypatch, capsys, tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    class FakeService:
        def __init__(self, catalog: Any) -> None:
            self.catalog = catalog

        def build(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return _fake_release_manifest(
                manifest_path=str(tmp_path / "out" / "package-release.json")
            )

    monkeypatch.setattr(workflow_cli, "_catalog", lambda _db: (None, object()))
    monkeypatch.setattr(workflow_cli, "PackageReleaseService", FakeService)

    code = main(
        [
            "publish",
            "release",
            "--db",
            str(tmp_path / "vclip.sqlite3"),
            "--output",
            str(tmp_path / "out"),
            "--overwrite",
            "sf-slug",
        ]
    )
    assert code == 0
    assert calls[0]["overwrite"] is True
    assert calls[0]["dry_run"] is False
    assert calls[0]["slug"] == "sf-slug"
    assert "Output:" in capsys.readouterr().out
