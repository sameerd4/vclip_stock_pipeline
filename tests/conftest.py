from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path

import pytest

from vclip_pipeline.db import CatalogRepository, Database
from vclip_pipeline.geo import CatalogLocationResolver, CompositeLocationResolver
from vclip_pipeline.stockify import StockifyOptions, StockifyService
from vclip_pipeline.stockify.core import format_time


@dataclass(frozen=True)
class ProjectSpec:
    name: str
    stamp: str
    latitude: float
    longitude: float
    graded: bool = False
    graded_clip_numbers: tuple[int, ...] | None = None
    clip_count: int = 1
    include_retime: bool = False
    asset_key: str | None = None
    mod_date: str | None = None
    clip_starts: tuple[float, ...] | None = None
    clip_durations: tuple[float, ...] | None = None
    asset_duration_seconds: float = 60.0


def write_srt(path: Path, stamp: str, latitude: float, longitude: float) -> None:
    base = datetime.strptime(stamp, "%Y%m%d%H%M%S")
    lines: list[str] = []
    for second in range(0, 61):
        captured = base + timedelta(seconds=second)
        lines.extend(
            [
                str(second + 1),
                f"00:00:{second:02d},000 --> 00:00:{second:02d},033",
                (
                    f"{captured:%Y-%m-%d %H:%M:%S}.000 "
                    f"latitude: {latitude + second * 0.000001:.6f} "
                    f"longitude: {longitude + second * 0.000001:.6f} "
                    "rel_alt: 20.0 yaw: 0"
                ),
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def build_source_xml(root_dir: Path, specs: list[ProjectSpec]) -> Path:
    root = ET.Element("fcpxml", {"version": "1.11"})
    resources = ET.SubElement(root, "resources")
    ET.SubElement(
        resources,
        "format",
        {
            "id": "r1",
            "name": "FFVideoFormat1080p30",
            "frameDuration": "1/30s",
            "width": "1920",
            "height": "1080",
            "colorSpace": "1-1-1 (Rec. 709)",
        },
    )
    ET.SubElement(resources, "effect", {"id": "rfx", "name": "Custom LUT", "uid": "custom-lut"})
    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", {"name": "Old Random Event", "uid": "EV1"})

    assets_by_key: dict[str, str] = {}
    next_asset_number = 2

    for project_number, spec in enumerate(specs, start=1):
        asset_key = spec.asset_key or f"unique-{project_number}"
        if asset_key in assets_by_key:
            asset_id = assets_by_key[asset_key]
        else:
            filename = f"DJI_{spec.stamp}_{project_number:04d}_D.MP4"
            media_path = root_dir / filename
            media_path.write_bytes(b"")
            write_srt(media_path.with_suffix(".SRT"), spec.stamp, spec.latitude, spec.longitude)
            asset_id = f"r{next_asset_number}"
            next_asset_number += 1
            asset = ET.SubElement(
                resources,
                "asset",
                {
                    "id": asset_id,
                    "name": filename,
                    "uid": f"ASSET-{asset_key}",
                    "start": "0s",
                    "duration": format_time(Fraction(str(spec.asset_duration_seconds))),
                    "hasVideo": "1",
                    "format": "r1",
                    "videoSources": "1",
                },
            )
            ET.SubElement(
                asset,
                "media-rep",
                {"kind": "original-media", "src": media_path.resolve().as_uri()},
            )
            assets_by_key[asset_key] = asset_id

        project_attrs = {"name": spec.name, "uid": f"PROJECT-{project_number}"}
        if spec.mod_date:
            project_attrs["modDate"] = spec.mod_date
        project = ET.SubElement(event, "project", project_attrs)
        sequence = ET.SubElement(
            project,
            "sequence",
            {
                "format": "r1",
                "duration": f"{spec.clip_count * 8 + (4 if spec.include_retime else 0)}s",
                "tcStart": "0s",
                "tcFormat": "NDF",
                "audioLayout": "stereo",
                "audioRate": "48k",
            },
        )
        spine = ET.SubElement(sequence, "spine")
        offset = 0
        starts = spec.clip_starts or tuple(
            clip_number * 3 for clip_number in range(1, spec.clip_count + 1)
        )
        durations = spec.clip_durations or tuple(8 for _ in starts)
        graded_numbers = (
            set(spec.graded_clip_numbers)
            if spec.graded_clip_numbers is not None
            else set(range(1, len(starts) + 1)) if spec.graded else set()
        )
        total_duration = sum(durations) + (4 if spec.include_retime else 0)
        sequence.set("duration", f"{total_duration}s")
        for clip_number, (start, duration) in enumerate(
            zip(starts, durations, strict=True),
            start=1,
        ):
            start_frac = Fraction(str(start))
            duration_frac = Fraction(str(duration))
            clip = ET.SubElement(
                spine,
                "asset-clip",
                {
                    "ref": asset_id,
                    "offset": format_time(Fraction(str(offset))),
                    "name": f"Clip {clip_number}",
                    "start": format_time(start_frac),
                    "duration": format_time(duration_frac),
                },
            )
            if clip_number in graded_numbers:
                ET.SubElement(clip, "filter-video", {"ref": "rfx", "name": "Custom LUT"})
            offset += float(duration)
        if spec.include_retime:
            retimed = ET.SubElement(
                spine,
                "asset-clip",
                {
                    "ref": asset_id,
                    "offset": format_time(Fraction(str(offset))),
                    "name": "Retimed",
                    "start": "20s",
                    "duration": "4s",
                },
            )
            time_map = ET.SubElement(retimed, "timeMap")
            ET.SubElement(time_map, "timept", {"time": "0s", "value": "20s"})
            ET.SubElement(time_map, "timept", {"time": "4s", "value": "28s"})

    ET.indent(root)
    path = root_dir / "source.fcpxml"
    path.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
    return path


def run_stockify(
    tmp_path: Path,
    specs: list[ProjectSpec],
    *,
    option_overrides: dict | None = None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = build_source_xml(tmp_path, specs)
    database_path = tmp_path / "vclip.sqlite3"
    database = Database(database_path)
    database.migrate()
    repository = CatalogRepository(database)
    resolver = CompositeLocationResolver(
        [
            CatalogLocationResolver.from_json(
                Path(__file__).parents[1] / "src" / "vclip_pipeline" / "data" / "places.json"
            )
        ]
    )
    output = tmp_path / "review.fcpxml"
    report = tmp_path / "stockify-report.json"
    manifest = tmp_path / "export-manifest.json"
    option_values = {
        "input_path": source,
        "output_path": output,
        "report_path": report,
        "database_path": database_path,
        "manifest_path": manifest,
        "layout": "both",
        "sidecar_roots": (),
    }
    option_values.update(option_overrides or {})
    options = StockifyOptions(**option_values)
    result = StockifyService(repository, resolver).run(options)
    return {
        "tmp_path": tmp_path,
        "source": source,
        "output": output,
        "report": report,
        "manifest": manifest,
        "database": database,
        "repository": repository,
        "resolver": resolver,
        "result": result,
        "options": options,
    }


@pytest.fixture
def scenario_specs() -> list[ProjectSpec]:
    return [
        ProjectSpec(
            "Seattle December 9th Remastered",
            "20251209153000",
            47.6231,
            -122.3165,
            graded=True,
            clip_count=2,
            include_retime=True,
        ),
        ProjectSpec(
            "May 2nd Evening Remastered",
            "20260502184500",
            47.6069,
            -122.3331,
            graded=True,
        ),
        ProjectSpec(
            "Hot Gunna Thug",
            "20260509190321",
            47.6253,
            -122.3377,
            graded=True,
            clip_count=2,
        ),
        ProjectSpec(
            "Hot Gunna Thug 1",
            "20260509191021",
            47.6254,
            -122.3378,
            graded=True,
            clip_count=2,
        ),
    ]


@pytest.fixture
def pipeline_run(tmp_path: Path, scenario_specs: list[ProjectSpec]):
    return run_stockify(tmp_path, scenario_specs)
