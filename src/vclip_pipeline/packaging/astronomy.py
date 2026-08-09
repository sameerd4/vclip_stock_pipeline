"""Astronomical capture context for ranking and structured package metadata."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..util import stable_id, utc_now
from .weather import WeatherRecord


# Proximity windows used only for solar_period + ranking affinity — not search tags.
SUNRISE_WINDOW_MINUTES = 45
SUNSET_WINDOW_MINUTES = 45
PRE_DAWN_BEFORE_SUNRISE_MINUTES = 90
MORNING_AFTER_SUNRISE_MINUTES = 180
DUSK_AFTER_SUNSET_MINUTES = 90

SOLAR_PERIODS = (
    "pre_dawn",
    "sunrise_window",
    "morning",
    "day",
    "sunset_window",
    "dusk",
    "night",
)

# Official sunrise/sunset: geometric center of the sun at -0.833° (refraction + radius).
_SUN_ZENITH_DEGREES = 90.833


@dataclass(frozen=True)
class AstronomyRecord:
    """Factual solar geometry plus extensible ranking / visual-analysis signals."""

    id: str
    session_id: str
    status: str
    sunrise_time: str | None
    sunset_time: str | None
    minutes_from_sunrise: int | None
    minutes_from_sunset: int | None
    solar_period: str | None
    timezone: str | None
    source_latitude: float | None
    source_longitude: float | None
    concept_signals: dict[str, Any] = field(default_factory=dict)
    visual_analysis: dict[str, Any] = field(default_factory=dict)
    computed_at: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        """Customer-facing astronomy block: facts + ranking signals, no source GPS."""
        return {
            "status": self.status,
            "sunrise_time": self.sunrise_time,
            "sunset_time": self.sunset_time,
            "minutes_from_sunrise": self.minutes_from_sunrise,
            "minutes_from_sunset": self.minutes_from_sunset,
            "solar_period": self.solar_period,
            "timezone": self.timezone,
            "concept_signals": self.concept_signals,
            "visual_analysis": {
                "status": self.visual_analysis.get("status", "not_analyzed"),
                "concepts": self.visual_analysis.get("concepts", {}),
            },
        }


def build_astronomy(
    session: dict[str, Any],
    weather: WeatherRecord | None = None,
) -> AstronomyRecord:
    """Compute local sunrise/sunset context for a shoot session."""
    session_id = str(session.get("id") or "UNKNOWN")
    latitude = _float_or_none(session.get("center_lat"))
    longitude = _float_or_none(session.get("center_lon"))
    captured_at = session.get("captured_at_local")
    timezone_name = session.get("timezone")
    empty_visual = _empty_visual_analysis()

    if latitude is None or longitude is None or not captured_at:
        return AstronomyRecord(
            id=stable_id("ASTRONOMY", session_id),
            session_id=session_id,
            status="unavailable",
            sunrise_time=None,
            sunset_time=None,
            minutes_from_sunrise=None,
            minutes_from_sunset=None,
            solar_period=None,
            timezone=str(timezone_name) if timezone_name else None,
            source_latitude=latitude,
            source_longitude=longitude,
            concept_signals={},
            visual_analysis=empty_visual,
            computed_at=utc_now(),
            raw={"reason": "missing_location_or_capture_time"},
        )

    try:
        capture_local = _parse_local_datetime(str(captured_at), timezone_name)
        zone = capture_local.tzinfo
        if zone is None:
            raise ValueError("capture datetime is missing timezone")
        sunrise, sunset = local_sunrise_sunset(
            capture_local.date(),
            latitude,
            longitude,
            zone,
        )
    except (ValueError, ZoneInfoNotFoundError) as exc:
        return AstronomyRecord(
            id=stable_id("ASTRONOMY", session_id),
            session_id=session_id,
            status="unavailable",
            sunrise_time=None,
            sunset_time=None,
            minutes_from_sunrise=None,
            minutes_from_sunset=None,
            solar_period=None,
            timezone=str(timezone_name) if timezone_name else None,
            source_latitude=latitude,
            source_longitude=longitude,
            concept_signals={},
            visual_analysis=empty_visual,
            computed_at=utc_now(),
            raw={"reason": f"compute_failed:{type(exc).__name__}", "detail": str(exc)},
        )

    if sunrise is None or sunset is None:
        return AstronomyRecord(
            id=stable_id("ASTRONOMY", session_id),
            session_id=session_id,
            status="unavailable",
            sunrise_time=None,
            sunset_time=None,
            minutes_from_sunrise=None,
            minutes_from_sunset=None,
            solar_period=None,
            timezone=getattr(zone, "key", str(zone)),
            source_latitude=latitude,
            source_longitude=longitude,
            concept_signals={},
            visual_analysis=empty_visual,
            computed_at=utc_now(),
            raw={"reason": "polar_day_or_night"},
        )

    minutes_from_sunrise = int(round((capture_local - sunrise).total_seconds() / 60.0))
    minutes_from_sunset = int(round((capture_local - sunset).total_seconds() / 60.0))
    solar_period = classify_solar_period(minutes_from_sunrise, minutes_from_sunset)
    concept_signals = build_concept_signals(
        minutes_from_sunrise=minutes_from_sunrise,
        minutes_from_sunset=minutes_from_sunset,
        solar_period=solar_period,
        weather=weather,
    )
    tz_label = getattr(zone, "key", None) or str(timezone_name or zone)
    return AstronomyRecord(
        id=stable_id("ASTRONOMY", session_id),
        session_id=session_id,
        status="enriched",
        sunrise_time=_format_local(sunrise),
        sunset_time=_format_local(sunset),
        minutes_from_sunrise=minutes_from_sunrise,
        minutes_from_sunset=minutes_from_sunset,
        solar_period=solar_period,
        timezone=tz_label,
        source_latitude=latitude,
        source_longitude=longitude,
        concept_signals=concept_signals,
        visual_analysis=empty_visual,
        computed_at=utc_now(),
        raw={
            "algorithm": "noaa_zenith_0.833",
            "capture_local": _format_local(capture_local),
            "sunrise_local": _format_local(sunrise),
            "sunset_local": _format_local(sunset),
        },
    )


def classify_solar_period(
    minutes_from_sunrise: int,
    minutes_from_sunset: int,
) -> str:
    """Derive a coarse solar_period bucket from sunrise/sunset proximity."""
    if abs(minutes_from_sunrise) <= SUNRISE_WINDOW_MINUTES:
        return "sunrise_window"
    if abs(minutes_from_sunset) <= SUNSET_WINDOW_MINUTES:
        return "sunset_window"
    if (
        -PRE_DAWN_BEFORE_SUNRISE_MINUTES
        <= minutes_from_sunrise
        < -SUNRISE_WINDOW_MINUTES
    ):
        return "pre_dawn"
    if SUNRISE_WINDOW_MINUTES < minutes_from_sunrise <= MORNING_AFTER_SUNRISE_MINUTES:
        return "morning"
    if (
        SUNSET_WINDOW_MINUTES
        < minutes_from_sunset
        <= DUSK_AFTER_SUNSET_MINUTES
    ):
        return "dusk"
    if minutes_from_sunrise > MORNING_AFTER_SUNRISE_MINUTES and minutes_from_sunset < 0:
        return "day"
    return "night"


def build_concept_signals(
    *,
    minutes_from_sunrise: int,
    minutes_from_sunset: int,
    solar_period: str,
    weather: WeatherRecord | None,
) -> dict[str, Any]:
    """Ranking/search signals for visual solar concepts — not customer search tags.

    Astronomical proximity stays factual via solar_period / minute offsets.
    Weather only reduces confidence that footage *looks like* sunrise/sunset.
    """
    weather_modifier, weather_factors = weather_confidence_modifier(weather)
    sunrise_affinity = proximity_affinity(
        minutes_from_sunrise,
        SUNRISE_WINDOW_MINUTES,
    )
    sunset_affinity = proximity_affinity(
        minutes_from_sunset,
        SUNSET_WINDOW_MINUTES,
    )
    return {
        "sunrise": _concept_signal(
            concept="sunrise",
            astronomical_affinity=sunrise_affinity,
            weather_modifier=weather_modifier,
            weather_factors=weather_factors,
            solar_period=solar_period,
        ),
        "sunset": _concept_signal(
            concept="sunset",
            astronomical_affinity=sunset_affinity,
            weather_modifier=weather_modifier,
            weather_factors=weather_factors,
            solar_period=solar_period,
        ),
    }


def proximity_affinity(minutes_from_event: int, window_minutes: int) -> float:
    """1.0 at the event, strong inside the window, tapering to 0 by 2× window."""
    distance = abs(minutes_from_event)
    if distance <= window_minutes:
        return round(1.0 - 0.25 * (distance / window_minutes), 4)
    if distance <= window_minutes * 2:
        return round(0.75 * (1.0 - (distance - window_minutes) / window_minutes), 4)
    return 0.0


def weather_confidence_modifier(
    weather: WeatherRecord | None,
) -> tuple[float, list[str]]:
    """Return a 0–1 multiplier and human-readable penalty factors."""
    if weather is None or weather.status != "enriched":
        return 1.0, []

    modifier = 1.0
    factors: list[str] = []
    label = (weather.condition_label or "").lower()
    cloud = weather.cloud_cover_percent
    visibility = weather.visibility_meters

    if label in {"fog"}:
        modifier = min(modifier, 0.35)
        factors.append("fog")
    if label in {"overcast"}:
        modifier = min(modifier, 0.45)
        factors.append("overcast")
    if label in {"heavy_rain", "heavy_snow", "thunderstorm", "thunderstorm_hail"}:
        modifier = min(modifier, 0.4)
        factors.append(label)
    if cloud is not None and cloud >= 85:
        modifier = min(modifier, 0.5)
        factors.append("heavy_cloud_cover")
    if visibility is not None and visibility < 2000:
        modifier = min(modifier, 0.3)
        factors.append("very_low_visibility")
    elif visibility is not None and visibility < 5000:
        modifier = min(modifier, 0.6)
        factors.append("low_visibility")

    return round(modifier, 4), list(dict.fromkeys(factors))


def local_sunrise_sunset(
    day: date,
    latitude: float,
    longitude: float,
    zone: Any,
) -> tuple[datetime | None, datetime | None]:
    """Approximate local sunrise/sunset using the NOAA solar zenith method."""
    sunrise_utc = _solar_event_utc(day, latitude, longitude, rising=True)
    sunset_utc = _solar_event_utc(day, latitude, longitude, rising=False)
    if sunrise_utc is None or sunset_utc is None:
        return None, None
    sunrise = _align_to_local_date(sunrise_utc.astimezone(zone), day)
    sunset = _align_to_local_date(sunset_utc.astimezone(zone), day)
    if sunset <= sunrise:
        sunset += timedelta(days=1)
    return sunrise, sunset


def _align_to_local_date(value: datetime, day: date) -> datetime:
    """Shift an event onto the requested local civil date after UTC wrap."""
    return value + timedelta(days=(day - value.date()).days)


def _solar_event_utc(
    day: date,
    latitude: float,
    longitude: float,
    *,
    rising: bool,
) -> datetime | None:
    """Return sunrise (rising=True) or sunset UTC, or None for polar day/night.

    NOAA-style zenith algorithm using the day-of-year formulation.
    """
    day_of_year = day.timetuple().tm_yday
    lng_hour = longitude / 15.0
    t = day_of_year + (((6.0 if rising else 18.0) - lng_hour) / 24.0)
    m = (0.9856 * t) - 3.289
    l_deg = (
        m
        + (1.916 * math.sin(math.radians(m)))
        + (0.020 * math.sin(math.radians(2 * m)))
        + 282.634
    ) % 360.0
    ra = math.degrees(math.atan(0.91764 * math.tan(math.radians(l_deg)))) % 360.0
    l_quadrant = math.floor(l_deg / 90.0) * 90.0
    ra_quadrant = math.floor(ra / 90.0) * 90.0
    ra = (ra + (l_quadrant - ra_quadrant)) / 15.0
    sin_dec = 0.39782 * math.sin(math.radians(l_deg))
    cos_dec = math.cos(math.asin(sin_dec))
    cos_h = (
        math.cos(math.radians(_SUN_ZENITH_DEGREES))
        - (sin_dec * math.sin(math.radians(latitude)))
    ) / (cos_dec * math.cos(math.radians(latitude)))
    if cos_h > 1.0 or cos_h < -1.0:
        return None
    h_deg = (
        360.0 - math.degrees(math.acos(cos_h))
        if rising
        else math.degrees(math.acos(cos_h))
    )
    local_mean_time = (h_deg / 15.0) + ra - (0.06571 * t) - 6.622
    utc_hours = (local_mean_time - lng_hour) % 24.0
    seconds = int(round(utc_hours * 3600.0))
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(
        seconds=seconds
    )


def _concept_signal(
    *,
    concept: str,
    astronomical_affinity: float,
    weather_modifier: float,
    weather_factors: list[str],
    solar_period: str,
) -> dict[str, Any]:
    factors = []
    if concept == "sunrise" and solar_period == "sunrise_window":
        factors.append("sunrise_window")
    if concept == "sunset" and solar_period == "sunset_window":
        factors.append("sunset_window")
    if astronomical_affinity > 0:
        factors.append("astronomical_proximity")
    factors.extend(weather_factors)
    return {
        "astronomical_affinity": astronomical_affinity,
        "weather_modifier": weather_modifier,
        "search_confidence": round(astronomical_affinity * weather_modifier, 4),
        "factors": list(dict.fromkeys(factors)),
    }


def _empty_visual_analysis() -> dict[str, Any]:
    return {
        "status": "not_analyzed",
        "provider": None,
        "concepts": {},
        "notes": (
            "Reserved for future frame-level detection of sunrise, sunset, "
            "golden light, fog, and related visual concepts."
        ),
    }


def _parse_local_datetime(value: str, timezone_name: str | None) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is not None:
        if timezone_name:
            try:
                return parsed.astimezone(ZoneInfo(str(timezone_name)))
            except ZoneInfoNotFoundError:
                return parsed
        return parsed
    if not timezone_name:
        raise ValueError("timezone is required when capture time has no offset")
    return parsed.replace(tzinfo=ZoneInfo(str(timezone_name)))


def _format_local(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def astronomy_to_db(record: AstronomyRecord) -> dict[str, Any]:
    """Flatten an AstronomyRecord for SQLite upsert."""
    return {
        "id": record.id,
        "session_id": record.session_id,
        "status": record.status,
        "sunrise_time": record.sunrise_time,
        "sunset_time": record.sunset_time,
        "minutes_from_sunrise": record.minutes_from_sunrise,
        "minutes_from_sunset": record.minutes_from_sunset,
        "solar_period": record.solar_period,
        "timezone": record.timezone,
        "source_latitude": record.source_latitude,
        "source_longitude": record.source_longitude,
        "concept_signals": record.concept_signals,
        "visual_analysis": record.visual_analysis,
        "computed_at": record.computed_at or utc_now(),
        "raw": record.raw,
    }


def astronomy_from_db(row: dict[str, Any]) -> AstronomyRecord:
    return AstronomyRecord(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        status=str(row["status"]),
        sunrise_time=row.get("sunrise_time"),
        sunset_time=row.get("sunset_time"),
        minutes_from_sunrise=row.get("minutes_from_sunrise"),
        minutes_from_sunset=row.get("minutes_from_sunset"),
        solar_period=row.get("solar_period"),
        timezone=row.get("timezone"),
        source_latitude=row.get("source_latitude"),
        source_longitude=row.get("source_longitude"),
        concept_signals=row.get("concept_signals") or {},
        visual_analysis=row.get("visual_analysis") or _empty_visual_analysis(),
        computed_at=str(row.get("computed_at") or ""),
        raw=row.get("raw") or {},
    )
