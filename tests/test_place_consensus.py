"""Regression tests for place consensus and multi-location cluster detection."""

from __future__ import annotations

from vclip_pipeline.geo import NominatimLocationResolver, nested_municipality_parent
from vclip_pipeline.stockify.flight_location import (
    FlightIdentity,
    TrajectorySample,
    resolve_flight_trajectory,
)
from vclip_pipeline.stockify.session_resegmentation import propose_session_splits


def _sample(lat: float, lon: float, source: str) -> TrajectorySample:
    return TrajectorySample(
        latitude=lat,
        longitude=lon,
        source_key=source,
        stock_clip_id=f"CLIP_{source}",
        sample_count=8,
        source="srt_full",
        filename=f"{source}.MP4",
    )


class RedmondBellevueResolver:
    """Fringe Nominatim-style labels around Overlake / Redmond."""

    def resolve(self, latitude: float, longitude: float):
        # Western fringe occasionally reverse-geocodes as Bellevue.
        if longitude <= -122.155:
            city = "Bellevue"
            neighborhood = "West Lake Sammamish"
        else:
            city = "Redmond"
            # Multiple Redmond neighborhoods → city-level consensus after majority.
            neighborhood = "Overlake" if latitude < 47.636 else "Downtown Redmond"
        return {
            "provider": "test",
            "country": "United States",
            "state": "Washington",
            "city": city,
            "neighborhood": neighborhood,
            "poi": None,
            "timezone": "America/Los_Angeles",
            "place_type": "city",
        }


class UvaCampusResolver:
    """OSM village campus label vs containing Charlottesville city."""

    def resolve(self, latitude: float, longitude: float):
        # Grounds / campus samples (slightly north of downtown).
        if latitude >= 38.032:
            return {
                "provider": "test",
                "country": "United States",
                "state": "Virginia",
                "city": "University of Virginia",
                "neighborhood": None,
                "poi": None,
                "timezone": "America/New_York",
                "place_type": "village",
            }
        return {
            "provider": "test",
            "country": "United States",
            "state": "Virginia",
            "city": "Charlottesville",
            "neighborhood": None,
            "poi": None,
            "timezone": "America/New_York",
            "place_type": "city",
        }


class TroutvilleCountyResolver:
    """County-only reverse geocode with no city field (Botetourt / Troutville)."""

    def resolve(self, latitude: float, longitude: float):
        return {
            "provider": "test",
            "country": "United States",
            "state": "Virginia",
            "city": None,
            "county": "Botetourt County",
            "neighborhood": None,
            "poi": None,
            "timezone": "America/New_York",
            "place_type": "county",
        }


class MixedVirginiaResolver:
    """Charlottesville campus + distant Blacksburg cluster (UVA 1 style)."""

    def resolve(self, latitude: float, longitude: float):
        if latitude < 37.5:
            return {
                "provider": "test",
                "country": "United States",
                "state": "Virginia",
                "city": "Blacksburg",
                "neighborhood": None,
                "poi": None,
                "timezone": "America/New_York",
            }
        if latitude >= 38.032:
            return {
                "provider": "test",
                "country": "United States",
                "state": "Virginia",
                "city": "University of Virginia",
                "neighborhood": None,
                "poi": None,
                "timezone": "America/New_York",
            }
        return {
            "provider": "test",
            "country": "United States",
            "state": "Virginia",
            "city": "Charlottesville",
            "neighborhood": None,
            "poi": None,
            "timezone": "America/New_York",
        }


def test_redmond_majority_absorbs_bellevue_fringe():
    # ~19 Overlake/Redmond vs ~3 Bellevue fringe votes.
    samples = [
        *[
            _sample(47.6340 + (i % 5) * 0.0010, -122.1400, f"red_{i}")
            for i in range(19)
        ],
        *[_sample(47.6316, -122.1600 - i * 0.0001, f"bel_{i}") for i in range(3)],
    ]
    result = resolve_flight_trajectory(
        samples,
        RedmondBellevueResolver(),
        identity=FlightIdentity(run_id="R", session_id="SESS_REDMOND"),
    )
    assert result.status == "resolved"
    assert result.coherence == "city"
    assert result.consensus_method == "majority"
    assert result.location is not None
    assert result.location["city"] == "Redmond"
    assert result.location["state"] == "Washington"
    assert "Redmond" in str(result.location["public_label"])
    assert len(result.geo_clusters) == 1


def test_uva_nested_campus_resolves_to_charlottesville():
    samples = [
        _sample(38.0335, -78.5079, "grounds_a"),
        _sample(38.0340, -78.5085, "grounds_b"),
        _sample(38.0290, -78.4780, "downtown"),
        _sample(38.0285, -78.4770, "downtown_b"),
    ]
    result = resolve_flight_trajectory(samples, UvaCampusResolver())
    assert result.status == "resolved"
    assert result.coherence == "city"
    assert result.consensus_method == "compatible_nested"
    assert result.location is not None
    assert result.location["city"] == "Charlottesville"
    assert result.location["state"] == "Virginia"


def test_troutville_county_fallback_when_city_absent():
    samples = [
        _sample(37.4580, -79.8400, "trout_a"),
        _sample(37.4585, -79.8390, "trout_b"),
        _sample(37.4575, -79.8410, "trout_c"),
    ]
    result = resolve_flight_trajectory(samples, TroutvilleCountyResolver())
    assert result.status == "resolved"
    assert result.coherence == "county"
    assert result.location is not None
    assert result.location["city"] == "Botetourt County"
    assert result.location["state"] == "Virginia"
    assert "Botetourt" in str(result.location["public_label"])


def test_uva1_distant_clusters_mark_multi_location_conflict():
    samples = [
        _sample(38.0335, -78.5079, "cville_a"),
        _sample(38.0290, -78.4780, "cville_b"),
        _sample(37.2295, -80.4139, "blacksburg_a"),
        _sample(37.2300, -80.4145, "blacksburg_b"),
    ]
    result = resolve_flight_trajectory(
        samples,
        MixedVirginiaResolver(),
        identity=FlightIdentity(run_id="R", session_id="SESS_UVA1"),
    )
    assert result.status == "multi_location"
    assert result.coherence == "multi_location"
    assert result.location is None
    assert len(result.geo_clusters) == 2
    diagnostics = result.diagnostics()
    assert diagnostics["geo_clusters"]
    source_totals = sum(item["source_count"] for item in diagnostics["geo_clusters"])
    assert source_totals == 4


def test_equal_adjacent_cities_still_conflict_without_majority():
    samples = [
        _sample(47.6740, -122.1215, "red_a"),
        _sample(47.6741, -122.1210, "red_b"),
        _sample(47.6100, -122.2010, "bel_a"),
        _sample(47.6101, -122.2005, "bel_b"),
    ]
    result = resolve_flight_trajectory(samples, RedmondBellevueResolver())
    assert result.status == "conflict"
    assert result.coherence == "conflict"
    assert result.location is None
    assert len(result.geo_clusters) == 1


def test_nominatim_prefers_city_and_maps_uva_campus():
    payload = {
        "address": {
            "village": "University of Virginia",
            "county": "Albemarle County",
            "state": "Virginia",
            "country": "United States",
        }
    }
    place = NominatimLocationResolver._normalize(payload)
    assert place is not None
    assert place["city"] == "Charlottesville"
    assert place["state"] == "Virginia"
    assert place["place_type"] == "city"
    assert place.get("neighborhood") == "University of Virginia"


def test_nominatim_county_fallback_without_city():
    payload = {
        "address": {
            "county": "Botetourt County",
            "state": "Virginia",
            "country": "United States",
        }
    }
    place = NominatimLocationResolver._normalize(payload)
    assert place is not None
    assert place["city"] == "Botetourt County"
    assert place["place_type"] == "county"
    assert place["county"] == "Botetourt County"


def test_nested_municipality_parent_helper():
    assert nested_municipality_parent("University of Virginia", "Virginia") == (
        "Charlottesville",
        "Virginia",
    )
    assert nested_municipality_parent("Charlottesville", "Virginia") is None


def test_session_resegmentation_proposal_preserves_project():
    points = [
        ("MEDIA_CVILLE_A", 38.0335, -78.5079),
        ("MEDIA_CVILLE_B", 38.0290, -78.4780),
        ("MEDIA_BURG_A", 37.2295, -80.4139),
        ("MEDIA_BURG_B", 37.2300, -80.4145),
    ]
    proposal = propose_session_splits(
        run_id="STOCKIFY_UVA1",
        parent_session_id="SESS_UVA1",
        original_project_id="PROJ_UVA1",
        source_points=points,
        location_resolver=MixedVirginiaResolver(),
    )
    assert proposal.status == "proposed"
    assert proposal.original_project_id == "PROJ_UVA1"
    assert len(proposal.clusters) == 2
    assert all(item["preserves_project_relationship"] for item in proposal.clusters)
    assert all(
        item["proposed_session_key"].startswith("geo:SESS_UVA1:cluster:")
        for item in proposal.clusters
    )
    suggested = {item.get("suggested_location") for item in proposal.clusters}
    assert any(label and "Charlottesville" in label for label in suggested)
    assert any(label and "Blacksburg" in label for label in suggested)
