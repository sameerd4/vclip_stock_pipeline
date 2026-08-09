from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from vclip_pipeline.cli import main
from vclip_pipeline.db import CatalogRepository, Database
from vclip_pipeline.geo import (
    CatalogLocationResolver,
    CompositeLocationResolver,
    build_location_resolver,
    default_places_path,
    resolve_place,
)
from vclip_pipeline.stockify.location_recovery import (
    LocationRecoveryService,
    format_location_recovery_report,
    rewrite_review_xml_names,
)
from vclip_pipeline.util import json_dumps


def _seed_unknown_session(
    repository: CatalogRepository,
    *,
    run_id: str = "STOCKIFY_LOC",
    output_xml: Path | None = None,
    clip_location: dict | None = None,
    second_clip_location: dict | None = None,
    capture_date: str = "2026-02-06",
) -> str:
    """Seed one unknown session for a Stockify run. Returns the session id."""
    database = repository.database
    output = str(output_xml or f"{run_id}-review.fcpxml")
    event_id = f"EVT_{run_id}"
    session_id = f"SESS_{run_id}"
    project_id = f"PROJ_{run_id}"
    clip_a = f"CLIP_A_{run_id}"
    clip_b = f"CLIP_B_{run_id}"
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO stockify_runs (
                id, source_xml_path, source_xml_sha256, output_xml_path, report_path,
                pipeline_version, status, options_json, started_at, completed_at
            ) VALUES (?, ?, 'abc', ?, 'report.json', '0.1.0', 'complete', '{}', 't', 't')
            """,
            (run_id, f"{run_id}.xml", output),
        )
        connection.execute(
            """
            INSERT INTO source_events (id, run_id, source_index, source_name, source_uid)
            VALUES (?, ?, 0, 'Random Shoot Day', NULL)
            """,
            (event_id, run_id),
        )
        connection.execute(
            """
            INSERT INTO shoot_sessions (
                id, run_id, session_key, capture_date, captured_at_local, timezone,
                center_lat, center_lon, gps_radius_meters, country, state, city,
                neighborhood, poi, public_label, location_confidence, time_of_day,
                time_of_day_confidence, generated_event_name, generated_base_label,
                anchor_stock_clip_id, weather_status, location_json, capture_json,
                created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?,
                ?, 'America/Los_Angeles',
                NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                'morning', 'medium',
                ?, 'Unknown Location Morning',
                ?, 'not_enriched', ?, ?, 't', 't'
            )
            """,
            (
                session_id,
                run_id,
                f"unknown-{capture_date}-{run_id}",
                capture_date,
                f"{capture_date}T08:00:00",
                f"Unknown Location — {capture_date}",
                clip_a,
                json_dumps({"status": "unknown"}),
                json_dumps(
                    {
                        "date": capture_date,
                        "captured_at_local": f"{capture_date}T08:00:00",
                    }
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO source_projects (
                id, run_id, source_event_id, source_index, source_name, source_uid,
                classification, session_id, anchor_segment_index, generated_event_name,
                generated_project_label, generated_compilation_name,
                accepted_clip_count, skipped_clip_count, created_at, updated_at
            ) VALUES (
                ?, ?, ?, 0, 'Random Project', NULL, 'accepted',
                ?, 0, ?,
                'Unknown Location Morning', 'Unknown Location Morning — Stock Compilation',
                2, 0, 't', 't'
            )
            """,
            (
                project_id,
                run_id,
                event_id,
                session_id,
                f"Unknown Location — {capture_date}",
            ),
        )
        for clip_id, segment, location, seq in (
            (clip_a, 0, clip_location or {}, 1),
            (
                clip_b,
                1,
                second_clip_location
                if second_clip_location is not None
                else (clip_location or {}),
                2,
            ),
        ):
            connection.execute(
                """
                INSERT INTO stock_candidates (
                    run_id, stock_clip_id, source_project_id, source_media_id, session_id,
                    source_segment_index, source_ref, source_name, eligibility_status,
                    rejection_reason, rejection_detail, original_start, original_duration,
                    original_duration_seconds, proposed_start, proposed_duration,
                    proposed_duration_seconds, short_clip_recovery, candidate_tier,
                    sidecar_path, srt_status, srt_window_status, srt_reasons_json,
                    visual_status, visual_reasons_json, visual_metrics_json, location_json,
                    capture_time_json, time_of_day_json, weather_json, creative_effects_json,
                    generated_event_name, generated_project_label, generated_compilation_name,
                    generated_clip_project_name, clip_sequence, expected_export_basename,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?, NULL, ?, ?, NULL, ?, 'accepted',
                    NULL, NULL, '0s', '5s', 5.0, '0s', '5s', 5.0, NULL, 'primary',
                    NULL, 'matched', 'matched', '[]', NULL, '[]', '{}', ?,
                    ?, '{"label":"morning","confidence":"medium"}', '{}', '[]',
                    ?, 'Unknown Location Morning',
                    'Unknown Location Morning — Stock Compilation',
                    ?, ?, ?, 't', 't'
                )
                """,
                (
                    run_id,
                    clip_id,
                    project_id,
                    session_id,
                    segment,
                    f"{clip_id}.MP4",
                    json_dumps(location),
                    json_dumps(
                        {
                            "date": capture_date,
                            "captured_at_local": f"{capture_date}T08:00:00",
                        }
                    ),
                    f"Unknown Location — {capture_date}",
                    f"Unknown Location Morning — Clip {seq:02d}",
                    seq,
                    f"Unknown Location Morning — Clip {seq:02d}",
                ),
            )
            connection.execute(
                """
                INSERT INTO generated_occurrences (
                    run_id, stock_clip_id, representation, generated_event_name,
                    generated_project_name, project_uid, source_start, duration,
                    timeline_offset, effect_signature
                ) VALUES
                (?, ?, 'individual', ?, ?, NULL, '0s', '5s', '0s', NULL),
                (?, ?, 'compilation', ?,
                 'Unknown Location Morning — Stock Compilation', NULL, '0s', '5s', '0s', NULL)
                """,
                (
                    run_id,
                    clip_id,
                    f"Unknown Location — {capture_date}",
                    f"Unknown Location Morning — Clip {seq:02d}",
                    run_id,
                    clip_id,
                    f"Unknown Location — {capture_date}",
                ),
            )
    return session_id


def _write_unknown_review_xml(path: Path, capture_date: str = "2026-02-06") -> None:
    path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.12">
  <library>
    <event name="Unknown Location — {capture_date}">
      <project name="Unknown Location Morning — Stock Compilation"/>
      <project name="Unknown Location Morning — Clip 01"/>
      <project name="Unknown Location Morning — Clip 02"/>
    </event>
  </library>
</fcpxml>
""",
        encoding="utf-8",
    )


def _catalog_resolver(repository: CatalogRepository) -> CompositeLocationResolver:
    return build_location_resolver(
        repository,
        places_path=default_places_path(),
        enable_nominatim=False,
    )


def _capitol_hill_gps() -> dict:
    # Exact Capitol Hill catalog anchor from places.json.
    return {
        "status": "resolved",
        "confidence": "high",
        "center_lat": 47.6231,
        "center_lon": -122.3165,
        "sample_count": 12,
        "radius_meters": 8.0,
        "evidence_sources": ["srt_gps"],
    }


def _ocean_gps() -> dict:
    # Non-null-island point far from the bundled place catalog.
    return {
        "status": "resolved",
        "confidence": "high",
        "center_lat": 1.0,
        "center_lon": 2.0,
        "sample_count": 3,
        "radius_meters": 1.0,
        "evidence_sources": ["srt_gps"],
    }


class ExplodingResolver:
    """Raises for one coordinate pair, otherwise delegates to a real catalog."""

    def __init__(self, delegate: CatalogLocationResolver, *, boom_lat: float) -> None:
        self.delegate = delegate
        self.boom_lat = boom_lat

    def resolve(self, latitude: float, longitude: float):
        if abs(latitude - self.boom_lat) < 1e-6:
            raise RuntimeError("simulated resolver failure")
        return self.delegate.resolve(latitude, longitude)


def test_resolve_place_uses_location_resolver_interface():
    catalog = CatalogLocationResolver.from_json(default_places_path())
    composite = CompositeLocationResolver([catalog])
    place = resolve_place(composite, 47.6231, -122.3165)
    assert place is not None
    assert place["city"] == "Seattle"
    assert place["neighborhood"] == "Capitol Hill"
    assert place["provider"] == "local_catalog"
    assert not callable(composite)


def test_composite_resolver_recovers_unknown_session_with_srt_gps(tmp_path: Path):
    database = Database(tmp_path / "recover.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    review = tmp_path / "review.fcpxml"
    _write_unknown_review_xml(review)
    _seed_unknown_session(
        repository,
        output_xml=review,
        clip_location=_capitol_hill_gps(),
        second_clip_location={"status": "unknown"},
    )

    resolver = _catalog_resolver(repository)
    assert isinstance(resolver, CompositeLocationResolver)

    report = LocationRecoveryService(repository, resolver).run(
        run_id="STOCKIFY_LOC",
        dry_run=False,
        rewrite_review_xml=True,
        report_path=tmp_path / "recovery-report.json",
    )

    assert report.stockify_runs_scanned == 1
    assert report.unknown_sessions_before == 1
    assert report.resolved_by_srt_consensus == 1
    assert report.still_unknown == 0
    assert report.clips_recovered == 2
    assert report.review_xmls_rewritten == 1
    assert format_location_recovery_report(report) == [
        "Stockify runs scanned:       1",
        "Unknown sessions before:     1",
        "Resolved by SRT consensus:   1",
        "Still unknown:               0",
        "Clips recovered:             2",
        "Review XMLs rewritten:       1",
    ]

    session = repository.sessions_for_run("STOCKIFY_LOC")[0]
    assert session["city"] == "Seattle"
    assert session["neighborhood"] == "Capitol Hill"
    assert "Unknown Location" not in session["generated_event_name"]
    assert session["location"]["recovery"]["method"] == "flight_trajectory"
    assert "CLIP_A_STOCKIFY_LOC" in session["location"]["recovery"]["contributing_clip_ids"]

    candidates = repository.candidates_for_run("STOCKIFY_LOC", accepted_only=True)
    assert all(item["location"].get("city") == "Seattle" for item in candidates)
    assert all("Capitol Hill" in (item["generated_project_label"] or "") for item in candidates)

    root = ET.parse(review).getroot()
    event = root.find("./library/event")
    assert event is not None
    assert "Capitol Hill" in (event.get("name") or "")
    project_names = [project.get("name") for project in event.findall("project")]
    assert any(name and name.endswith("Clip 01") for name in project_names)
    assert "Unknown Location" not in " ".join(name or "" for name in project_names)


def test_catalog_wide_recovery_processes_multiple_runs(tmp_path: Path):
    database = Database(tmp_path / "multi.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    review_a = tmp_path / "review-a.fcpxml"
    review_b = tmp_path / "review-b.fcpxml"
    _write_unknown_review_xml(review_a, "2026-02-06")
    _write_unknown_review_xml(review_b, "2026-03-01")
    session_a = _seed_unknown_session(
        repository,
        run_id="STOCKIFY_A",
        output_xml=review_a,
        clip_location=_capitol_hill_gps(),
        capture_date="2026-02-06",
    )
    session_b = _seed_unknown_session(
        repository,
        run_id="STOCKIFY_B",
        output_xml=review_b,
        clip_location=_capitol_hill_gps(),
        capture_date="2026-03-01",
    )

    report = LocationRecoveryService(repository, _catalog_resolver(repository)).run(
        run_id=None,
        dry_run=False,
        rewrite_review_xml=True,
        report_path=tmp_path / "multi-report.json",
    )

    assert report.stockify_runs_scanned == 2
    assert set(report.stockify_run_ids) == {"STOCKIFY_A", "STOCKIFY_B"}
    assert report.unknown_sessions_before == 2
    assert report.resolved_by_srt_consensus == 2
    assert report.still_unknown == 0
    assert report.clips_recovered == 4
    assert report.review_xmls_rewritten == 2
    assert set(report.rewritten_review_xmls) == {str(review_a), str(review_b)}

    assert {item["stockify_run_id"] for item in report.sessions} == {
        "STOCKIFY_A",
        "STOCKIFY_B",
    }
    assert {item["session_id"] for item in report.sessions} == {session_a, session_b}
    assert all(item["status"] == "resolved" for item in report.sessions)

    for run_id in ("STOCKIFY_A", "STOCKIFY_B"):
        sessions = repository.sessions_for_run(run_id)
        assert len(sessions) == 1
        assert sessions[0]["city"] == "Seattle"
        assert sessions[0]["run_id"] == run_id
        assert "Unknown Location" not in sessions[0]["generated_event_name"]

    for path in (review_a, review_b):
        event = ET.parse(path).getroot().find("./library/event")
        assert event is not None
        assert "Capitol Hill" in (event.get("name") or "")
        assert "Unknown Location" not in (event.get("name") or "")


def test_session_resolver_exception_does_not_abort_other_runs(tmp_path: Path):
    database = Database(tmp_path / "fault.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    review_bad = tmp_path / "review-bad.fcpxml"
    review_good = tmp_path / "review-good.fcpxml"
    _write_unknown_review_xml(review_bad, "2026-02-06")
    _write_unknown_review_xml(review_good, "2026-03-01")
    _seed_unknown_session(
        repository,
        run_id="STOCKIFY_BAD",
        output_xml=review_bad,
        clip_location=_capitol_hill_gps(),
        capture_date="2026-02-06",
    )
    _seed_unknown_session(
        repository,
        run_id="STOCKIFY_GOOD",
        output_xml=review_good,
        clip_location={
            "status": "resolved",
            "center_lat": 47.6253,
            "center_lon": -122.3377,
            "sample_count": 4,
            "radius_meters": 2.0,
            "evidence_sources": ["srt_gps"],
        },
        capture_date="2026-03-01",
    )

    catalog = CatalogLocationResolver.from_json(default_places_path())
    resolver = ExplodingResolver(catalog, boom_lat=47.6231)
    report = LocationRecoveryService(repository, resolver).run(
        run_id=None,
        dry_run=False,
        rewrite_review_xml=True,
        report_path=None,
    )

    assert report.stockify_runs_scanned == 2
    assert report.unknown_sessions_before == 2
    assert report.resolved_by_srt_consensus == 1
    assert report.still_unknown == 1
    assert report.clips_recovered == 2
    assert any(item["status"] == "error" for item in report.sessions)
    assert any("recovery failed" in warning for warning in report.warnings)

    bad = repository.sessions_for_run("STOCKIFY_BAD")[0]
    good = repository.sessions_for_run("STOCKIFY_GOOD")[0]
    assert bad["city"] is None
    assert "Unknown Location" in bad["generated_event_name"]
    assert good["city"] == "Seattle"
    assert "Unknown Location" not in good["generated_event_name"]

    good_event = ET.parse(review_good).getroot().find("./library/event")
    assert good_event is not None
    assert "Unknown Location" not in (good_event.get("name") or "")


def test_unresolvable_gps_stays_unknown_without_abort(tmp_path: Path):
    database = Database(tmp_path / "ocean.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    _seed_unknown_session(repository, clip_location=_ocean_gps())
    report = LocationRecoveryService(repository, _catalog_resolver(repository)).run(
        run_id=None,
        dry_run=False,
        rewrite_review_xml=False,
        report_path=None,
    )
    assert report.resolved_by_srt_consensus == 0
    assert report.still_unknown == 1
    assert report.sessions[0]["reason"] == "gps_unresolved_no_city"


def test_name_hints_alone_do_not_invent_location(tmp_path: Path):
    database = Database(tmp_path / "names.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    _seed_unknown_session(repository, clip_location={"status": "unknown"})
    with database.transaction() as connection:
        connection.execute(
            "UPDATE source_events SET source_name=? WHERE id=?",
            ("Capitol Hill Morning", "EVT_STOCKIFY_LOC"),
        )
        connection.execute(
            "UPDATE source_projects SET source_name=? WHERE id=?",
            ("Seattle Walk", "PROJ_STOCKIFY_LOC"),
        )

    report = LocationRecoveryService(repository, _catalog_resolver(repository)).run(
        run_id="STOCKIFY_LOC",
        dry_run=False,
        rewrite_review_xml=False,
        report_path=None,
    )
    assert report.resolved_by_srt_consensus == 0
    assert report.still_unknown == 1
    session = repository.sessions_for_run("STOCKIFY_LOC")[0]
    assert session["city"] is None
    assert "Unknown Location" in session["generated_event_name"]


def test_rewrite_review_xml_names_updates_titles(tmp_path: Path):
    path = tmp_path / "names.fcpxml"
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.12">
  <library>
    <event name="Unknown Location — 2026-02-06">
      <project name="Unknown Location Morning — Clip 01"/>
      <project name="Unknown Location Morning — Graded 1 — Clip 02"/>
    </event>
  </library>
</fcpxml>
""",
        encoding="utf-8",
    )
    changed = rewrite_review_xml_names(
        path,
        event_renames={"Unknown Location — 2026-02-06": "Capitol Hill, Seattle — 2026-02-06"},
        project_renames={
            "Unknown Location Morning": "Capitol Hill Morning",
            "Unknown Location Morning — Clip 01": "Capitol Hill Morning — Clip 01",
        },
    )
    assert changed >= 2
    root = ET.parse(path).getroot()
    assert root.find("./library/event").get("name") == "Capitol Hill, Seattle — 2026-02-06"
    names = [node.get("name") for node in root.findall("./library/event/project")]
    assert "Capitol Hill Morning — Clip 01" in names
    assert "Capitol Hill Morning — Graded 1 — Clip 02" in names


def test_recover_locations_cli_uses_composite_resolver(tmp_path: Path, monkeypatch, capsys):
    database = Database(tmp_path / "cli.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    _seed_unknown_session(repository, clip_location=_capitol_hill_gps())
    # Keep the real CompositeLocationResolver path; only force offline catalog.
    monkeypatch.setattr(
        "vclip_pipeline.cli.build_location_resolver",
        lambda repo, **kwargs: build_location_resolver(
            repo,
            places_path=default_places_path(),
            enable_nominatim=False,
        ),
    )
    code = main(
        [
            "recover-locations",
            "--db",
            str(tmp_path / "cli.sqlite3"),
            "--location-provider",
            "catalog",
            "--quiet",
        ]
    )
    assert code == 0
    captured = capsys.readouterr().out
    assert "Stockify runs scanned:       1" in captured
    assert "Resolved by SRT consensus:   1" in captured
    assert "Clips recovered:             2" in captured
