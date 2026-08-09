from __future__ import annotations

import copy
import xml.etree.ElementTree as ET

from vclip_pipeline.stockify.clips import sanitize_review_clip_effects
from vclip_pipeline.stockify.fcpxml import video_treatment_signature


def _lut_clip(*, ref: str = "r120", mix: str = "0.2", indent: bool = False) -> ET.Element:
    clip = ET.Element("asset-clip", {"ref": "r2", "name": "Clip", "start": "0s", "duration": "8s"})
    ET.SubElement(clip, "conform-rate", {"srcFrameRate": "59.94"})
    filt = ET.SubElement(clip, "filter-video", {"ref": ref, "name": "Custom LUT"})
    data = ET.SubElement(filt, "data", {"key": "effectConfig"})
    data.text = (
        "YnBsaXN0MDDUAQIDBAUGBwpYJHZlcnNpb25ZJGFyY2hpdmVyVCR0b3BYJG9iamVjdHMSAAGGoA=="
        if not indent
        else "YnBsaXN0MDDUAQIDBAUG\n  BwpYJHZlcnNpb25ZJGFyY2hpdmVyVCR0b3BYJG9iamVjdHMSAAGGoA=="
    )
    ET.SubElement(filt, "param", {"name": "LUT", "key": "3", "value": "lut-payload-a"})
    ET.SubElement(filt, "param", {"name": "Input", "key": "100/101", "value": "0 (Rec. 709)"})
    ET.SubElement(filt, "param", {"name": "Output", "key": "100/102", "value": "0 (Rec. 709)"})
    ET.SubElement(filt, "param", {"name": "Mix", "key": "99990", "value": mix})
    return clip


def test_treatment_signature_ignores_final_cut_round_trip_noise():
    original = _lut_clip(ref="r120", indent=False)
    round_tripped = _lut_clip(ref="r4", indent=True)
    # Final Cut may also reorder params.
    filt = next(
        child for child in list(round_tripped) if child.tag == "filter-video"
    )
    params = [child for child in list(filt) if child.tag == "param"]
    for param in params:
        filt.remove(param)
    for param in reversed(params):
        filt.append(param)

    assert video_treatment_signature(original) == video_treatment_signature(round_tripped)


def test_treatment_signature_detects_real_lut_parameter_change():
    original = _lut_clip(mix="0.2")
    edited = _lut_clip(mix="0.9")
    assert video_treatment_signature(original) != video_treatment_signature(edited)

    swapped = _lut_clip()
    filt = next(child for child in list(swapped) if child.tag == "filter-video")
    lut_param = next(child for child in list(filt) if child.get("name") == "LUT")
    lut_param.set("value", "lut-payload-b")
    assert video_treatment_signature(_lut_clip()) != video_treatment_signature(swapped)


def test_sanitized_review_treatment_matches_emitted_custom_lut_only_clip():
    source = _lut_clip()
    ET.SubElement(source, "filter-video", {"ref": "rletterbox", "name": "Letterbox"})
    ET.SubElement(source, "filter-video", {"ref": "raura", "name": "Aura"})
    resources = {
        "r120": ET.Element("effect", {"id": "r120", "name": "Custom LUT"}),
        "rletterbox": ET.Element("effect", {"id": "rletterbox", "name": "Letterbox"}),
        "raura": ET.Element("effect", {"id": "raura", "name": "Aura"}),
    }
    sanitized = sanitize_review_clip_effects(copy.deepcopy(source), resources)
    emitted_only_lut = _lut_clip()
    assert video_treatment_signature(sanitized) == video_treatment_signature(emitted_only_lut)
    assert video_treatment_signature(source) != video_treatment_signature(emitted_only_lut)
