from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from vclip_pipeline.config import (
    DEFAULT_NOMINATIM_USER_AGENT,
    ENV_NOMINATIM_USER_AGENT,
    nominatim_user_agent,
)
from vclip_pipeline.db import CatalogRepository, Database
from vclip_pipeline.geo import (
    CatalogLocationResolver,
    CompositeLocationResolver,
    NominatimLocationResolver,
    as_place_resolver,
    build_location_resolver,
    default_places_path,
    resolve_place,
)


@pytest.fixture
def repository(tmp_path: Path) -> CatalogRepository:
    database = Database(tmp_path / "geo.sqlite3")
    database.migrate()
    return CatalogRepository(database)


def test_nominatim_user_agent_prefers_env_then_config_then_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv(ENV_NOMINATIM_USER_AGENT, raising=False)
    monkeypatch.setenv("VCLIP_CONFIG_DIR", str(tmp_path / "cfg"))
    assert nominatim_user_agent() == DEFAULT_NOMINATIM_USER_AGENT

    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"nominatim_user_agent": "VClipConfig/1.0 me@example.com"}),
        encoding="utf-8",
    )
    assert nominatim_user_agent() == "VClipConfig/1.0 me@example.com"

    monkeypatch.setenv(ENV_NOMINATIM_USER_AGENT, "VClipEnv/1.0 me@example.com")
    assert nominatim_user_agent() == "VClipEnv/1.0 me@example.com"
    assert nominatim_user_agent("CLI/1.0") == "CLI/1.0"


def test_resolve_place_and_as_place_resolver_share_resolve_api(
    repository: CatalogRepository,
):
    resolver = build_location_resolver(
        repository,
        places_path=default_places_path(),
        enable_nominatim=False,
    )
    direct = resolve_place(resolver, 47.6231, -122.3165)
    adapted = as_place_resolver(resolver)(47.6231, -122.3165)
    assert direct == adapted
    assert direct is not None
    assert direct["neighborhood"] == "Capitol Hill"


def test_city_halo_does_not_claim_neighboring_municipality():
    """Fremont GPS must not resolve to San Jose via a metro-scale halo."""
    catalog = CatalogLocationResolver.from_json(default_places_path())
    # August 2026 Mission San Jose / Fremont trajectory sample.
    place = catalog.resolve(37.538525, -121.938783)
    assert place is not None
    assert place["city"] == "Fremont"
    assert place["city"] != "San Jose"
    assert place.get("neighborhood") in {"Mission San Jose", "Central Fremont", None}


def test_san_jose_downtown_still_resolves_inside_municipality():
    catalog = CatalogLocationResolver.from_json(default_places_path())
    place = catalog.resolve(37.3352, -121.8863)
    assert place is not None
    assert place["city"] == "San Jose"


def test_composite_prefers_nominatim_over_weak_city_blob(
    repository: CatalogRepository,
    tmp_path: Path,
):
    places = tmp_path / "halo.json"
    places.write_text(
        json.dumps(
            [
                {
                    "poi": None,
                    "neighborhood": None,
                    "city": "BigCity",
                    "state": "California",
                    "country": "United States",
                    "lat": 37.3382,
                    "lon": -121.8863,
                    "radius_m": 28000,
                    "priority": 20,
                    "timezone": "America/Los_Angeles",
                    "aliases": ["bigcity"],
                }
            ]
        ),
        encoding="utf-8",
    )

    class FixedNominatim:
        def resolve(self, latitude: float, longitude: float):
            return {
                "city": "Fremont",
                "state": "California",
                "country": "United States",
                "neighborhood": "Mission San Jose",
                "poi": None,
                "timezone": None,
                "aliases": [],
                "provider": "nominatim",
                "match_confidence": "high",
            }

    resolver = CompositeLocationResolver(
        [
            CatalogLocationResolver.from_json(places),
            FixedNominatim(),
        ]
    )
    # Far enough from BigCity center that containment rejects the halo.
    result = resolver.resolve(37.538525, -121.938783)
    assert result is not None
    assert result["city"] == "Fremont"
    assert result["provider"] == "nominatim"


def test_catalog_match_wins_before_nominatim(repository: CatalogRepository):
    calls: list[tuple[float, float]] = []

    class TrackingNominatim(NominatimLocationResolver):
        def resolve(self, latitude: float, longitude: float):
            calls.append((latitude, longitude))
            return {
                "city": "ShouldNotWin",
                "provider": "nominatim",
                "neighborhood": None,
                "poi": None,
                "state": None,
                "country": None,
                "timezone": None,
                "aliases": [],
            }

    resolver = CompositeLocationResolver(
        [
            CatalogLocationResolver.from_json(default_places_path()),
            TrackingNominatim(repository, user_agent="test-agent"),
        ]
    )
    # South Lake Union catalog point
    result = resolver.resolve(47.6253, -122.3377)
    assert result is not None
    assert result["provider"] == "local_catalog"
    assert result.get("neighborhood") == "South Lake Union"
    assert calls == []


def test_nominatim_cache_used_when_catalog_misses(
    repository: CatalogRepository,
    tmp_path: Path,
):
    empty_catalog = tmp_path / "empty-places.json"
    empty_catalog.write_text("[]", encoding="utf-8")
    payload = {
        "address": {
            "city": "Reykjavik",
            "state": "Capital Region",
            "country": "Iceland",
            "suburb": "Miðborg",
        }
    }
    from vclip_pipeline.util import stable_id

    cache_key = stable_id("GEO", round(64.1466, 5), round(-21.9426, 5))
    repository.put_geocode_cache(
        cache_key=cache_key,
        latitude=64.1466,
        longitude=-21.9426,
        provider="nominatim",
        response=payload,
    )
    resolver = build_location_resolver(
        repository,
        places_path=empty_catalog,
        enable_nominatim=True,
        nominatim_user_agent_override="test-agent",
    )
    with patch("vclip_pipeline.geo.urllib.request.urlopen") as urlopen:
        result = resolver.resolve(64.1466, -21.9426)
    assert result is not None
    assert result["provider"] == "nominatim"
    assert result["city"] == "Reykjavik"
    assert result["neighborhood"] == "Miðborg"
    urlopen.assert_not_called()


def test_nominatim_network_failure_keeps_resolution_unresolved(
    repository: CatalogRepository,
    tmp_path: Path,
):
    empty_catalog = tmp_path / "empty-places.json"
    empty_catalog.write_text("[]", encoding="utf-8")
    resolver = build_location_resolver(
        repository,
        places_path=empty_catalog,
        enable_nominatim=True,
        nominatim_user_agent_override="test-agent",
    )

    with patch(
        "vclip_pipeline.geo.urllib.request.urlopen",
        side_effect=TimeoutError("offline"),
    ):
        result = resolver.resolve(1.23456, 2.34567)
    assert result is None
