"""Resolve exact SRT coordinates into safe public location labels."""

from __future__ import annotations

import json
import logging
import math
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .config import nominatim_user_agent
from .db.repository import CatalogRepository
from .util import stable_id

logger = logging.getLogger(__name__)

# Coarse city-only catalog rows often use large metro "search halos". Those
# radii are useful for nearby known archives, but a point on the fringe is not
# proof it lies inside that municipality. Require near-center containment for
# city-only matches so neighboring cities are not swallowed (e.g. Fremont GPS
# inside a San Jose 28 km halo).
CITY_ONLY_CONTAINMENT_FRACTION = 0.45
CITY_ONLY_CONTAINMENT_FLOOR_M = 8_000.0
CITY_ONLY_CONTAINMENT_CEILING_M = 15_000.0

# Campus / institution labels that OSM often tags as village/hamlet while the
# public shoot location should be the containing city.
NESTED_MUNICIPALITY_PARENTS: dict[tuple[str, str], tuple[str, str]] = {
    ("university of virginia", "virginia"): ("Charlottesville", "Virginia"),
    ("uva", "virginia"): ("Charlottesville", "Virginia"),
}


def haversine_meters(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Measure distance between two GPS coordinates."""
    radius_m = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * radius_m * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class LocationResolver(Protocol):
    """Minimal interface used by Stockify and location recovery."""

    def resolve(self, latitude: float, longitude: float) -> dict[str, object] | None:
        ...


def resolve_place(
    resolver: LocationResolver,
    latitude: float,
    longitude: float,
) -> dict[str, object] | None:
    """Resolve GPS through the shared LocationResolver.resolve interface."""
    return resolver.resolve(latitude, longitude)


def as_place_resolver(
    resolver: LocationResolver,
    *,
    on_error: Callable[[Exception, float, float], None] | None = None,
) -> Callable[[float, float], dict[str, object] | None]:
    """Adapt LocationResolver.resolve for call sites that still take a callable."""

    def _resolve(latitude: float, longitude: float) -> dict[str, object] | None:
        try:
            return resolve_place(resolver, latitude, longitude)
        except Exception as exc:
            if on_error is not None:
                on_error(exc, latitude, longitude)
            return None

    return _resolve


@dataclass(frozen=True)
class CatalogPlace:
    poi: str | None
    neighborhood: str | None
    city: str | None
    state: str | None
    country: str | None
    lat: float
    lon: float
    radius_m: float
    priority: int
    timezone: str | None
    aliases: tuple[str, ...]

    def as_location(self) -> dict[str, object]:
        return {
            "poi": self.poi,
            "neighborhood": self.neighborhood,
            "city": self.city,
            "state": self.state,
            "country": self.country,
            "lat": self.lat,
            "lon": self.lon,
            "radius_m": self.radius_m,
            "timezone": self.timezone,
            "aliases": list(self.aliases),
            "provider": "local_catalog",
        }


class CatalogLocationResolver:
    """Fast offline matching for places that matter to the archive."""

    def __init__(self, places: list[CatalogPlace]) -> None:
        self.places = places

    @classmethod
    def from_json(cls, path: Path) -> CatalogLocationResolver:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            [
                CatalogPlace(
                    poi=item.get("poi"),
                    neighborhood=item.get("neighborhood"),
                    city=item.get("city"),
                    state=item.get("state"),
                    country=item.get("country"),
                    lat=float(item["lat"]),
                    lon=float(item["lon"]),
                    radius_m=float(item["radius_m"]),
                    priority=int(item.get("priority", 0)),
                    timezone=item.get("timezone"),
                    aliases=tuple(item.get("aliases", [])),
                )
                for item in payload
            ]
        )

    def resolve(self, latitude: float, longitude: float) -> dict[str, object] | None:
        candidates: list[tuple[int, int, float, float, CatalogPlace]] = []
        for place in self.places:
            distance = haversine_meters(latitude, longitude, place.lat, place.lon)
            if distance > place.radius_m:
                continue
            if not _catalog_place_contains(place, distance):
                continue
            specificity = _catalog_specificity(place)
            candidates.append(
                (
                    -place.priority,
                    -specificity,
                    distance / place.radius_m,
                    distance,
                    place,
                )
            )
        if not candidates:
            return None
        # Prefer specific places (POI/neighborhood) over coarse city blobs, then
        # priority, then normalized distance to the place anchor.
        highest_priority = max(-item[0] for item in candidates)
        specific = [item for item in candidates if -item[0] >= highest_priority - 10]
        _, _, _, distance, place = sorted(
            specific,
            key=lambda item: (item[1], item[2], item[3], item[0]),
        )[0]
        result = place.as_location()
        result["distance_m"] = round(distance, 2)
        result["match_confidence"] = (
            "high" if _catalog_specificity(place) >= 2 else "medium"
        )
        return result


class NominatimLocationResolver:
    """Cached reverse geocoder for locations absent from the local catalog.

    Network failures return None so Stockify can keep exact GPS and continue offline.
    """

    def __init__(
        self,
        repository: CatalogRepository,
        *,
        user_agent: str | None = None,
        minimum_interval_seconds: float = 1.05,
    ) -> None:
        resolved_agent = (user_agent or nominatim_user_agent()).strip()
        if not resolved_agent:
            raise ValueError("Nominatim requires an identifying user agent.")
        self.repository = repository
        self.user_agent = resolved_agent
        self.minimum_interval_seconds = minimum_interval_seconds
        self._last_request_at = 0.0

    def resolve(self, latitude: float, longitude: float) -> dict[str, object] | None:
        cache_key = stable_id("GEO", round(latitude, 5), round(longitude, 5))
        cached = self.repository.get_geocode_cache(cache_key)
        if cached is not None:
            response = cached["response"]
            return self._normalize(response)

        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.minimum_interval_seconds:
            time.sleep(self.minimum_interval_seconds - elapsed)

        query = urllib.parse.urlencode(
            {
                "lat": f"{latitude:.7f}",
                "lon": f"{longitude:.7f}",
                "format": "jsonv2",
                "addressdetails": "1",
                "zoom": "18",
            }
        )
        url = f"https://nominatim.openstreetmap.org/reverse?{query}"
        try:
            payload = _fetch_nominatim_json(url, user_agent=self.user_agent)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            logger.debug(
                "Nominatim unavailable for %.5f, %.5f: %s",
                latitude,
                longitude,
                exc,
            )
            return None
        if payload is None:
            return None

        self._last_request_at = time.monotonic()
        self.repository.put_geocode_cache(
            cache_key=cache_key,
            latitude=latitude,
            longitude=longitude,
            provider="nominatim",
            response=payload,
        )
        return self._normalize(payload)

    @staticmethod
    def _normalize(payload: dict[str, Any]) -> dict[str, object] | None:
        address = payload.get("address") or {}
        if not address:
            return None
        neighborhood = (
            address.get("neighbourhood")
            or address.get("suburb")
            or address.get("quarter")
            or address.get("city_district")
        )
        # Prefer true municipalities. OSM often tags campuses as village while
        # the containing city is absent or secondary.
        city = address.get("city") or address.get("town")
        place_type = "city" if city else None
        if not city and address.get("municipality"):
            city = address.get("municipality")
            place_type = "municipality"
        if not city and address.get("village"):
            city = address.get("village")
            place_type = "village"
        if not city and address.get("hamlet"):
            city = address.get("hamlet")
            place_type = "hamlet"
        county = address.get("county")
        if not city and county:
            city = county
            place_type = "county"
        state = address.get("state")
        parent = nested_municipality_parent(
            str(city) if city else None,
            str(state) if state else None,
        )
        if parent is not None:
            if city and str(city) != parent[0]:
                neighborhood = neighborhood or str(city)
            city = parent[0]
            state = parent[1]
            place_type = "city"
        poi = address.get("amenity") or address.get("tourism") or address.get("leisure")
        if not any(
            [poi, neighborhood, city, county, state, address.get("country")]
        ):
            return None
        return {
            "poi": poi,
            "neighborhood": neighborhood,
            "city": city,
            "county": county,
            "state": state,
            "country": address.get("country"),
            "timezone": None,
            "aliases": [],
            "provider": "nominatim",
            "place_type": place_type,
            "match_confidence": "high",
        }


def nested_municipality_parent(
    city: str | None,
    state: str | None,
) -> tuple[str, str] | None:
    """Return the containing city for nested campus/institution labels."""
    if not city or not state:
        return None
    return NESTED_MUNICIPALITY_PARENTS.get((city.casefold(), state.casefold()))


class CompositeLocationResolver:
    """Try cheap deterministic resolution before optional online lookup.

    High-confidence catalog matches (neighborhood/POI) win immediately.
    Medium/weak catalog city blobs yield to later resolvers (Nominatim) so the
    containing municipality can override a nearby metro halo.
    """

    def __init__(self, resolvers: list[LocationResolver]) -> None:
        self.resolvers = resolvers

    def resolve(self, latitude: float, longitude: float) -> dict[str, object] | None:
        fallback: dict[str, object] | None = None
        for resolver in self.resolvers:
            try:
                result = resolver.resolve(latitude, longitude)
            except Exception as exc:
                logger.debug(
                    "Location resolver %s failed for %.5f, %.5f: %s",
                    type(resolver).__name__,
                    latitude,
                    longitude,
                    exc,
                )
                continue
            if not result:
                continue
            confidence = str(result.get("match_confidence") or "high")
            if confidence == "high":
                return result
            if fallback is None:
                fallback = result
        return fallback


def _catalog_specificity(place: CatalogPlace) -> int:
    if place.poi:
        return 3
    if place.neighborhood:
        return 2
    return 1


def city_only_containment_radius_m(radius_m: float) -> float:
    """Max distance for a city-only catalog row to count as containing."""
    return min(
        float(radius_m),
        CITY_ONLY_CONTAINMENT_CEILING_M,
        max(CITY_ONLY_CONTAINMENT_FLOOR_M, float(radius_m) * CITY_ONLY_CONTAINMENT_FRACTION),
    )


def _catalog_place_contains(place: CatalogPlace, distance_m: float) -> bool:
    if place.poi or place.neighborhood:
        return distance_m <= place.radius_m
    return distance_m <= city_only_containment_radius_m(place.radius_m)


def default_places_path() -> Path:
    return Path(__file__).parent / "data" / "places.json"


def build_location_resolver(
    repository: CatalogRepository,
    *,
    places_path: Path | None = None,
    enable_nominatim: bool = True,
    nominatim_user_agent_override: str | None = None,
) -> CompositeLocationResolver:
    """Catalog-first resolver with transparent cached Nominatim fallback."""
    catalog = CatalogLocationResolver.from_json(places_path or default_places_path())
    resolvers: list[LocationResolver] = [catalog]
    if enable_nominatim:
        resolvers.append(
            NominatimLocationResolver(
                repository,
                user_agent=nominatim_user_agent(nominatim_user_agent_override),
            )
        )
    return CompositeLocationResolver(resolvers)


def _fetch_nominatim_json(url: str, *, user_agent: str) -> dict[str, Any] | None:
    """Fetch Nominatim JSON via urllib, with curl fallback for broken SSL stores."""
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — narrow to SSL/network; re-raise others below
        if isinstance(exc, json.JSONDecodeError):
            raise
        reason = str(getattr(exc, "reason", None) or exc)
        upper = reason.upper()
        if "CERTIFICATE" in upper or "SSL" in upper or type(exc).__name__ == "SSLError":
            logger.debug(
                "urllib SSL failed for Nominatim; trying curl fallback: %s", exc
            )
            return _curl_nominatim_json(url, user_agent=user_agent)
        if isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)):
            raise
        raise


def _curl_nominatim_json(url: str, *, user_agent: str) -> dict[str, Any] | None:
    try:
        completed = subprocess.run(
            [
                "curl",
                "-sS",
                "-L",
                "--max-time",
                "20",
                "-A",
                user_agent,
                url,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        logger.debug("curl Nominatim fallback unavailable: %s", exc)
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        logger.debug(
            "curl Nominatim fallback failed rc=%s stderr=%s",
            completed.returncode,
            (completed.stderr or "")[:200],
        )
        return None
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
