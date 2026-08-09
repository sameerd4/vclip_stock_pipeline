from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

from vclip_pipeline.packaging.astronomy import (
    build_astronomy,
    classify_solar_period,
    local_sunrise_sunset,
    proximity_affinity,
    weather_confidence_modifier,
)
from vclip_pipeline.packaging.weather import WeatherRecord


def _weather(**overrides) -> WeatherRecord:
    base = dict(
        id="WEATHER_TEST",
        session_id="SESSION_TEST",
        provider="open-meteo",
        status="enriched",
        requested_at="2026-02-06T07:30:00",
        observed_at="2026-02-06T08:00",
        timezone="America/Los_Angeles",
        condition_label="mainly_clear",
        temperature_c=8.0,
        precipitation_mm=0.0,
        rain_mm=0.0,
        cloud_cover_percent=10.0,
        visibility_meters=20000.0,
        wind_speed_kmh=5.0,
        weather_code=1,
        grid_latitude=47.0,
        grid_longitude=-123.0,
        source_latitude=47.0,
        source_longitude=-123.0,
        fetched_at="2026-02-06T12:00:00+00:00",
        raw={},
    )
    base.update(overrides)
    return WeatherRecord(**base)


def test_local_sunrise_sunset_olympia_winter_morning():
    zone = ZoneInfo("America/Los_Angeles")
    sunrise, sunset = local_sunrise_sunset(
        date(2026, 2, 6),
        47.0379,
        -122.9007,
        zone,
    )
    assert sunrise is not None and sunset is not None
    assert sunrise.astimezone(zone).hour == 7
    assert 20 <= sunrise.astimezone(zone).minute <= 40
    assert sunset.astimezone(zone).hour == 17
    assert sunrise < sunset


def test_classify_solar_period_buckets():
    assert classify_solar_period(0, -600) == "sunrise_window"
    assert classify_solar_period(-30, -650) == "sunrise_window"
    assert classify_solar_period(-60, -700) == "pre_dawn"
    assert classify_solar_period(90, -500) == "morning"
    assert classify_solar_period(300, -200) == "day"
    assert classify_solar_period(600, 0) == "sunset_window"
    assert classify_solar_period(700, 60) == "dusk"
    assert classify_solar_period(-300, 400) == "night"


def test_build_astronomy_persists_facts_without_sunrise_search_tag_semantics():
    record = build_astronomy(
        {
            "id": "SESSION_OLY",
            "center_lat": 47.0379,
            "center_lon": -122.9007,
            "captured_at_local": "2026-02-06T07:30:00",
            "timezone": "America/Los_Angeles",
        },
        _weather(condition_label="mainly_clear"),
    )
    assert record.status == "enriched"
    assert record.sunrise_time is not None
    assert record.sunset_time is not None
    assert record.minutes_from_sunrise is not None
    assert abs(record.minutes_from_sunrise) <= 45
    assert record.solar_period == "sunrise_window"
    public = record.public_dict()
    assert public["solar_period"] == "sunrise_window"
    assert "sunrise" not in public  # concept lives under concept_signals only
    assert public["concept_signals"]["sunrise"]["astronomical_affinity"] > 0.7
    assert public["concept_signals"]["sunrise"]["search_confidence"] > 0.7
    assert public["visual_analysis"]["status"] == "not_analyzed"
    assert "source_latitude" not in public


def test_fog_reduces_visual_sunrise_confidence_but_keeps_solar_period():
    clear = build_astronomy(
        {
            "id": "SESSION_CLEAR",
            "center_lat": 47.0379,
            "center_lon": -122.9007,
            "captured_at_local": "2026-02-06T07:30:00",
            "timezone": "America/Los_Angeles",
        },
        _weather(condition_label="mainly_clear", visibility_meters=20000),
    )
    foggy = build_astronomy(
        {
            "id": "SESSION_FOG",
            "center_lat": 47.0379,
            "center_lon": -122.9007,
            "captured_at_local": "2026-02-06T07:30:00",
            "timezone": "America/Los_Angeles",
        },
        _weather(
            condition_label="fog",
            cloud_cover_percent=95,
            visibility_meters=800,
        ),
    )
    assert clear.solar_period == foggy.solar_period == "sunrise_window"
    assert clear.minutes_from_sunrise == foggy.minutes_from_sunrise
    assert (
        foggy.concept_signals["sunrise"]["search_confidence"]
        < clear.concept_signals["sunrise"]["search_confidence"]
    )
    assert "fog" in foggy.concept_signals["sunrise"]["factors"]
    assert "very_low_visibility" in foggy.concept_signals["sunrise"]["factors"]


def test_weather_modifier_and_proximity_helpers():
    modifier, factors = weather_confidence_modifier(
        _weather(condition_label="overcast", cloud_cover_percent=90)
    )
    assert modifier <= 0.5
    assert "overcast" in factors
    assert proximity_affinity(0, 45) == 1.0
    assert 0.7 <= proximity_affinity(45, 45) <= 0.8
    assert proximity_affinity(200, 45) == 0.0
