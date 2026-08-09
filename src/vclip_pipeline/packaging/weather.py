"""Historical weather enrichment for search and package metadata."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ..util import stable_id, utc_now


# WMO weather interpretation codes → search-friendly normalized labels.
# https://open-meteo.com/en/docs#weathervariables
WMO_CONDITION_LABELS: dict[int, str] = {
    0: "clear",
    1: "mainly_clear",
    2: "partly_cloudy",
    3: "overcast",
    45: "fog",
    48: "fog",
    51: "drizzle",
    53: "drizzle",
    55: "drizzle",
    56: "freezing_drizzle",
    57: "freezing_drizzle",
    61: "rain",
    63: "rain",
    65: "heavy_rain",
    66: "freezing_rain",
    67: "freezing_rain",
    71: "snow",
    73: "snow",
    75: "heavy_snow",
    77: "snow",
    80: "rain_showers",
    81: "rain_showers",
    82: "heavy_rain_showers",
    85: "snow_showers",
    86: "snow_showers",
    95: "thunderstorm",
    96: "thunderstorm_hail",
    99: "thunderstorm_hail",
}

# Broader aliases derived from the normalized condition for package search tags.
_CONDITION_SEARCH_ALIASES: dict[str, tuple[str, ...]] = {
    "clear": ("sunny", "clear sky"),
    "mainly_clear": ("sunny", "clear", "mainly clear"),
    "partly_cloudy": ("partly cloudy", "cloudy"),
    "overcast": ("cloudy", "overcast"),
    "fog": ("foggy", "fog"),
    "drizzle": ("rain", "drizzle", "rainy"),
    "freezing_drizzle": ("rain", "drizzle", "freezing drizzle"),
    "rain": ("rain", "rainy"),
    "heavy_rain": ("rain", "rainy", "heavy rain"),
    "freezing_rain": ("rain", "rainy", "freezing rain"),
    "snow": ("snow", "snowy"),
    "heavy_snow": ("snow", "snowy", "heavy snow"),
    "rain_showers": ("rain", "rainy", "showers"),
    "heavy_rain_showers": ("rain", "rainy", "showers", "heavy rain"),
    "snow_showers": ("snow", "snowy", "showers"),
    "thunderstorm": ("thunderstorm", "storm"),
    "thunderstorm_hail": ("thunderstorm", "storm", "hail"),
}


def normalize_wmo_condition(weather_code: int | None) -> str | None:
    """Map a WMO weather code to a stable, search-friendly condition label."""
    if weather_code is None:
        return None
    return WMO_CONDITION_LABELS.get(int(weather_code), "unknown")


@dataclass(frozen=True)
class WeatherRecord:
    id: str
    session_id: str
    provider: str
    status: str
    requested_at: str | None
    observed_at: str | None
    timezone: str | None
    condition_label: str | None
    temperature_c: float | None
    precipitation_mm: float | None
    rain_mm: float | None
    cloud_cover_percent: float | None
    visibility_meters: float | None
    wind_speed_kmh: float | None
    weather_code: int | None
    grid_latitude: float | None
    grid_longitude: float | None
    source_latitude: float | None
    source_longitude: float | None
    fetched_at: str
    raw: dict[str, Any] = field(default_factory=dict)

    def search_tags(self) -> list[str]:
        """Return customer-friendly weather search terms from the normalized condition."""
        tags: list[str] = []
        if self.condition_label:
            tags.append(self.condition_label)
            pretty = self.condition_label.replace("_", " ")
            if pretty != self.condition_label:
                tags.append(pretty)
            tags.extend(_CONDITION_SEARCH_ALIASES.get(self.condition_label, ()))
        if (self.rain_mm or 0) > 0 or (self.precipitation_mm or 0) > 0:
            tags.extend(["rain", "rainy"])
        return list(dict.fromkeys(tag for tag in tags if tag))

    def public_dict(self) -> dict[str, Any]:
        """Fields safe for customer-facing package metadata.json."""
        return {
            "provider": self.provider,
            "status": self.status,
            "requested_at": self.requested_at,
            "observed_at": self.observed_at,
            "timezone": self.timezone,
            "condition_label": self.condition_label,
            "temperature_c": self.temperature_c,
            "precipitation_mm": self.precipitation_mm,
            "rain_mm": self.rain_mm,
            "cloud_cover_percent": self.cloud_cover_percent,
            "visibility_meters": self.visibility_meters,
            "wind_speed_kmh": self.wind_speed_kmh,
            "weather_code": self.weather_code,
            "grid_latitude": self.grid_latitude,
            "grid_longitude": self.grid_longitude,
            "fetched_at": self.fetched_at,
        }


class WeatherProvider(Protocol):
    name: str

    def fetch(self, session: dict[str, Any]) -> WeatherRecord:
        ...


class NoWeatherProvider:
    name = "none"

    def fetch(self, session: dict[str, Any]) -> WeatherRecord:
        return WeatherRecord(
            id=stable_id("WEATHER", session["id"], self.name),
            session_id=str(session["id"]),
            provider=self.name,
            status="not_enriched",
            requested_at=session.get("captured_at_local"),
            observed_at=None,
            timezone=session.get("timezone"),
            condition_label=None,
            temperature_c=None,
            precipitation_mm=None,
            rain_mm=None,
            cloud_cover_percent=None,
            visibility_meters=None,
            wind_speed_kmh=None,
            weather_code=None,
            grid_latitude=None,
            grid_longitude=None,
            source_latitude=session.get("center_lat"),
            source_longitude=session.get("center_lon"),
            fetched_at=utc_now(),
            raw={"reason": "weather_disabled"},
        )


class OpenMeteoHistoricalWeatherProvider:
    """Fetch the nearest historical hourly observation from Open-Meteo archive API."""

    name = "open-meteo"
    archive_url = "https://archive-api.open-meteo.com/v1/archive"

    def fetch(self, session: dict[str, Any]) -> WeatherRecord:
        latitude = session.get("center_lat")
        longitude = session.get("center_lon")
        capture_date = session.get("capture_date")
        requested_at = session.get("captured_at_local")
        timezone = session.get("timezone") or "auto"
        if latitude is None or longitude is None or not capture_date or not requested_at:
            return self._record(
                session,
                status="unavailable",
                reason="missing_location_or_capture_time",
            )

        variables = [
            "temperature_2m",
            "precipitation",
            "rain",
            "cloud_cover",
            "visibility",
            "wind_speed_10m",
            "weather_code",
        ]
        query = urllib.parse.urlencode(
            {
                "latitude": latitude,
                "longitude": longitude,
                "start_date": capture_date,
                "end_date": capture_date,
                "hourly": ",".join(variables),
                "timezone": timezone,
            }
        )
        request = urllib.request.Request(
            f"{self.archive_url}?{query}",
            headers={"User-Agent": "vclip-stock-pipeline/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            return self._record(
                session,
                status="failed",
                reason=f"fetch_failed:{type(exc).__name__}",
                detail=str(exc),
            )
        except Exception as exc:  # pragma: no cover - defensive
            return self._record(
                session,
                status="failed",
                reason=f"fetch_failed:{type(exc).__name__}",
                detail=str(exc),
            )

        hourly = payload.get("hourly") or {}
        times = hourly.get("time") or []
        index = nearest_hourly_index(times, requested_at)
        if index is None:
            return self._record(
                session,
                status="unavailable",
                reason="no_hourly_values",
                raw=payload,
            )

        code = _int_at(hourly, "weather_code", index)
        response_timezone = payload.get("timezone") or timezone
        return WeatherRecord(
            id=stable_id("WEATHER", session["id"], self.name),
            session_id=str(session["id"]),
            provider=self.name,
            status="enriched",
            requested_at=str(requested_at),
            observed_at=_value_at(hourly, "time", index),
            timezone=str(response_timezone) if response_timezone else None,
            condition_label=normalize_wmo_condition(code),
            temperature_c=_float_at(hourly, "temperature_2m", index),
            precipitation_mm=_float_at(hourly, "precipitation", index),
            rain_mm=_float_at(hourly, "rain", index),
            cloud_cover_percent=_float_at(hourly, "cloud_cover", index),
            visibility_meters=_float_at(hourly, "visibility", index),
            wind_speed_kmh=_float_at(hourly, "wind_speed_10m", index),
            weather_code=code,
            grid_latitude=_float_or_none(payload.get("latitude")),
            grid_longitude=_float_or_none(payload.get("longitude")),
            source_latitude=_float_or_none(latitude),
            source_longitude=_float_or_none(longitude),
            fetched_at=utc_now(),
            raw=payload,
        )

    def _record(
        self,
        session: dict[str, Any],
        *,
        status: str,
        reason: str,
        detail: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> WeatherRecord:
        payload = {"reason": reason}
        if detail:
            payload["detail"] = detail
        if raw:
            payload["response"] = raw
        return WeatherRecord(
            id=stable_id("WEATHER", session["id"], self.name),
            session_id=str(session["id"]),
            provider=self.name,
            status=status,
            requested_at=session.get("captured_at_local"),
            observed_at=None,
            timezone=session.get("timezone"),
            condition_label=None,
            temperature_c=None,
            precipitation_mm=None,
            rain_mm=None,
            cloud_cover_percent=None,
            visibility_meters=None,
            wind_speed_kmh=None,
            weather_code=None,
            grid_latitude=None,
            grid_longitude=None,
            source_latitude=_float_or_none(session.get("center_lat")),
            source_longitude=_float_or_none(session.get("center_lon")),
            fetched_at=utc_now(),
            raw=payload,
        )


def nearest_hourly_index(values: list[str], captured_at: str | None) -> int | None:
    """Select the true nearest hourly timestamp; do not floor to the current hour.

    Example: 07:56 selects 08:00, not 07:00.
    """
    if not values:
        return None
    if not captured_at:
        return min(12, len(values) - 1)
    try:
        target = _parse_local_datetime(str(captured_at))
        parsed = [_parse_local_datetime(value) for value in values]
    except ValueError:
        return min(12, len(values) - 1)
    return min(range(len(parsed)), key=lambda index: abs(parsed[index] - target))


def _parse_local_datetime(value: str) -> datetime:
    """Parse Open-Meteo / session local timestamps without flooring the clock."""
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    return parsed.replace(tzinfo=None)


def _value_at(hourly: dict[str, Any], key: str, index: int) -> Any:
    values = hourly.get(key) or []
    return values[index] if index < len(values) else None


def _float_at(hourly: dict[str, Any], key: str, index: int) -> float | None:
    return _float_or_none(_value_at(hourly, key, index))


def _int_at(hourly: dict[str, Any], key: str, index: int) -> int | None:
    value = _value_at(hourly, key, index)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
