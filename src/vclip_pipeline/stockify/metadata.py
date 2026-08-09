"""Convert telemetry and source hints into location, time, and package metadata."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from fractions import Fraction

from .constants import CREATIVE_NAME_TOKENS, DJI_FILENAME_TIMESTAMP_RE, KNOWN_PLACES
from .core import normalized_text, slugify
from .models import SrtInfo, SrtSample

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 fallback.
    ZoneInfo = None  # type: ignore[assignment]


# Geographic helpers

# Measure the surface distance between two GPS points.
def haversine_meters(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> float:
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


# Calculate travel direction between two GPS points.
def bearing_degrees(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = (
        math.cos(phi1) * math.sin(phi2)
        - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    )
    return (math.degrees(math.atan2(y, x)) + 360) % 360


# Return the smallest difference between two headings.
def angular_delta_degrees(a: float, b: float) -> float:
    delta = abs(a - b) % 360
    return min(delta, 360 - delta)


# Identify source names that should not be treated as location evidence.
def is_creative_project_name(value: str | None) -> bool:
    text = normalized_text(value)
    return any(token in text for token in CREATIVE_NAME_TOKENS)


# Record which event, project, or sidecar names support a place match.
def name_hint_details(
    event_name: str,
    project_name: str,
    sidecar_path: str | None,
    place: dict[str, object] | None,
) -> dict[str, object]:
    sources = [
        ("event", event_name),
        ("project", project_name),
        ("sidecar_path", sidecar_path or ""),
    ]
    aliases = [str(alias).lower() for alias in (place or {}).get("aliases", [])]
    used: list[str] = []
    ignored: list[str] = []
    matched_sources: list[str] = []

    for source_name, value in sources:
        if not value:
            continue
        text = normalized_text(value)
        if source_name == "project" and is_creative_project_name(value):
            ignored.append(value)
            continue
        if aliases and any(alias in text for alias in aliases):
            used.append(value)
            matched_sources.append(f"{source_name}_name_hint")
        elif source_name in {"event", "project"}:
            ignored.append(value)

    return {
        "used": used,
        "ignored": ignored,
        "matched_sources": matched_sources,
    }


# DJI SRTs often emit null-island placeholders before a real lock.
GPS_NULL_ISLAND_EPSILON = 1e-6


def is_usable_gps(latitude: float | None, longitude: float | None) -> bool:
    """True when coordinates are present and not the (0,0) placeholder."""
    if latitude is None or longitude is None:
        return False
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(lat) or not math.isfinite(lon):
        return False
    return not (
        abs(lat) <= GPS_NULL_ISLAND_EPSILON and abs(lon) <= GPS_NULL_ISLAND_EPSILON
    )


def is_usable_gps_sample(sample: SrtSample) -> bool:
    return is_usable_gps(sample.latitude, sample.longitude)


def valid_gps_samples(samples: Iterable[SrtSample]) -> list[SrtSample]:
    """Keep only non-null, non-(0,0) GPS samples from a full or windowed SRT."""
    return [sample for sample in samples if is_usable_gps_sample(sample)]


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


# Select usable GPS samples that fall inside a clip source range.
def srt_samples_for_window(
    srt_info: SrtInfo | None,
    start: Fraction,
    duration: Fraction,
) -> list[SrtSample]:
    if srt_info is None:
        return []
    end = start + duration
    return [
        sample
        for sample in srt_info.samples
        if start <= sample.time <= end and is_usable_gps_sample(sample)
    ]


# Reduce clip GPS samples to a robust center point and spread.
def summarize_gps_samples(samples: list[SrtSample]) -> dict[str, object] | None:
    usable = valid_gps_samples(samples)
    if not usable:
        return None
    lats = [float(sample.latitude) for sample in usable if sample.latitude is not None]
    lons = [float(sample.longitude) for sample in usable if sample.longitude is not None]
    if not lats or not lons:
        return None
    # Coordinate-wise median resists early (0,0) leftovers and brief GPS spikes.
    center_lat = _median(lats)
    center_lon = _median(lons)
    radius = max(
        haversine_meters(center_lat, center_lon, lat, lon)
        for lat, lon in zip(lats, lons, strict=True)
    )
    return {
        "center_lat": round(center_lat, 6),
        "center_lon": round(center_lon, 6),
        "sample_count": len(lats),
        "valid_sample_count": len(lats),
        "radius_meters": round(radius, 3),
    }


def extract_gps_summary(
    srt_info: SrtInfo | None,
    *,
    start: Fraction | None = None,
    duration: Fraction | None = None,
    allow_full_sidecar_fallback: bool = True,
) -> dict[str, object] | None:
    """Shared GPS extraction for Stockify, recovery, and diagnostics.

    Prefers usable samples inside the clip window. When the window has only
    (0,0)/missing GPS, optionally falls back to all valid samples in the SRT.
    """
    if srt_info is None:
        return None
    samples: list[SrtSample] = []
    if start is not None and duration is not None:
        samples = srt_samples_for_window(srt_info, start, duration)
    if not samples and allow_full_sidecar_fallback:
        samples = valid_gps_samples(srt_info.samples)
    return summarize_gps_samples(samples)


# Match a GPS point to the nearest configured public place.
def nearest_known_place(lat: float, lon: float) -> dict[str, object] | None:
    candidates: list[tuple[float, dict[str, object]]] = []
    for place in KNOWN_PLACES:
        distance = haversine_meters(
            lat,
            lon,
            float(place["lat"]),
            float(place["lon"]),
        )
        if distance <= float(place["radius_m"]):
            candidates.append((distance, place))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item[0])[0][1]


# Build a safe public label from a known place.
def public_location_label(place: dict[str, object] | None) -> str | None:
    if not place:
        return None
    poi = place.get("poi")
    neighborhood = place.get("neighborhood")
    city = place.get("city")
    state = place.get("state")
    if poi and city:
        return f"{poi}, {city}"
    if neighborhood and city:
        if str(neighborhood).lower() == "downtown":
            return f"Downtown {city}"
        return f"{neighborhood}, {city}"
    if city and state:
        return f"{city}, {state}"
    return str(city or state or "") or None


# Combine SRT GPS and weak name hints into clip location metadata.
def resolve_clip_location(
    *,
    srt_info: SrtInfo | None,
    start: Fraction,
    duration: Fraction,
    event_name: str,
    project_name: str,
    sidecar_path: str | None,
    location_resolver: Callable[[float, float], dict[str, object] | None] | None = None,
) -> dict[str, object]:
    gps = extract_gps_summary(srt_info, start=start, duration=duration)
    if gps is None:
        hints = name_hint_details(event_name, project_name, sidecar_path, None)
        return {
            "status": "unresolved",
            "confidence": "low",
            "evidence_sources": ["missing_srt_gps"],
            "center_lat": None,
            "center_lon": None,
            "sample_count": 0,
            "radius_meters": None,
            "country": None,
            "state": None,
            "city": None,
            "neighborhood": None,
            "poi": None,
            "public_label": None,
            "name_hints": hints,
            "private_precision": "none",
            "review_required": True,
        }

    center_lat = float(gps["center_lat"])
    center_lon = float(gps["center_lon"])
    place = (
        location_resolver(center_lat, center_lon)
        if location_resolver is not None
        else nearest_known_place(center_lat, center_lon)
    )
    hints = name_hint_details(event_name, project_name, sidecar_path, place)
    evidence_sources = ["srt_gps", *list(hints["matched_sources"])]
    confidence = "high" if place else "medium"
    status = "resolved" if place else "gps_unresolved"

    return {
        "status": status,
        "confidence": confidence,
        "evidence_sources": evidence_sources,
        "center_lat": center_lat,
        "center_lon": center_lon,
        "sample_count": gps["sample_count"],
        "radius_meters": gps["radius_meters"],
        "country": place.get("country") if place else None,
        "state": place.get("state") if place else None,
        "city": place.get("city") if place else None,
        "neighborhood": place.get("neighborhood") if place else None,
        "poi": place.get("poi") if place else None,
        "public_label": public_location_label(place),
        "timezone": place.get("timezone") if place else None,
        "place_provider": place.get("provider") if place else None,
        "name_hints": hints,
        "private_precision": "exact_clip_gps_internal_only",
        "review_required": not bool(place),
    }


# Capture time and package metadata

# Read a DJI timestamp embedded in a media filename.
def parse_dji_filename_datetime(*names: str | None) -> datetime | None:
    for name in names:
        match = DJI_FILENAME_TIMESTAMP_RE.search(name or "")
        if not match:
            continue
        try:
            return datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S")
        except ValueError:
            continue
    return None


# Parse an ISO timestamp without throwing on bad input.
def parse_iso_local_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# Look up the configured timezone for resolved clip coordinates.
def timezone_for_location(location: dict[str, object]) -> str | None:
    direct = location.get("timezone")
    if direct:
        return str(direct)
    center_lat = location.get("center_lat")
    center_lon = location.get("center_lon")
    if isinstance(center_lat, (int, float)) and isinstance(center_lon, (int, float)):
        place = nearest_known_place(float(center_lat), float(center_lon))
        if place and place.get("timezone"):
            return str(place["timezone"])
    return None


# Assign a broad daylight band from local capture time.
def classify_time_of_day(local_dt: datetime | None) -> dict[str, object]:
    if local_dt is None:
        return {
            "label": "unknown",
            "confidence": "low",
            "source": "missing_capture_time",
        }
    hour = local_dt.hour + (local_dt.minute / 60)
    if 5 <= hour < 8:
        label = "morning_golden_hour"
    elif 8 <= hour < 11:
        label = "morning"
    elif 11 <= hour < 15:
        label = "midday"
    elif 15 <= hour < 18:
        label = "afternoon"
    elif 18 <= hour < 20:
        label = "evening_golden_hour"
    elif 20 <= hour < 22:
        label = "blue_hour"
    else:
        label = "night"
    return {
        "label": label,
        "confidence": "medium",
        "source": "local_hour_band",
        "local_hour": round(hour, 3),
    }


# Choose the best capture timestamp and localize it when possible.
def resolve_capture_time(
    *,
    srt_info: SrtInfo | None,
    start: Fraction,
    duration: Fraction,
    source_name: str,
    sidecar_path: str | None,
    location: dict[str, object],
) -> dict[str, object]:
    samples = srt_samples_for_window(srt_info, start, duration)
    captured_at = next((parse_iso_local_datetime(sample.captured_at) for sample in samples if sample.captured_at), None)
    source = "srt_timestamp" if captured_at else "dji_filename"
    confidence = "high" if captured_at else "medium"
    if captured_at is None:
        captured_at = parse_dji_filename_datetime(source_name, sidecar_path)
    if captured_at is None:
        return {
            "captured_at_local": None,
            "captured_at_utc": None,
            "timezone": None,
            "date": None,
            "confidence": "low",
            "source": "missing",
        }

    timezone_name = timezone_for_location(location)
    captured_at_utc = None
    if timezone_name and ZoneInfo is not None:
        try:
            localized = captured_at.replace(tzinfo=ZoneInfo(timezone_name))
            captured_at_utc = localized.astimezone(UTC).isoformat()
            captured_at_local = localized.isoformat()
        except Exception:
            captured_at_local = captured_at.isoformat()
    else:
        captured_at_local = captured_at.isoformat()

    return {
        "captured_at_local": captured_at_local,
        "captured_at_utc": captured_at_utc,
        "timezone": timezone_name,
        "date": captured_at.date().isoformat(),
        "confidence": confidence,
        "source": source,
    }


# Return the explicit placeholder used before weather enrichment.
def default_weather_metadata() -> dict[str, object]:
    return {
        "status": "not_enriched",
        "source": "historical_weather_not_configured",
        "review_required": False,
    }


# Return a value only when enough clips agree on it.
def dominant_value(clips: list[dict[str, object]], path: tuple[str, ...], threshold: float = 0.7) -> object | None:
    values: list[object] = []
    for clip in clips:
        current: object = clip
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if current not in (None, ""):
            values.append(current)
    if not values:
        return None
    counts = Counter(values)
    value, count = counts.most_common(1)[0]
    return value if count / len(values) >= threshold else None


# Infer a small set of seasonal tags from source names.
def season_tags(event_name: str, project_name: str) -> list[str]:
    text = normalized_text(f"{event_name} {project_name}")
    tags: list[str] = []
    if "cherry blossom" in text or "blossom" in text:
        tags.extend(["spring", "cherry blossoms"])
    return tags


# Build a reviewable stock-package title, description, tags, and provenance.
def build_package_hint(
    *,
    event_name: str,
    project_name: str,
    clips: list[dict[str, object]],
    fmt_info: dict[str, str | int | float | None],
) -> dict[str, object]:
    poi = dominant_value(clips, ("location", "poi"), threshold=0.5)
    neighborhood = dominant_value(clips, ("location", "neighborhood"))
    city = dominant_value(clips, ("location", "city"))
    state = dominant_value(clips, ("location", "state"))
    country = dominant_value(clips, ("location", "country"))
    public_label = dominant_value(clips, ("location", "public_label"), threshold=0.5)
    time_label = dominant_value(clips, ("time_of_day", "label"), threshold=0.5)
    confidence = "high" if city and (poi or neighborhood) else "medium" if city else "low"
    width = fmt_info.get("width")
    height = fmt_info.get("height")
    orientation = "unknown"
    if isinstance(width, int) and isinstance(height, int):
        orientation = "vertical" if height > width else "landscape" if width > height else "square"

    seasonal = season_tags(event_name, project_name)
    place_title = str(poi or neighborhood or city or "Stock Footage")
    descriptor = " ".join([tag.title() for tag in seasonal[:1]])
    title_parts = [place_title]
    if descriptor:
        title_parts.append(descriptor)
    title_parts.append("Aerials")
    suggested_title = " ".join(title_parts)

    tags = []
    for place in KNOWN_PLACES:
        if poi and place.get("poi") == poi:
            tags.extend(str(tag) for tag in place.get("tags", []))
            use_cases = [str(item) for item in place.get("use_cases", [])]
            break
    else:
        use_cases = []
    for value in [city, neighborhood, poi, *seasonal, time_label, "aerial", "drone", f"{orientation} video"]:
        if value and str(value) not in tags:
            tags.append(str(value))

    return {
        "suggested_title": suggested_title,
        "suggested_slug": slugify(suggested_title),
        "suggested_description": (
            f"Color-graded {orientation} drone clips around {public_label or place_title}."
        ),
        "source_provenance": {
            "source_event": event_name,
            "source_project": project_name,
            "name_policy": "source names retained as provenance only",
            "project_name_looks_creative": is_creative_project_name(project_name),
        },
        "location": {
            "country": country,
            "state": state,
            "city": city,
            "neighborhood": neighborhood,
            "poi": poi,
            "public_label": public_label,
            "confidence": confidence,
            "private_precision": "exact_clip_gps_internal_only",
            "outlier_count": sum(
                1 for clip in clips
                if isinstance(clip.get("location"), dict)
                and clip["location"].get("status") != "resolved"
            ),
        },
        "capture": {
            "time_of_day": time_label,
            "date": dominant_value(clips, ("capture_time", "date"), threshold=0.5),
            "timezone": dominant_value(clips, ("capture_time", "timezone"), threshold=0.5),
        },
        "discoverability": {
            "tags": tags,
            "use_cases": use_cases,
        },
        "review": {
            "metadata_review_required": True,
            "clip_review_required": True,
            "weather_enrichment_required": True,
            "notes": [
                "Generated at stockify time from SRT GPS, capture timestamps, and weak FCP name hints.",
                "Exact GPS is internal; public package metadata should use the resolved public label.",
            ],
        },
    }
