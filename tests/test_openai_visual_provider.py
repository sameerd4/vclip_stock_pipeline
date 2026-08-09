from __future__ import annotations

from pathlib import Path

from vclip_pipeline.workflow.enrichment import UsageTotals, format_openai_usage_block
from vclip_pipeline.workflow.models import ProviderUsage
from vclip_pipeline.workflow.pricing import (
    PRICING_VERSION,
    estimate_token_costs,
    lookup_model_rates,
)
from vclip_pipeline.workflow.providers.openai import (
    MAX_NAMED_SUBJECTS,
    MAX_OUTPUT_TOKENS,
    MAX_TAGS,
    PROMPT_VERSION,
    VISUAL_ANALYSIS_SCHEMA,
    OpenAIVisualAnalyzer,
)
from vclip_pipeline.workflow.taxonomy import VisualTaxonomy


def _taxonomy() -> VisualTaxonomy:
    return VisualTaxonomy.from_path(
        Path(__file__).parents[1]
        / "src"
        / "vclip_pipeline"
        / "data"
        / "visual_taxonomy.json"
    )


def _provider() -> OpenAIVisualAnalyzer:
    return OpenAIVisualAnalyzer(
        taxonomy=_taxonomy(),
        model="gpt-5-mini",
        api_key="test",
    )


def test_prompt_version_and_structured_output_limits():
    assert PROMPT_VERSION == "visual-taxonomy-v3"
    assert MAX_OUTPUT_TOKENS == 3000
    assert MAX_TAGS == 12
    assert MAX_NAMED_SUBJECTS == 3
    assert VISUAL_ANALYSIS_SCHEMA["properties"]["tags"]["maxItems"] == 12
    assert VISUAL_ANALYSIS_SCHEMA["properties"]["named_subjects"]["maxItems"] == 3
    frame_item = VISUAL_ANALYSIS_SCHEMA["properties"]["tags"]["items"]["properties"][
        "frame_hits"
    ]["items"]
    assert frame_item["minimum"] == 1
    assert frame_item["maximum"] == 6


def test_provider_discards_unknown_tags_and_never_verifies_named_subjects():
    provider = _provider()
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


def test_provider_filters_frame_hits_outside_one_through_six():
    provider = _provider()
    analysis = provider._normalize(
        {
            "caption": "Road.",
            "tags": [
                {
                    "group": "subject",
                    "tag": "road",
                    "strength": "primary",
                    "score": 0.8,
                    "frame_hits": [0, 1, 6, 7, -1, 3],
                }
            ],
            "named_subjects": [],
        }
    )
    assert analysis.tags[0].frame_hits == (1, 3, 6)


def test_provider_caps_tags_and_named_subjects():
    provider = _provider()
    tags = [
        {
            "group": "subject",
            "tag": "road",
            "strength": "secondary",
            "score": 0.5,
            "frame_hits": [1],
        }
        for _ in range(20)
    ]
    subjects = [{"name": f"Place {index}", "confidence": "possible"} for index in range(10)]
    analysis = provider._normalize(
        {"caption": "Many tags.", "tags": tags, "named_subjects": subjects}
    )
    assert len(analysis.tags) == MAX_TAGS
    assert len(analysis.named_subjects) == MAX_NAMED_SUBJECTS


def test_provider_extracts_json_from_code_fence():
    assert OpenAIVisualAnalyzer._parse_json('```json\n{"caption": "ok"}\n```') == {
        "caption": "ok"
    }


def test_parse_usage_full_object():
    usage = OpenAIVisualAnalyzer.parse_usage(
        {
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 400,
                "total_tokens": 1400,
                "input_tokens_details": {"cached_tokens": 250},
                "output_tokens_details": {"reasoning_tokens": 120},
            }
        },
        model="gpt-5-mini",
    )
    assert usage.input_tokens == 1000
    assert usage.cached_input_tokens == 250
    assert usage.output_tokens == 400
    assert usage.reasoning_tokens == 120
    assert usage.total_tokens == 1400
    assert usage.usage_missing is False
    # 1000/1e6*0.25 + 400/1e6*2.00 = 0.00025 + 0.0008 = 0.00105
    assert usage.estimated_input_cost_usd == 0.00025
    assert usage.estimated_output_cost_usd == 0.0008
    assert abs(usage.estimated_total_cost_usd - 0.00105) < 1e-12


def test_parse_usage_missing_object_does_not_fail():
    usage = OpenAIVisualAnalyzer.parse_usage({}, model="gpt-5-mini")
    assert usage.usage_missing is True
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.estimated_total_cost_usd is None


def test_parse_usage_cached_and_reasoning_tokens():
    usage = OpenAIVisualAnalyzer.parse_usage(
        {
            "usage": {
                "input_tokens": 500,
                "output_tokens": 200,
                "total_tokens": 700,
                "input_tokens_details": {"cached_tokens": 500},
                "output_tokens_details": {"reasoning_tokens": 80},
            }
        },
        model="gpt-5-mini",
    )
    assert usage.cached_input_tokens == 500
    assert usage.reasoning_tokens == 80
    # Pricing uses full output_tokens (200), not 200-80.
    assert usage.estimated_output_cost_usd == (200 / 1_000_000.0) * 2.0


def test_gpt5_mini_pricing_math():
    rates = lookup_model_rates("gpt-5-mini")
    assert rates is not None
    assert rates["input_per_million"] == 0.25
    assert rates["output_per_million"] == 2.0
    costs = estimate_token_costs(
        model="gpt-5-mini",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert costs.priced is True
    assert costs.input_cost_usd == 0.25
    assert costs.output_cost_usd == 2.0
    assert costs.total_cost_usd == 2.25
    assert PRICING_VERSION


def test_unknown_model_pricing_is_null():
    costs = estimate_token_costs(
        model="gpt-unknown-future",
        input_tokens=1000,
        output_tokens=500,
    )
    assert costs.priced is False
    assert costs.input_cost_usd is None
    assert costs.output_cost_usd is None
    assert costs.total_cost_usd is None
    usage = OpenAIVisualAnalyzer.parse_usage(
        {
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 500,
                "total_tokens": 1500,
            }
        },
        model="gpt-unknown-future",
    )
    assert usage.input_tokens == 1000
    assert usage.output_tokens == 500
    assert usage.estimated_total_cost_usd is None


def test_usage_aggregation_over_multiple_clips():
    totals = UsageTotals()
    totals.add(
        ProviderUsage(
            provider="openai",
            model="gpt-5-mini",
            input_tokens=1000,
            cached_input_tokens=100,
            output_tokens=200,
            reasoning_tokens=50,
            total_tokens=1200,
            estimated_total_cost_usd=0.001,
        )
    )
    totals.add(
        ProviderUsage(
            provider="openai",
            model="gpt-5-mini",
            input_tokens=2000,
            cached_input_tokens=400,
            output_tokens=300,
            reasoning_tokens=90,
            total_tokens=2300,
            estimated_total_cost_usd=0.002,
        )
    )
    totals.add(
        ProviderUsage(
            provider="openai",
            model="gpt-5-mini",
            usage_missing=True,
        )
    )
    assert totals.requests == 3
    assert totals.input_tokens == 3000
    assert totals.cached_input_tokens == 500
    assert totals.output_tokens == 500
    assert totals.reasoning_tokens == 140
    assert totals.total_tokens == 3500
    assert abs((totals.estimated_cost_usd or 0) - 0.003) < 1e-12
    assert totals.missing_usage_responses == 1

    from vclip_pipeline.workflow.enrichment import EnrichmentReport

    report = EnrichmentReport(
        provider="openai",
        model="gpt-5-mini",
        taxonomy_version=1,
        sampler_version="uniform-six-v1",
        analyzed=3,
        usage=totals,
    )
    lines = format_openai_usage_block(report)
    assert lines[0] == "OpenAI usage"
    assert "Requests:             3" in lines
    assert "Input tokens:       3,000" in lines
    assert "Cached input:       500" in lines
    assert "Output tokens:      500" in lines
    assert "Reasoning tokens:    140" in lines
    assert "Estimated cost:      $0.00" in lines


def test_analyze_returns_usage_with_mocked_response(tmp_path: Path):
    provider = _provider()
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"fake-jpeg")

    def fake_request(payload: dict):
        assert payload["max_output_tokens"] == 3000
        assert payload["text"]["format"]["strict"] is True
        assert payload["text"]["format"]["type"] == "json_schema"
        assert payload["text"]["format"]["schema"]["properties"]["tags"]["maxItems"] == 12
        return {
            "output_text": (
                '{"caption":"A road.","tags":[{"group":"subject","tag":"road",'
                '"strength":"primary","score":0.9,"frame_hits":[1],"rationale":"road"}],'
                '"named_subjects":[]}'
            ),
            "usage": {
                "input_tokens": 800,
                "output_tokens": 100,
                "total_tokens": 900,
                "input_tokens_details": {"cached_tokens": 50},
                "output_tokens_details": {"reasoning_tokens": 20},
            },
        }

    provider._request = fake_request  # type: ignore[method-assign]
    result = provider.analyze((frame,), context={"orientation": "landscape"})
    assert result.analysis.caption == "A road."
    assert result.usage is not None
    assert result.usage.input_tokens == 800
    assert result.usage.cached_input_tokens == 50
    assert result.usage.reasoning_tokens == 20
    assert result.usage.estimated_total_cost_usd is not None
