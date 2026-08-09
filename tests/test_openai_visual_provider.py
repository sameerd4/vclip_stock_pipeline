from __future__ import annotations

from pathlib import Path

from vclip_pipeline.workflow.providers.openai import OpenAIVisualAnalyzer
from vclip_pipeline.workflow.taxonomy import VisualTaxonomy


def test_provider_discards_unknown_tags_and_never_verifies_named_subjects():
    taxonomy = VisualTaxonomy.from_path(
        Path(__file__).parents[1]
        / "src"
        / "vclip_pipeline"
        / "data"
        / "visual_taxonomy.json"
    )
    provider = OpenAIVisualAnalyzer(
        taxonomy=taxonomy,
        model="test",
        api_key="test",
    )
    analysis = provider._normalize(
        {
            "caption": "An urban road beside water.",
            "tags": [
                {
                    "group": "subject",
                    "tag": "road",
                    "strength": "primary",
                    "score": 0.9,
                    "frame_hits": [1, 2, 2],
                },
                {"group": "subject", "tag": "invented", "strength": "primary"},
            ],
            "named_subjects": [
                {"name": "Treasure Island", "confidence": "likely", "verified": True}
            ],
        }
    )
    assert [tag.tag for tag in analysis.tags] == ["road"]
    assert analysis.tags[0].frame_hits == (1, 2)
    assert analysis.named_subjects[0].verified is False


def test_provider_extracts_json_from_code_fence():
    assert OpenAIVisualAnalyzer._parse_json("```json\n{\"caption\": \"ok\"}\n```") == {
        "caption": "ok"
    }
