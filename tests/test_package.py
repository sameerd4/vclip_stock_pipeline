from __future__ import annotations

import json
from pathlib import Path

import pytest

from vclip_pipeline.errors import VClipError
from vclip_pipeline.packaging import PackageService
from vclip_pipeline.packaging.media import MediaProbe
from vclip_pipeline.packaging.weather import NoWeatherProvider
from vclip_pipeline.reconcile import ReconcileService


def test_package_matches_exports_and_writes_public_and_internal_metadata(pipeline_run):
    ReconcileService(pipeline_run["repository"]).run(
        reviewed_xml=pipeline_run["output"],
        run_id=pipeline_run["result"].stockify_run_id,
        authority="auto",
        scope="full-run",
        report_path=None,
        allow_conflicts=False,
    )
    candidates = pipeline_run["repository"].candidates_for_run(
        pipeline_run["result"].stockify_run_id,
        accepted_only=True,
        approved_only=True,
    )
    exports = pipeline_run["tmp_path"] / "exports"
    exports.mkdir()
    for candidate in candidates:
        name = candidate["expected_export_basename"]
        (exports / f"{name}.mp4").write_bytes(b"fake-video")

    output = pipeline_run["tmp_path"] / "packages"
    report = PackageService(pipeline_run["repository"]).run(
        exports_directory=exports,
        output_directory=output,
        run_id=pipeline_run["result"].stockify_run_id,
        project_labels={"South Lake Union Evening — Graded 1"},
        mode="copy",
        weather_provider=NoWeatherProvider(),
        calculate_checksums=False,
        inspect_media=False,
        allow_unmatched=True,
        allow_missing=False,
        allow_duration_mismatch=False,
        allow_unreconciled=False,
        require_weather=False,
        overwrite=False,
        dry_run=False,
        report_path=None,
    )
    assert report.packages_created == 1
    package = output / "south-lake-union-evening-graded-1"
    public = json.loads((package / "metadata.json").read_text())
    internal = json.loads((package / "vclip-internal.json").read_text())
    manifest = json.loads((package / "manifest.json").read_text())

    assert public["title"] == "South Lake Union Evening — Graded 1"
    assert public["location"]["neighborhood"] == "South Lake Union"
    assert "center_lat" not in public["location"]
    assert public["clip_count"] == 2
    assert public["capture"]["time_of_day"] == "Evening"
    assert public["weather"]["status"] == "not_enriched"
    assert "source_latitude" not in public["weather"]
    assert public["astronomy"]["status"] == "enriched"
    assert public["astronomy"]["solar_period"] in {
        "pre_dawn",
        "sunrise_window",
        "morning",
        "day",
        "sunset_window",
        "dusk",
        "night",
    }
    assert public["astronomy"]["sunrise_time"]
    assert public["astronomy"]["sunset_time"]
    assert "sunrise" not in public["search_tags"]
    assert "sunset" not in public["search_tags"]
    assert public["astronomy"]["visual_analysis"]["status"] == "not_analyzed"
    assert internal["source_project_name"] == "Hot Gunna Thug"
    assert "source_latitude" in internal["astronomy"]
    assert internal["session"]["center_lat"] is not None
    assert manifest["status"] == "ready"
    assert len(list((package / "clips").glob("*.mp4"))) == 2


def test_package_enriches_weather_by_default_and_adds_condition_search_tags(
    pipeline_run, monkeypatch
):
    ReconcileService(pipeline_run["repository"]).run(
        reviewed_xml=pipeline_run["output"],
        run_id=pipeline_run["result"].stockify_run_id,
        authority="auto",
        scope="full-run",
        report_path=None,
        allow_conflicts=False,
    )
    payload = {
        "latitude": 47.625,
        "longitude": -122.338,
        "timezone": "America/Los_Angeles",
        "hourly": {
            "time": [f"2026-05-09T{hour:02d}:00" for hour in range(24)],
            "temperature_2m": [18.0] * 24,
            "precipitation": [0.0] * 24,
            "rain": [0.0] * 24,
            "cloud_cover": [15.0] * 24,
            "visibility": [20000.0] * 24,
            "wind_speed_10m": [8.0] * 24,
            "weather_code": [1] * 24,
        },
    }

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(
        "vclip_pipeline.packaging.weather.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(),
    )

    candidates = [
        candidate
        for candidate in pipeline_run["repository"].candidates_for_run(
            pipeline_run["result"].stockify_run_id,
            accepted_only=True,
            approved_only=True,
        )
        if candidate["generated_project_label"] == "South Lake Union Evening — Graded 1"
    ]
    exports = pipeline_run["tmp_path"] / "weather-exports"
    exports.mkdir()
    for candidate in candidates:
        (exports / f"{candidate['expected_export_basename']}.mp4").write_bytes(b"fake")

    output = pipeline_run["tmp_path"] / "weather-packages"
    report = PackageService(pipeline_run["repository"]).run(
        exports_directory=exports,
        output_directory=output,
        run_id=pipeline_run["result"].stockify_run_id,
        project_labels={"South Lake Union Evening — Graded 1"},
        mode="copy",
        weather_provider=None,  # default Open-Meteo path
        calculate_checksums=False,
        inspect_media=False,
        allow_unmatched=True,
        allow_missing=False,
        allow_duration_mismatch=False,
        allow_unreconciled=False,
        require_weather=True,
        overwrite=False,
        dry_run=False,
        report_path=None,
    )
    assert report.packages_created == 1
    public = json.loads(
        (output / "south-lake-union-evening-graded-1" / "metadata.json").read_text()
    )
    assert public["weather"]["status"] == "enriched"
    assert public["weather"]["provider"] == "open-meteo"
    assert public["weather"]["condition_label"] == "mainly_clear"
    assert public["weather"]["requested_at"]
    assert public["weather"]["observed_at"]
    assert public["weather"]["grid_latitude"] == 47.625
    assert "source_latitude" not in public["weather"]
    assert "mainly_clear" in public["search_tags"]
    sessions = {
        str(session["id"]): session
        for session in pipeline_run["repository"].sessions_for_run(
            pipeline_run["result"].stockify_run_id
        )
    }
    project = next(
        project
        for project in pipeline_run["repository"].projects_for_run(
            pipeline_run["result"].stockify_run_id
        )
        if project.get("generated_project_label") == "South Lake Union Evening — Graded 1"
    )
    cached = pipeline_run["repository"].weather_for_session(
        str(project["session_id"]), "open-meteo"
    )
    assert cached is not None
    assert cached["status"] == "enriched"
    assert cached["requested_at"]
    assert cached["observed_at"]
    assert cached["source_latitude"] == sessions[str(project["session_id"])]["center_lat"]
    assert cached["grid_latitude"] == 47.625
    assert "source_latitude" in (internal := json.loads(
        (output / "south-lake-union-evening-graded-1" / "vclip-internal.json").read_text()
    ))["weather"]
    assert internal["weather"]["source_latitude"] == cached["source_latitude"]


def test_allow_missing_marks_package_partial(pipeline_run):
    ReconcileService(pipeline_run["repository"]).run(
        reviewed_xml=pipeline_run["output"],
        run_id=pipeline_run["result"].stockify_run_id,
        authority="auto",
        scope="full-run",
        report_path=None,
        allow_conflicts=False,
    )
    candidates = [
        candidate
        for candidate in pipeline_run["repository"].candidates_for_run(
            pipeline_run["result"].stockify_run_id,
            accepted_only=True,
            approved_only=True,
        )
        if candidate["generated_project_label"] == "South Lake Union Evening — Graded 1"
    ]
    exports = pipeline_run["tmp_path"] / "partial-exports"
    exports.mkdir()
    (exports / f"{candidates[0]['expected_export_basename']}.mp4").write_bytes(b"fake-video")

    output = pipeline_run["tmp_path"] / "partial-packages"
    report = PackageService(pipeline_run["repository"]).run(
        exports_directory=exports,
        output_directory=output,
        run_id=pipeline_run["result"].stockify_run_id,
        project_labels={"South Lake Union Evening — Graded 1"},
        mode="copy",
        weather_provider=NoWeatherProvider(),
        calculate_checksums=False,
        inspect_media=False,
        allow_unmatched=False,
        allow_missing=True,
        allow_duration_mismatch=False,
        allow_unreconciled=False,
        require_weather=False,
        overwrite=False,
        dry_run=False,
        report_path=None,
    )
    assert report.missing_candidate_ids == [candidates[1]["stock_clip_id"]]
    manifest = json.loads(
        (
            output
            / "south-lake-union-evening-graded-1"
            / "manifest.json"
        ).read_text()
    )
    assert manifest["status"] == "partial"


def test_material_export_duration_mismatch_is_blocked(pipeline_run, monkeypatch):
    ReconcileService(pipeline_run["repository"]).run(
        reviewed_xml=pipeline_run["output"],
        run_id=pipeline_run["result"].stockify_run_id,
        authority="auto",
        scope="full-run",
        report_path=None,
        allow_conflicts=False,
    )
    candidates = [
        candidate
        for candidate in pipeline_run["repository"].candidates_for_run(
            pipeline_run["result"].stockify_run_id,
            accepted_only=True,
            approved_only=True,
        )
        if candidate["generated_project_label"] == "South Lake Union Evening — Graded 1"
    ]
    exports = pipeline_run["tmp_path"] / "mismatched-exports"
    exports.mkdir()
    for candidate in candidates:
        (exports / f"{candidate['expected_export_basename']}.mp4").write_bytes(
            b"fake-video"
        )
    monkeypatch.setattr(
        "vclip_pipeline.packaging.service.probe_media",
        lambda _path: MediaProbe(30.0, 1920, 1080, "h264", 30.0),
    )

    with pytest.raises(VClipError, match="differ materially"):
        PackageService(pipeline_run["repository"]).run(
            exports_directory=exports,
            output_directory=pipeline_run["tmp_path"] / "mismatched-packages",
            run_id=pipeline_run["result"].stockify_run_id,
            project_labels={"South Lake Union Evening — Graded 1"},
            mode="copy",
            weather_provider=NoWeatherProvider(),
            calculate_checksums=False,
            inspect_media=True,
            allow_unmatched=False,
            allow_missing=False,
            allow_duration_mismatch=False,
            allow_unreconciled=False,
            require_weather=False,
            overwrite=False,
            dry_run=False,
            report_path=None,
        )


def test_export_relocation_preserves_id_and_package_clips_fk(pipeline_run):
    """Moving an export file must keep exports.id and package_clips.export_id aligned."""
    import shutil

    from vclip_pipeline.util import export_stable_id, stable_id

    ReconcileService(pipeline_run["repository"]).run(
        reviewed_xml=pipeline_run["output"],
        run_id=pipeline_run["result"].stockify_run_id,
        authority="auto",
        scope="full-run",
        report_path=None,
        allow_conflicts=False,
    )
    candidates = pipeline_run["repository"].candidates_for_run(
        pipeline_run["result"].stockify_run_id,
        accepted_only=True,
        approved_only=True,
    )
    target = candidates[0]
    run_id = pipeline_run["result"].stockify_run_id
    clip_id = target["stock_clip_id"]
    basename = f"{target['expected_export_basename']}.mp4"

    path_a = pipeline_run["tmp_path"] / "exports-a"
    path_b = pipeline_run["tmp_path"] / "exports-b"
    path_a.mkdir()
    path_b.mkdir()
    file_a = path_a / basename
    file_a.write_bytes(b"fake-video-a")

    # Simulate a legacy path-derived export id already in the DB (pre-fix rows).
    legacy_id = stable_id("EXPORT", run_id, clip_id, str(file_a.resolve()))
    logical_id = export_stable_id(run_id, clip_id)
    assert legacy_id != logical_id
    pipeline_run["repository"].upsert_export(
        {
            "id": legacy_id,
            "stockify_run_id": run_id,
            "stock_clip_id": clip_id,
            "exported_filename": basename,
            "exported_path": str(file_a.resolve()),
            "match_method": "exact_project_name",
            "match_confidence": "high",
            "file_size_bytes": file_a.stat().st_size,
            "duration_seconds": None,
            "sha256": None,
            "reconciled_at": "2026-01-01T00:00:00+00:00",
        }
    )

    file_b = path_b / basename
    shutil.copy2(file_a, file_b)

    output = pipeline_run["tmp_path"] / "packages-relocated"
    report = PackageService(pipeline_run["repository"]).run(
        exports_directory=path_b,
        output_directory=output,
        run_id=run_id,
        project_labels={target["generated_project_label"]},
        mode="copy",
        weather_provider=NoWeatherProvider(),
        calculate_checksums=False,
        inspect_media=False,
        allow_unmatched=True,
        allow_missing=True,
        allow_duration_mismatch=False,
        allow_unreconciled=False,
        require_weather=False,
        overwrite=False,
        dry_run=False,
        report_path=None,
    )
    assert report.packages_created == 1

    stored = pipeline_run["repository"].export_for_candidate(run_id, clip_id)
    assert stored is not None
    assert stored["id"] == legacy_id
    assert stored["exported_path"] == str(file_b.resolve())

    with pipeline_run["database"].connect() as connection:
        package_clip = connection.execute(
            """
            SELECT export_id, stock_clip_id
            FROM package_clips
            WHERE stockify_run_id=? AND stock_clip_id=?
            """,
            (run_id, clip_id),
        ).fetchone()
        export_exists = connection.execute(
            "SELECT 1 FROM exports WHERE id=?",
            (package_clip["export_id"],),
        ).fetchone()
    assert package_clip is not None
    assert package_clip["export_id"] == legacy_id
    assert export_exists is not None
