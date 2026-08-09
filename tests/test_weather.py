from __future__ import annotations

import json
from typing import Any

from vclip_pipeline.packaging.weather import (
    OpenMeteoHistoricalWeatherProvider,
    nearest_hourly_index,
    normalize_wmo_condition,
)


def test_nearest_hourly_index_rounds_to_true_nearest_hour():
    hours = [f"2026-02-06T{hour:02d}:00" for hour in range(24)]
    # 07:56 is nearer to 08:00 than 07:00.
    assert nearest_hourly_index(hours, "2026-02-06T07:56:18") == 8
    assert hours[nearest_hourly_index(hours, "2026-02-06T07:56:18")] == "2026-02-06T08:00"
    # Exact hour and just-before midpoint stay on the earlier stamp.
    assert nearest_hourly_index(hours, "2026-02-06T07:00:00") == 7
    assert nearest_hourly_index(hours, "2026-02-06T07:29:59") == 7
    assert nearest_hourly_index(hours, "2026-02-06T07:30:00") == 7
    assert nearest_hourly_index(hours, "2026-02-06T07:30:01") == 8


def test_normalize_wmo_condition_labels():
    assert normalize_wmo_condition(0) == "clear"
    assert normalize_wmo_condition(1) == "mainly_clear"
    assert normalize_wmo_condition(2) == "partly_cloudy"
    assert normalize_wmo_condition(3) == "overcast"
    assert normalize_wmo_condition(45) == "fog"
    assert normalize_wmo_condition(61) == "rain"
    assert normalize_wmo_condition(71) == "snow"
    assert normalize_wmo_condition(999) == "unknown"
    assert normalize_wmo_condition(None) is None


def test_open_meteo_selects_nearest_hour_and_keeps_grid_provenance(monkeypatch):
    payload = {
        "latitude": 47.125,
        "longitude": -122.875,
        "timezone": "America/Los_Angeles",
        "hourly": {
            "time": [f"2026-02-06T{hour:02d}:00" for hour in range(24)],
            "temperature_2m": [float(hour) for hour in range(24)],
            "precipitation": [0.0] * 24,
            "rain": [0.0] * 24,
            "cloud_cover": [10.0] * 24,
            "visibility": [20000.0] * 24,
            "wind_speed_10m": [5.0] * 24,
            "weather_code": [1] * 24,
        },
    }

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    def fake_urlopen(request, timeout=30):  # noqa: ARG001
        assert "archive-api.open-meteo.com/v1/archive" in request.full_url
        assert "latitude=47.25" in request.full_url
        return _Response()

    monkeypatch.setattr(
        "vclip_pipeline.packaging.weather.urllib.request.urlopen",
        fake_urlopen,
    )

    session = {
        "id": "SESSION_TEST",
        "center_lat": 47.25,
        "center_lon": -122.9,
        "capture_date": "2026-02-06",
        "captured_at_local": "2026-02-06T07:56:18",
        "timezone": "America/Los_Angeles",
    }
    record = OpenMeteoHistoricalWeatherProvider().fetch(session)
    assert record.status == "enriched"
    assert record.provider == "open-meteo"
    assert record.requested_at == "2026-02-06T07:56:18"
    assert record.observed_at == "2026-02-06T08:00"
    assert record.timezone == "America/Los_Angeles"
    assert record.temperature_c == 8.0
    assert record.condition_label == "mainly_clear"
    assert record.weather_code == 1
    assert record.source_latitude == 47.25
    assert record.source_longitude == -122.9
    assert record.grid_latitude == 47.125
    assert record.grid_longitude == -122.875
    assert "mainly_clear" in record.search_tags()
    assert "sunny" in record.search_tags()
    public = record.public_dict()
    assert "source_latitude" not in public
    assert public["grid_latitude"] == 47.125


def test_open_meteo_failure_is_non_fatal_status(monkeypatch):
    def boom(*_args: Any, **_kwargs: Any):
        raise TimeoutError("network down")

    monkeypatch.setattr(
        "vclip_pipeline.packaging.weather.urllib.request.urlopen",
        boom,
    )
    record = OpenMeteoHistoricalWeatherProvider().fetch(
        {
            "id": "SESSION_FAIL",
            "center_lat": 47.25,
            "center_lon": -122.9,
            "capture_date": "2026-02-06",
            "captured_at_local": "2026-02-06T07:56:18",
            "timezone": "America/Los_Angeles",
        }
    )
    assert record.status == "failed"
    assert record.raw["reason"].startswith("fetch_failed:")
    assert record.condition_label is None


def test_open_meteo_unavailable_without_capture_time():
    record = OpenMeteoHistoricalWeatherProvider().fetch(
        {
            "id": "SESSION_MISSING",
            "center_lat": 47.25,
            "center_lon": -122.9,
            "capture_date": "2026-02-06",
            "captured_at_local": None,
            "timezone": "America/Los_Angeles",
        }
    )
    assert record.status == "unavailable"
    assert record.raw["reason"] == "missing_location_or_capture_time"
