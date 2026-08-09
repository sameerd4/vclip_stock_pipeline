from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from conftest import ProjectSpec, build_source_xml
from vclip_pipeline.db import CatalogRepository, Database
from vclip_pipeline.geo import CatalogLocationResolver, CompositeLocationResolver
from vclip_pipeline.stockify import StockifyOptions, StockifyService
from vclip_pipeline.stockify.core import local_name


def test_review_xml_keeps_custom_lut_and_strips_letterbox(tmp_path: Path):
    specs = [
        ProjectSpec(
            name="Graded With Effects",
            stamp="20260509190000",
            latitude=47.6253,
            longitude=-122.3377,
            graded=True,
            clip_count=1,
        )
    ]
    source = build_source_xml(tmp_path, specs)
    tree = ET.parse(source)
    root = tree.getroot()
    resources = root.find("resources")
    assert resources is not None
    ET.SubElement(
        resources,
        "effect",
        {"id": "rletterbox", "name": "Letterbox", "uid": ".../Letterbox.fxplug"},
    )
    clip = root.find(".//asset-clip")
    assert clip is not None
    ET.SubElement(clip, "filter-video", {"ref": "rletterbox", "name": "Letterbox"})
    lut = next(
        child
        for child in list(clip)
        if local_name(child.tag) == "filter-video"
        and "lut" in (child.get("name") or "").lower()
    )
    ET.SubElement(lut, "param", {"name": "LUT Name", "key": "1", "value": "Test LUT"})
    source.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))

    database_path = tmp_path / "effects.sqlite3"
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
    output = tmp_path / "review-effects.fcpxml"
    result = StockifyService(repository, resolver).run(
        StockifyOptions(
            input_path=source,
            output_path=output,
            report_path=tmp_path / "review-effects-report.json",
            database_path=database_path,
            manifest_path=tmp_path / "review-effects-manifest.json",
            layout="both",
            sidecar_roots=(),
        )
    )

    source_filters = [
        child.get("name")
        for child in list(ET.parse(source).getroot().find(".//asset-clip"))
        if local_name(child.tag) == "filter-video"
    ]
    assert "Custom LUT" in source_filters
    assert "Letterbox" in source_filters

    review_text = output.read_text(encoding="utf-8")
    assert "Custom LUT" in review_text
    assert "Letterbox" not in review_text
    assert "rletterbox" not in review_text

    review_root = ET.parse(output).getroot()
    effect_names = {
        node.get("name")
        for node in review_root.find("resources")
        if local_name(node.tag) == "effect"
    }
    assert "Custom LUT" in effect_names
    assert "Letterbox" not in effect_names

    review_clips = [
        node for node in review_root.iter() if local_name(node.tag) == "asset-clip"
    ]
    assert review_clips
    for review_clip in review_clips:
        filter_names = [
            child.get("name")
            for child in list(review_clip)
            if local_name(child.tag) == "filter-video"
        ]
        assert filter_names == ["Custom LUT"]
        lut_node = next(
            child
            for child in list(review_clip)
            if local_name(child.tag) == "filter-video"
        )
        assert any(
            local_name(child.tag) == "param" and child.get("value") == "Test LUT"
            for child in list(lut_node)
        )

    candidates = repository.candidates_for_run(result.stockify_run_id)
    accepted = [item for item in candidates if item["eligibility_status"] == "accepted"]
    assert accepted
    assert "Letterbox" in accepted[0]["creative_effects"]
    assert any("lut" in effect.lower() for effect in accepted[0]["creative_effects"])
