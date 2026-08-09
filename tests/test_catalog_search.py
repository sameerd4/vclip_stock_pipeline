from __future__ import annotations

from vclip_pipeline.workflow.search import CatalogSearch


def _row(
    clip_id: str,
    *,
    caption: str = "",
    tags: list[dict] | None = None,
    named_subjects: list[dict] | None = None,
    markets: list[dict] | None = None,
    city: str = "San Francisco",
) -> dict:
    return {
        "stockify_run_id": "RUN",
        "stock_clip_id": clip_id,
        "caption": caption,
        "tags": tags or [],
        "named_subjects": named_subjects or [],
        "markets": markets or [{"market_id": "san-francisco", "market_label": "San Francisco"}],
        "city": city,
        "public_label": city,
        "exported_path": f"/tmp/{clip_id}.mp4",
    }


def test_query_aliases_roads_and_buildings_normalize_to_tags():
    search = CatalogSearch()
    roads = search.parse_query("roads")
    assert len(roads.concepts) == 1
    assert roads.concepts[0].kind == "tag"
    assert roads.concepts[0].value == "road"

    buildings = search.parse_query("buildings")
    assert buildings.concepts[0].value == "architecture"

    marinas = search.parse_query("marinas")
    assert marinas.concepts[0].value == "waterfront"


def test_taxonomy_labels_resolve_like_ids():
    search = CatalogSearch()
    by_label = search.parse_query("Roads")
    by_id = search.parse_query("road")
    assert by_label.concepts[0].value == "road"
    assert by_id.concepts[0].value == "road"
    golden = search.parse_query("Golden Hour")
    assert golden.concepts[0].value == "golden_hour"


def test_road_and_roads_return_same_strong_results():
    search = CatalogSearch()
    road_clip = _row(
        "ROAD_PRIMARY",
        caption="Aerial follow along an urban arterial.",
        tags=[{"tag": "road", "strength": "primary", "tag_group": "subject"}],
    )
    water_clip = _row(
        "WATER_ONLY",
        caption="Quiet marina morning.",
        tags=[{"tag": "waterfront", "strength": "primary", "tag_group": "subject"}],
    )
    rows = [road_clip, water_clip]
    road_hits = search.rank(rows, "road")
    roads_hits = search.rank(rows, "roads")
    assert [row["stock_clip_id"] for row in road_hits] == ["ROAD_PRIMARY"]
    assert [row["stock_clip_id"] for row in roads_hits] == ["ROAD_PRIMARY"]
    assert road_hits[0]["search_score"] == roads_hits[0]["search_score"]
    assert road_hits[0]["search_score"] >= 40.0


def test_palace_of_fine_arts_ranks_canonical_and_suppresses_incidental():
    search = CatalogSearch()
    palace = _row(
        "PALACE",
        caption="Morning light on the Palace of Fine Arts rotunda.",
        tags=[
            {"tag": "architecture", "strength": "primary", "tag_group": "subject"},
            {"tag": "establishing", "strength": "secondary", "tag_group": "use"},
        ],
        named_subjects=[
            {
                "subject": "Palace of Fine Arts",
                "raw_name": "Palace of Fine Arts",
                "canonical_entity_id": "ENTITY_PALACE_OF_FINE_ARTS",
                "canonical_label": "Palace of Fine Arts",
            }
        ],
    )
    incidental = _row(
        "INCIDENTAL",
        caption="A fine arts festival banner beside palace-like apartments.",
        tags=[
            {"tag": "city_urban", "strength": "primary", "tag_group": "scene"},
            {"tag": "establishing", "strength": "primary", "tag_group": "use"},
        ],
    )
    results = search.rank([incidental, palace], "Palace of Fine Arts", explain=True)
    assert results
    assert results[0]["stock_clip_id"] == "PALACE"
    assert all(row["stock_clip_id"] != "INCIDENTAL" for row in results)
    kinds = {item["kind"] for item in results[0]["search_explain"]["contributions"]}
    assert "exact_canonical_entity" in kinds or "canonical_entity_alias" in kinds


def test_golden_gate_bridge_prioritizes_canonical_entity():
    search = CatalogSearch()
    golden = _row(
        "GGB",
        caption="Telephoto compression of the Golden Gate Bridge.",
        tags=[{"tag": "bridge", "strength": "primary", "tag_group": "subject"}],
        named_subjects=[
            {
                "subject": "Golden Gate Bridge",
                "raw_name": "Golden Gate Bridge",
                "canonical_entity_id": "ENTITY_GOLDEN_GATE_BRIDGE",
                "canonical_label": "Golden Gate Bridge",
            }
        ],
    )
    bay = _row(
        "BAY",
        caption="Wide establishing of the Bay Bridge span.",
        tags=[{"tag": "bridge", "strength": "primary", "tag_group": "subject"}],
        named_subjects=[
            {
                "subject": "Bay Bridge",
                "raw_name": "Bay Bridge",
                "canonical_entity_id": "ENTITY_BAY_BRIDGE",
                "canonical_label": "Bay Bridge",
            }
        ],
    )
    generic_bridge = _row(
        "BRIDGE",
        caption="An unnamed overpass above the freeway.",
        tags=[{"tag": "bridge", "strength": "primary", "tag_group": "subject"}],
    )
    results = search.rank(
        [generic_bridge, bay, golden],
        "Golden Gate Bridge",
        explain=True,
    )
    assert results[0]["stock_clip_id"] == "GGB"
    assert results[0]["search_score"] > 80
    # Entity-centric query should not surface unrelated bridge clips.
    assert [row["stock_clip_id"] for row in results] == ["GGB"]


def test_bridge_tag_query_keeps_strong_bridge_behavior():
    search = CatalogSearch()
    primary = _row(
        "BRIDGE_PRIMARY",
        caption="Approach along the bridge deck.",
        tags=[{"tag": "bridge", "strength": "primary", "tag_group": "subject"}],
    )
    secondary = _row(
        "BRIDGE_SECONDARY",
        caption="Skyline with a bridge in the distance.",
        tags=[
            {"tag": "skyline", "strength": "primary", "tag_group": "subject"},
            {"tag": "bridge", "strength": "secondary", "tag_group": "subject"},
        ],
    )
    road = _row(
        "ROAD",
        caption="Highway curves without a bridge.",
        tags=[{"tag": "road", "strength": "primary", "tag_group": "subject"}],
    )
    results = search.rank([road, secondary, primary], "bridge", explain=True)
    ids = [row["stock_clip_id"] for row in results]
    assert ids[0] == "BRIDGE_PRIMARY"
    assert "BRIDGE_SECONDARY" in ids
    assert "ROAD" not in ids
    assert results[0]["search_score"] > results[1]["search_score"]


def test_multi_term_query_rewards_complete_concept_coverage():
    search = CatalogSearch()
    complete = _row(
        "COMPLETE",
        caption="Golden hour waterfront road in Mission Bay.",
        tags=[
            {"tag": "road", "strength": "primary", "tag_group": "subject"},
            {"tag": "waterfront", "strength": "secondary", "tag_group": "subject"},
            {"tag": "golden_hour", "strength": "secondary", "tag_group": "style"},
        ],
    )
    partial = _row(
        "PARTIAL",
        caption="Golden hour downtown towers.",
        tags=[
            {"tag": "golden_hour", "strength": "primary", "tag_group": "style"},
            {"tag": "architecture", "strength": "secondary", "tag_group": "subject"},
        ],
    )
    results = search.rank(
        [partial, complete],
        "road waterfront golden hour",
        explain=True,
    )
    assert results[0]["stock_clip_id"] == "COMPLETE"
    assert results[0]["search_score"] > results[1]["search_score"]
    assert any(
        item["kind"] == "multi_concept_all"
        for item in results[0]["search_explain"]["contributions"]
    )


def test_explain_includes_parsed_concepts_and_contributions():
    search = CatalogSearch()
    row = _row(
        "WF",
        caption="Marina edge at dusk.",
        tags=[{"tag": "waterfront", "strength": "primary", "tag_group": "subject"}],
    )
    results = search.rank([row], "waterfronts", explain=True)
    assert results[0]["search_explain"]["parsed_concepts"][0]["value"] == "waterfront"
    assert results[0]["search_explain"]["contributions"]
    assert results[0]["search_explain"]["score"] == results[0]["search_score"]


def test_salesforce_tower_alias_query_matches_canonical_clip():
    search = CatalogSearch()
    clip = _row(
        "SALESFORCE",
        caption="Downtown towers including Salesforce Tower.",
        named_subjects=[
            {
                "subject": "Salesforce Tower (San Francisco)",
                "raw_name": "Salesforce Tower (San Francisco)",
                "canonical_entity_id": "ENTITY_SALESFORCE_TOWER",
                "canonical_label": "Salesforce Tower",
            }
        ],
        tags=[{"tag": "architecture", "strength": "primary", "tag_group": "subject"}],
    )
    other = _row(
        "OTHER",
        caption="Generic downtown architecture.",
        tags=[{"tag": "architecture", "strength": "primary", "tag_group": "subject"}],
    )
    results = search.rank(
        [other, clip],
        "Salesforce Tower (San Francisco)",
        explain=True,
    )
    assert results[0]["stock_clip_id"] == "SALESFORCE"
    kinds = {item["kind"] for item in results[0]["search_explain"]["contributions"]}
    assert "canonical_entity_alias" in kinds or "exact_canonical_entity" in kinds
