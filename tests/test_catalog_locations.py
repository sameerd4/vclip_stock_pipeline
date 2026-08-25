from __future__ import annotations

import json
from pathlib import Path

from vclip_pipeline.db import Database
from vclip_pipeline.util import json_dumps, stable_id
from vclip_pipeline.workflow.catalog import WorkflowCatalog
from vclip_pipeline.workflow.cli import build_parser, main
from vclip_pipeline.workflow.models import NamedSubject, VisualAnalysis, VisualTag


class InventoryHarness:
    def __init__(self, tmp_path: Path) -> None:
        self.database = Database(tmp_path / "inventory.sqlite3")
        self.database.migrate()
        self.catalog = WorkflowCatalog(self.database)
        self.run_id = "RUN_INV"
        self._index = 0
        self._ensure_run()

    def _ensure_run(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO stockify_runs (
                    id, source_xml_path, source_xml_sha256, output_xml_path, report_path,
                    pipeline_version, status, options_json, started_at, completed_at
                ) VALUES (?, 'a.xml', 'h', 'out.fcpxml', 'r.json', '0.1.0', 'complete',
                          '{}', 't', 't')
                """,
                (self.run_id,),
            )
            connection.execute(
                """
                INSERT INTO source_events (id, run_id, source_index, source_name, source_uid)
                VALUES (?, ?, 0, 'Event', NULL)
                """,
                (f"EVT_{self.run_id}", self.run_id),
            )

    def add_clip(
        self,
        clip_id: str,
        *,
        city: str | None = None,
        neighborhood: str | None = None,
        session_city: str | None = None,
        session_neighborhood: str | None = None,
        duration_seconds: float = 10.0,
        size_bytes: int | None = 1_000_000,
        width: int | None = 1080,
        height: int | None = 1920,
        review_status: str = "approved",
        export_status: str = "matched",
        eligibility_status: str = "accepted",
        exported: bool = True,
        location: dict | None = None,
    ) -> str:
        self._index += 1
        index = self._index
        project_id = f"PROJ_{clip_id}"
        session_id = f"SESS_{clip_id}"
        if location is None:
            location = {}
            if city:
                location["city"] = city
            if neighborhood:
                location["neighborhood"] = neighborhood
            if city or neighborhood:
                location["public_label"] = (
                    f"{neighborhood}, {city}" if neighborhood and city else (city or neighborhood)
                )
        session_city = city if session_city is None else session_city
        session_neighborhood = (
            neighborhood if session_neighborhood is None else session_neighborhood
        )
        with self.database.transaction() as connection:
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
                    ?, ?, ?, '2026-08-01', '2026-08-01T18:00:00', 'America/Los_Angeles',
                    NULL, NULL, NULL, 'United States', 'California', ?, ?, NULL, ?,
                    'high', 'evening', 'medium', ?, ?, ?, 'not_enriched', ?, '{}', 't', 't'
                )
                """,
                (
                    session_id,
                    self.run_id,
                    f"session-{clip_id}",
                    session_city,
                    session_neighborhood,
                    session_city or "Unknown Location",
                    session_city or "Unknown Location",
                    session_city or "Unknown Location",
                    clip_id,
                    json_dumps({"city": session_city, "neighborhood": session_neighborhood}),
                ),
            )
            connection.execute(
                """
                INSERT INTO source_projects (
                    id, run_id, source_event_id, source_index, source_name, source_uid,
                    classification, session_id, accepted_clip_count, skipped_clip_count,
                    generated_event_name, generated_project_label, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, 'accepted', ?, 1, 0, ?, ?, 't', 't')
                """,
                (
                    project_id,
                    self.run_id,
                    f"EVT_{self.run_id}",
                    index,
                    clip_id,
                    session_id,
                    session_city or "Unknown Location",
                    session_city or "Unknown Location",
                ),
            )
            connection.execute(
                """
                INSERT INTO stock_candidates (
                    run_id, stock_clip_id, source_project_id, session_id,
                    source_segment_index, source_name, eligibility_status,
                    original_start, original_duration, original_duration_seconds,
                    proposed_start, proposed_duration, proposed_duration_seconds,
                    final_duration_seconds, review_status, srt_reasons_json,
                    visual_reasons_json, visual_metrics_json, location_json,
                    capture_time_json, time_of_day_json, weather_json,
                    creative_effects_json, generated_event_name, generated_project_label,
                    generated_clip_project_name, clip_sequence, export_status,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, 0, ?, ?,
                    '0s', '10s', 10.0, '0s', '10s', 10.0, 10.0, ?, '[]',
                    '[]', '{}', ?, '{}', '{}', '{}', '[]', ?, ?, ?, 1, ?, 't', 't'
                )
                """,
                (
                    self.run_id,
                    clip_id,
                    project_id,
                    session_id,
                    clip_id,
                    eligibility_status,
                    review_status,
                    json_dumps(location),
                    session_city or "Unknown Location",
                    session_city or "Unknown Location",
                    clip_id,
                    export_status,
                ),
            )
            export_id = stable_id("EXPORT", self.run_id, clip_id)
            if exported:
                connection.execute(
                    """
                    INSERT INTO exports (
                        id, stockify_run_id, stock_clip_id, exported_filename,
                        exported_path, match_method, match_confidence, file_size_bytes,
                        duration_seconds, sha256, reconciled_at
                    ) VALUES (?, ?, ?, ?, ?, 'basename', 'high', ?, ?, 'abc', 't')
                    """,
                    (
                        export_id,
                        self.run_id,
                        clip_id,
                        f"{clip_id}.mp4",
                        f"/missing/exports/{clip_id}.mp4",
                        size_bytes,
                        duration_seconds,
                    ),
                )
                if width is not None and height is not None:
                    connection.execute(
                        """
                        INSERT INTO export_media_metadata (
                            export_id, width, height, codec_name, frame_rate,
                            probe_json, probed_at
                        ) VALUES (?, ?, ?, 'h264', 30.0, '{}', 't')
                        """,
                        (export_id, width, height),
                    )
        return export_id

    def add_tags_and_subjects(self, clip_id: str, export_id: str) -> None:
        self.catalog.upsert_visual_analysis(
            analysis_key=f"ANALYSIS_{clip_id}",
            analysis_run_id="VISUAL_RUN",
            stockify_run_id=self.run_id,
            stock_clip_id=clip_id,
            export_id=export_id,
            export_sha256="abc",
            provider="test",
            model="test",
            taxonomy_version=1,
            analysis=VisualAnalysis(
                caption="Downtown aerial.",
                tags=(
                    VisualTag("subject", "architecture", "primary", 0.9),
                    VisualTag("subject", "waterfront", "secondary", 0.8),
                    VisualTag("use", "establishing", "primary", 0.7),
                ),
                named_subjects=(
                    NamedSubject(name="Salesforce Tower"),
                    NamedSubject(name="Ferry Building"),
                ),
            ),
            evidence={},
        )

    def add_to_collections(self, clip_id: str, export_id: str, slugs: tuple[str, ...]) -> None:
        for slug in slugs:
            self.catalog.save_collection_definition(
                slug=slug,
                title=slug,
                description=None,
                rule={"markets": ["san-francisco"]},
            )
            self.catalog.publish_collection_version(
                collection_id=stable_id("COLLECTION", slug),
                rule={"markets": ["san-francisco"]},
                metadata={"title": slug},
                clips=[
                    {
                        "stockify_run_id": self.run_id,
                        "stock_clip_id": clip_id,
                        "export_id": export_id,
                        "score": 1.0,
                        "rationale": {},
                    }
                ],
            )


def _seed_standard(tmp_path: Path) -> InventoryHarness:
    harness = InventoryHarness(tmp_path)
    rincon = harness.add_clip(
        "SF_RINCON",
        city="San Francisco",
        neighborhood="Rincon Hill",
        duration_seconds=70.0,
        size_bytes=4_000_000_000,
        width=1080,
        height=1920,
    )
    harness.add_clip(
        "SF_MARINA",
        city="San Francisco",
        neighborhood="Marina District",
        duration_seconds=50.0,
        size_bytes=2_500_000_000,
        width=1080,
        height=1920,
    )
    harness.add_clip(
        "SF_SOMA_LANDSCAPE",
        city="San Francisco",
        neighborhood="South of Market",
        duration_seconds=42.0,
        size_bytes=1_800_000_000,
        width=1920,
        height=1080,
    )
    seattle = harness.add_clip(
        "SEA_SLU",
        city="Seattle",
        neighborhood="South Lake Union",
        duration_seconds=91.0,
        size_bytes=5_900_000_000,
        width=1080,
        height=1920,
    )
    unknown = harness.add_clip(
        "UNKNOWN_1",
        city=None,
        neighborhood=None,
        duration_seconds=62.0,
        size_bytes=410_200_000,
        width=1080,
        height=1920,
        location={"public_label": "Unknown Location", "city": ""},
    )
    harness.catalog.upsert_market(
        run_id=harness.run_id,
        clip_id="SF_RINCON",
        market_id="san-francisco",
        market_label="San Francisco",
    )
    harness.catalog.upsert_market(
        run_id=harness.run_id,
        clip_id="SF_MARINA",
        market_id="san-francisco",
        market_label="San Francisco",
    )
    harness.catalog.upsert_market(
        run_id=harness.run_id,
        clip_id="SF_SOMA_LANDSCAPE",
        market_id="san-francisco",
        market_label="San Francisco",
    )
    harness.catalog.upsert_market(
        run_id=harness.run_id,
        clip_id="SEA_SLU",
        market_id="seattle",
        market_label="Seattle",
    )
    # Dual-market clip: counted in both market groups, once in unique totals.
    harness.catalog.upsert_market(
        run_id=harness.run_id,
        clip_id="SF_RINCON",
        market_id="bay-area",
        market_label="Bay Area",
    )
    harness.add_tags_and_subjects("SF_RINCON", rincon)
    harness.add_to_collections("SF_RINCON", rincon, ("sf-set-a", "sf-set-b"))
    harness.add_tags_and_subjects("UNKNOWN_1", unknown)
    harness.add_tags_and_subjects("SEA_SLU", seattle)
    # These must not appear in the inventory.
    harness.add_clip(
        "PENDING",
        city="San Francisco",
        neighborhood="Mission Bay",
        review_status="pending",
    )
    harness.add_clip(
        "UNEXPORTED",
        city="San Francisco",
        neighborhood="Mission Bay",
        exported=False,
        export_status="pending",
    )
    return harness


def test_city_aggregation_and_duration_sum(tmp_path: Path):
    harness = _seed_standard(tmp_path)
    report = harness.catalog.location_inventory(group_by="city")
    keys = [item["key"] for item in report["groups"]]
    assert keys == ["San Francisco", "Seattle", "(unknown)"]
    by_key = {item["key"]: item for item in report["groups"]}
    assert by_key["San Francisco"]["clip_count"] == 3
    assert by_key["San Francisco"]["total_duration_seconds"] == 162.0
    assert by_key["San Francisco"]["vertical_clip_count"] == 2
    assert by_key["San Francisco"]["horizontal_clip_count"] == 1
    assert by_key["Seattle"]["clip_count"] == 1
    assert by_key["Seattle"]["total_duration_seconds"] == 91.0
    assert by_key["(unknown)"]["clip_count"] == 1
    assert by_key["(unknown)"]["total_duration_seconds"] == 62.0
    assert report["totals"]["clip_count"] == 5
    assert report["totals"]["located_clip_count"] == 4
    assert report["totals"]["unlocated_clip_count"] == 1
    assert report["totals"]["total_duration_seconds"] == 315.0
    assert report["totals"]["total_size_bytes"] == (
        4_000_000_000 + 2_500_000_000 + 1_800_000_000 + 5_900_000_000 + 410_200_000
    )


def test_neighborhood_aggregation_and_city_filter(tmp_path: Path):
    harness = _seed_standard(tmp_path)
    all_neighborhoods = harness.catalog.location_inventory(group_by="neighborhood")
    keys = [item["key"] for item in all_neighborhoods["groups"]]
    assert keys[0] == "South Lake Union"
    assert "Rincon Hill" in keys
    assert "Marina District" in keys
    assert "South of Market" in keys
    assert "(unknown)" in keys
    report = harness.catalog.location_inventory(
        group_by="neighborhood",
        city="San Francisco",
    )
    keys = [item["key"] for item in report["groups"]]
    assert keys == ["Rincon Hill", "Marina District", "South of Market"]
    by_key = {item["key"]: item for item in report["groups"]}
    assert by_key["Rincon Hill"]["clip_count"] == 1
    assert by_key["Rincon Hill"]["total_duration_seconds"] == 70.0
    assert report["totals"]["clip_count"] == 3
    assert report["totals"]["located_clip_count"] == 3
    assert report["totals"]["unlocated_clip_count"] == 0


def test_prefers_resolved_location_neighborhood_over_session(tmp_path: Path):
    harness = InventoryHarness(tmp_path)
    harness.add_clip(
        "SF_RESOLVED",
        city="San Francisco",
        neighborhood="Rincon Hill",
        session_neighborhood="South of Market",
        duration_seconds=12.0,
    )
    report = harness.catalog.location_inventory(
        group_by="neighborhood",
        city="San Francisco",
    )
    assert [item["key"] for item in report["groups"]] == ["Rincon Hill"]


def test_orientation_filter(tmp_path: Path):
    harness = _seed_standard(tmp_path)
    vertical = harness.catalog.location_inventory(
        group_by="neighborhood",
        city="San Francisco",
        orientation="vertical",
    )
    assert [item["key"] for item in vertical["groups"]] == ["Rincon Hill", "Marina District"]
    assert vertical["totals"]["clip_count"] == 2
    horizontal = harness.catalog.location_inventory(
        group_by="neighborhood",
        city="San Francisco",
        orientation="horizontal",
    )
    assert [item["key"] for item in horizontal["groups"]] == ["South of Market"]
    assert horizontal["totals"]["clip_count"] == 1
    assert horizontal["groups"][0]["horizontal_clip_count"] == 1
    assert horizontal["groups"][0]["vertical_clip_count"] == 0


def test_unknown_locations_are_not_discarded(tmp_path: Path):
    harness = _seed_standard(tmp_path)
    report = harness.catalog.location_inventory(group_by="city")
    unknown = next(item for item in report["groups"] if item["key"] == "(unknown)")
    assert unknown["clip_count"] == 1
    assert report["totals"]["unlocated_clip_count"] == 1


def test_tags_named_subjects_and_collections_do_not_multiply_rows(tmp_path: Path):
    harness = _seed_standard(tmp_path)
    report = harness.catalog.location_inventory(group_by="city")
    by_key = {item["key"]: item for item in report["groups"]}
    assert by_key["San Francisco"]["clip_count"] == 3
    search_rows = harness.catalog.catalog_rows()
    rincon = next(row for row in search_rows if row["stock_clip_id"] == "SF_RINCON")
    assert len(rincon["tags"]) >= 3
    assert len(rincon["named_subjects"]) >= 2


def test_market_grouping_is_many_to_many_with_unique_totals(tmp_path: Path):
    harness = _seed_standard(tmp_path)
    report = harness.catalog.location_inventory(group_by="market")
    by_key = {item["key"]: item for item in report["groups"]}
    assert by_key["San Francisco"]["clip_count"] == 3
    assert by_key["Bay Area"]["clip_count"] == 1
    assert by_key["Seattle"]["clip_count"] == 1
    assert by_key["(unknown)"]["clip_count"] == 1
    group_sum = sum(item["clip_count"] for item in report["groups"])
    assert group_sum == 6
    assert report["totals"]["clip_count"] == 5
    filtered = harness.catalog.location_inventory(group_by="market", market="San Francisco")
    assert [item["key"] for item in filtered["groups"]] == ["San Francisco"]
    assert filtered["totals"]["clip_count"] == 3


def test_duplicate_market_sources_do_not_double_count_same_market(tmp_path: Path):
    harness = InventoryHarness(tmp_path)
    harness.add_clip("DUAL_SOURCE", city="San Francisco", duration_seconds=8.0)
    harness.catalog.upsert_market(
        run_id=harness.run_id,
        clip_id="DUAL_SOURCE",
        market_id="san-francisco",
        market_label="San Francisco",
        source="location_rule",
    )
    harness.catalog.upsert_market(
        run_id=harness.run_id,
        clip_id="DUAL_SOURCE",
        market_id="san-francisco",
        market_label="San Francisco",
        source="manual",
    )
    report = harness.catalog.location_inventory(group_by="market")
    assert report["groups"][0]["key"] == "San Francisco"
    assert report["groups"][0]["clip_count"] == 1
    assert report["totals"]["clip_count"] == 1


def test_json_shape(tmp_path: Path):
    harness = _seed_standard(tmp_path)
    report = harness.catalog.location_inventory(group_by="city")
    assert set(report) == {"group_by", "filters", "totals", "groups"}
    assert report["group_by"] == "city"
    assert report["filters"] == {
        "city": None,
        "neighborhood": None,
        "market": None,
        "orientation": None,
    }
    assert set(report["totals"]) == {
        "clip_count",
        "total_duration_seconds",
        "total_size_bytes",
        "located_clip_count",
        "unlocated_clip_count",
    }
    assert set(report["groups"][0]) == {
        "key",
        "clip_count",
        "total_duration_seconds",
        "total_size_bytes",
        "vertical_clip_count",
        "horizontal_clip_count",
    }


def test_empty_catalog(tmp_path: Path):
    harness = InventoryHarness(tmp_path)
    report = harness.catalog.location_inventory()
    assert report["groups"] == []
    assert report["totals"]["clip_count"] == 0
    assert report["totals"]["located_clip_count"] == 0
    assert report["totals"]["unlocated_clip_count"] == 0
    text = main_output(["catalog", "locations", "--db", str(harness.database.path)])
    assert "TOTAL" in text
    assert "0m 00s" in text


def test_export_duration_preferred_over_final_duration(tmp_path: Path):
    harness = InventoryHarness(tmp_path)
    harness.add_clip("DUR", city="Seattle", duration_seconds=7.5, size_bytes=100)
    with harness.database.transaction() as connection:
        connection.execute(
            """
            UPDATE stock_candidates
            SET final_duration_seconds=99, proposed_duration_seconds=88
            WHERE stock_clip_id='DUR'
            """
        )
    report = harness.catalog.location_inventory()
    assert report["totals"]["total_duration_seconds"] == 7.5


def test_missing_export_file_does_not_crash(tmp_path: Path):
    harness = InventoryHarness(tmp_path)
    harness.add_clip("MISSING_FILE", city="Seattle", duration_seconds=5.0)
    report = harness.catalog.location_inventory()
    assert report["totals"]["clip_count"] == 1
    assert not Path("/missing/exports/MISSING_FILE.mp4").exists()


def test_inventory_is_read_only(tmp_path: Path):
    harness = _seed_standard(tmp_path)
    before = _table_counts(harness.database)
    harness.catalog.location_inventory(group_by="neighborhood", city="San Francisco")
    assert _table_counts(harness.database) == before


def test_cli_json_and_human_output(tmp_path: Path, capsys):
    harness = _seed_standard(tmp_path)
    code = main(
        [
            "catalog",
            "locations",
            "--db",
            str(harness.database.path),
            "--city",
            "San Francisco",
            "--orientation",
            "vertical",
            "--group-by",
            "neighborhood",
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["group_by"] == "neighborhood"
    assert payload["filters"]["city"] == "San Francisco"
    assert payload["filters"]["orientation"] == "vertical"
    assert [item["key"] for item in payload["groups"]] == ["Rincon Hill", "Marina District"]

    code = main(["catalog", "locations", "--db", str(harness.database.path)])
    assert code == 0
    text = capsys.readouterr().out
    assert text.startswith("Location")
    assert "San Francisco" in text
    assert "Seattle" in text
    assert "(unknown)" in text
    assert "TOTAL" in text
    assert "Unlocated: 1" in text


def test_existing_catalog_commands_remain_functional(tmp_path: Path, capsys):
    harness = _seed_standard(tmp_path)
    parser = build_parser()
    for command in (
        ["catalog", "search", "waterfront", "--db", str(harness.database.path)],
        ["catalog", "reindex", "--db", str(harness.database.path)],
        ["catalog", "audit", "--db", str(harness.database.path), "--json"],
        ["catalog", "locations", "--db", str(harness.database.path)],
    ):
        args = parser.parse_args(command)
        assert callable(args.handler)

    reindex_code = main(["catalog", "reindex", "--db", str(harness.database.path)])
    assert reindex_code == 0
    assert "Indexed 5 exported clip(s)." in capsys.readouterr().out

    search_code = main(
        ["catalog", "search", "waterfront", "--db", str(harness.database.path)]
    )
    assert search_code == 0
    search_out = capsys.readouterr().out
    assert "SF_RINCON" in search_out

    audit_code = main(
        ["catalog", "audit", "--db", str(harness.database.path), "--json"]
    )
    assert audit_code == 0
    audit_payload = json.loads(capsys.readouterr().out)
    assert audit_payload["total_enriched_clips"] == 3


def main_output(argv: list[str]) -> str:
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert main(argv) == 0
    return buffer.getvalue()


def _table_counts(database: Database) -> dict[str, int]:
    tables = (
        "stock_candidates",
        "exports",
        "clip_markets",
        "clip_tags",
        "clip_named_subjects",
        "clip_search_documents",
        "collection_version_clips",
    )
    counts = {}
    with database.connect() as connection:
        for table in tables:
            counts[table] = int(
                connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            )
    return counts
