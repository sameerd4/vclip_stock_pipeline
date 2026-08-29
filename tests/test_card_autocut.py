from __future__ import annotations

import importlib.util
from pathlib import Path

from vclip_pipeline.packaging.media import MediaProbe
from vclip_pipeline.stockify.core import parse_time


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vclip_card_autocut.py"
spec = importlib.util.spec_from_file_location("vclip_card_autocut", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def source(tmp_path: Path, name: str = "DJI_0001_D.MP4"):
    media = tmp_path / name
    media.write_bytes(b"video")
    return module.SourceMedia(
        path=media,
        probe=MediaProbe(
            duration_seconds=30.0,
            width=3840,
            height=2160,
            codec_name="hevc",
            frame_rate=59.94,
        ),
        srt_path=None,
        color_md="D-Log M",
        capture_time="2026-08-29T12:00:00-07:00",
    )


def candidate(src, start: float, duration: float, score: float):
    return module.Candidate(
        source=src,
        start_s=start,
        duration_s=duration,
        score=score,
        status="CANDIDATE",
        reasons=[],
        motion=module.MotionMetrics(),
    )


def test_windows_trim_edges_and_respect_bounds(tmp_path: Path):
    src = source(tmp_path)
    rows = module.windows(src, 5.0, 14.0, 4.0, 2.0)
    assert rows
    assert all(start >= 2.0 for start, _duration in rows)
    assert all(start + duration <= 28.0 + 1e-6 for start, duration in rows)


def test_choose_best_avoids_large_overlap(tmp_path: Path):
    src = source(tmp_path)
    rows = [
        candidate(src, 2.0, 10.0, 5.0),
        candidate(src, 4.0, 10.0, 4.0),
        candidate(src, 16.0, 10.0, 3.0),
    ]
    selected = module.choose_best(rows, 20.0, 2)
    assert [(round(row.start_s), round(row.duration_s)) for row in selected] == [
        (2, 10),
        (16, 10),
    ]


def test_fcpxml_references_source_and_applies_camera_lut(tmp_path: Path):
    src = source(tmp_path)
    row = candidate(src, 5.0, 10.0, 5.0)
    row.status = "SELECTED"
    root = module.build_fcpxml(
        [row],
        "2026-08-29 — DJI Auto Selects",
        "2026-08-29 — Best Of",
        "123 (DJI_DLogM)",
    )
    asset = next(node for node in root.iter() if node.tag == "asset")
    clip = next(node for node in root.iter() if node.tag == "asset-clip")
    media_rep = next(node for node in asset if node.tag == "media-rep")
    assert asset.get("customLUTOverride") == "123 (DJI_DLogM)"
    assert media_rep.get("src") == src.path.as_uri()
    assert abs(float(parse_time(clip.get("start"))) - 5.005) < 0.002
    assert abs(float(parse_time(clip.get("duration"))) - 10.01) < 0.02
