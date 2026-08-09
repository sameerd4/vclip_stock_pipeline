from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path

from vclip_pipeline.stockify.fcpxml import INGEST_DATE_KEY, asset_ingest_datetime
from vclip_pipeline.stockify.sidecars import build_sidecar_index, sidecar_match_for_asset


def _write_srt(path: Path, captured_at: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "1",
                "00:00:00,000 --> 00:00:00,033",
                f"{captured_at} latitude: 47.606200 longitude: -122.332100 rel_alt: 20.0",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _make_asset(
    *,
    asset_id: str,
    media_path: Path,
    ingest_date: str | None = None,
    name: str | None = None,
) -> ET.Element:
    asset = ET.Element(
        "asset",
        {
            "id": asset_id,
            "name": name or media_path.name,
            "hasVideo": "1",
            "videoSources": "1",
        },
    )
    ET.SubElement(asset, "media-rep", {"src": media_path.resolve().as_uri()})
    if ingest_date is not None:
        metadata = ET.SubElement(asset, "metadata")
        ET.SubElement(
            metadata,
            "md",
            {"key": INGEST_DATE_KEY, "value": ingest_date},
        )
    return asset


def test_sibling_srt_wins_over_duplicate_archive_match(tmp_path: Path):
    media_dir = tmp_path / "media"
    archive_a = tmp_path / "archive" / "miami"
    archive_b = tmp_path / "archive" / "seattle"
    media_dir.mkdir(parents=True)
    media = media_dir / "DJI_0437.MP4"
    media.write_bytes(b"")
    sibling = media.with_suffix(".SRT")
    _write_srt(sibling, "2024-05-09 19:10:00.000")
    # Lexicographically first archive path is the wrong year; sibling must still win.
    _write_srt(archive_a / "DJI_0437.SRT", "2023-04-26 07:23:36.000")
    _write_srt(archive_b / "DJI_0437.SRT", "2024-05-09 19:10:00.000")

    asset = _make_asset(
        asset_id="r1",
        media_path=media,
        ingest_date="2024-05-02 20:19:17 -0700",
    )
    index = build_sidecar_index([asset], [tmp_path / "archive"])
    match = sidecar_match_for_asset(asset, index)
    assert match.path == sibling.resolve()
    assert match.method == "exact_sibling"
    assert match.confidence == "high"
    assert match.ambiguous is False
    assert match.archive_candidate_count == 2


def test_ingest_date_disambiguates_duplicate_archive_stems(tmp_path: Path):
    media_dir = tmp_path / "media"
    archive_miami = tmp_path / "archive" / "miami"
    archive_seattle = tmp_path / "archive" / "seattle"
    media_dir.mkdir(parents=True)
    media = media_dir / "DJI_0437.MP4"
    media.write_bytes(b"")
    miami = archive_miami / "DJI_0437.SRT"
    seattle = archive_seattle / "DJI_0437.SRT"
    _write_srt(miami, "2023-04-26 07:23:36.000")
    _write_srt(seattle, "2024-05-09 19:10:00.000")

    asset = _make_asset(
        asset_id="r1",
        media_path=media,
        ingest_date="2024-05-02 20:19:17 -0700",
    )
    index = build_sidecar_index([asset], [tmp_path / "archive"])
    match = sidecar_match_for_asset(asset, index)
    assert match.path == seattle.resolve()
    assert match.method == "archive_stem_ingest_date"
    assert match.confidence == "medium"
    assert match.ambiguous is False
    assert match.archive_candidate_count == 2


def test_duplicate_archive_stems_without_ingest_date_are_ambiguous(tmp_path: Path):
    media_dir = tmp_path / "media"
    archive_a = tmp_path / "archive" / "a"
    archive_b = tmp_path / "archive" / "b"
    media_dir.mkdir(parents=True)
    media = media_dir / "DJI_0437.MP4"
    media.write_bytes(b"")
    _write_srt(archive_a / "DJI_0437.SRT", "2023-04-26 07:23:36.000")
    _write_srt(archive_b / "DJI_0437.SRT", "2024-05-09 19:10:00.000")

    asset = _make_asset(asset_id="r1", media_path=media)
    index = build_sidecar_index([asset], [tmp_path / "archive"])
    match = sidecar_match_for_asset(asset, index)
    assert match.path is None
    assert match.method == "ambiguous"
    assert match.confidence is None
    assert match.ambiguous is True
    assert match.archive_candidate_count == 2


def test_duplicate_archive_stems_with_close_timestamps_are_ambiguous(tmp_path: Path):
    media_dir = tmp_path / "media"
    archive_a = tmp_path / "archive" / "a"
    archive_b = tmp_path / "archive" / "b"
    media_dir.mkdir(parents=True)
    media = media_dir / "DJI_0437.MP4"
    media.write_bytes(b"")
    _write_srt(archive_a / "DJI_0437.SRT", "2024-05-08 10:00:00.000")
    _write_srt(archive_b / "DJI_0437.SRT", "2024-05-09 12:00:00.000")

    asset = _make_asset(
        asset_id="r1",
        media_path=media,
        ingest_date="2024-05-10 20:19:17 -0700",
    )
    index = build_sidecar_index([asset], [tmp_path / "archive"])
    match = sidecar_match_for_asset(asset, index)
    assert match.path is None
    assert match.method == "ambiguous"
    assert match.ambiguous is True
    assert match.archive_candidate_count == 2


def test_r12_reingest_prefers_nearest_archive_srt_by_ingest_date(
    tmp_path: Path,
    caplog,
):
    """Mirror asset r12: library re-ingest in 2026, capture SRTs from 2023 vs 2024."""
    media_dir = tmp_path / "media"
    archive_miami = tmp_path / "archive" / "march-may 2023"
    archive_seattle = tmp_path / "archive" / "may 2024"
    media_dir.mkdir(parents=True)
    media = media_dir / "DJI_0437.mp4"
    media.write_bytes(b"")
    miami = archive_miami / "DJI_0437.SRT"
    seattle = archive_seattle / "DJI_0437.SRT"
    _write_srt(miami, "2023-04-26 07:23:36.619")
    _write_srt(seattle, "2024-05-09 21:30:25.554")

    asset = _make_asset(
        asset_id="r12",
        media_path=media,
        name="DJI_0437",
        ingest_date="2026-08-07 00:13:33 -0700",
    )
    assert asset_ingest_datetime(asset).isoformat(sep=" ") == "2026-08-07 00:13:33-07:00"

    with caplog.at_level(logging.DEBUG, logger="vclip_pipeline.stockify.sidecars"):
        index = build_sidecar_index([asset], [tmp_path / "archive"])
        match = sidecar_match_for_asset(asset, index)

    assert match.path == seattle.resolve()
    assert match.method == "archive_stem_ingest_date"
    assert match.confidence == "medium"
    assert match.ambiguous is False
    assert match.archive_candidate_count == 2
    assert any(
        "ingest-date SRT match asset=r12 chose" in record.message
        and "may 2024" in record.message
        for record in caplog.records
    )
