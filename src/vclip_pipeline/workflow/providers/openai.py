"""Hosted multimodal analysis through the OpenAI Responses API."""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ...errors import VClipError
from ..models import (
    NamedSubject,
    ProviderUsage,
    VisualAnalysis,
    VisualAnalysisResult,
    VisualTag,
)
from ..entities import EntityCatalog
from ..pricing import estimate_token_costs
from ..taxonomy import VisualTaxonomy


PROMPT_VERSION = "visual-taxonomy-v3"
MAX_TAGS = 12
MAX_NAMED_SUBJECTS = 3
MAX_FRAME_INDEX = 6
MAX_OUTPUT_TOKENS = 3000

VISUAL_ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "caption": {"type": "string"},
        "tags": {
            "type": "array",
            "maxItems": MAX_TAGS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "group": {"type": "string"},
                    "tag": {"type": "string"},
                    "strength": {
                        "type": "string",
                        "enum": ["primary", "secondary", "context"],
                    },
                    "score": {"type": "number"},
                    "frame_hits": {
                        "type": "array",
                        "items": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": MAX_FRAME_INDEX,
                        },
                    },
                    "rationale": {"type": "string"},
                },
                "required": [
                    "group",
                    "tag",
                    "strength",
                    "score",
                    "frame_hits",
                    "rationale",
                ],
            },
        },
        "named_subjects": {
            "type": "array",
            "maxItems": MAX_NAMED_SUBJECTS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "confidence": {
                        "type": "string",
                        "enum": ["possible", "likely"],
                    },
                },
                "required": ["name", "confidence"],
            },
        },
    },
    "required": ["caption", "tags", "named_subjects"],
}


class OpenAIVisualAnalyzer:
    """Analyze six representative JPEGs in one low-detail multimodal request."""

    provider_name = "openai"

    def __init__(
        self,
        *,
        taxonomy: VisualTaxonomy,
        model: str = "gpt-5-mini",
        api_key: str | None = None,
        endpoint: str = "https://api.openai.com/v1/responses",
        timeout_seconds: float = 120.0,
        retries: int = 3,
        entities: EntityCatalog | None = None,
    ) -> None:
        self.taxonomy = taxonomy
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.entities = entities or EntityCatalog.default()
        if not self.api_key:
            raise VClipError("OPENAI_API_KEY is required for --provider openai.")

    def analyze(
        self,
        frames: tuple[Path, ...],
        *,
        context: dict[str, Any],
    ) -> VisualAnalysisResult:
        prompt = self._prompt(context)
        content: list[dict[str, Any]] = [
            {"type": "input_text", "text": prompt}
        ]
        for frame in frames:
            encoded = base64.b64encode(frame.read_bytes()).decode("ascii")
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:image/jpeg;base64,{encoded}",
                    "detail": "low",
                }
            )
        payload = {
            "model": self.model,
            "input": [{"role": "user", "content": content}],
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "visual_analysis",
                    "strict": True,
                    "schema": VISUAL_ANALYSIS_SCHEMA,
                }
            },
        }
        response = self._request(payload)
        text = self._response_text(response)
        parsed = self._parse_json(text)
        analysis = self._normalize(parsed)
        usage = self.parse_usage(response, model=self.model)
        return VisualAnalysisResult(analysis=analysis, usage=usage)

    def _prompt(self, context: dict[str, Any]) -> str:
        return json.dumps(
            {
                "task": (
                    "Analyze the ordered representative frames as one short stock-footage clip. "
                    "Describe only what is visibly supported across the frames. GPS/location context "
                    "is provenance, not proof of visible subject. A road or building in a tiny corner "
                    "must not be marked primary. Do not claim a named landmark unless visually plausible; "
                    "named subjects are suggestions and are never verified."
                ),
                "taxonomy": self.taxonomy.prompt_catalog(),
                "strengths": {
                    "primary": "central customer-facing subject or defining property",
                    "secondary": "clearly visible and useful but not dominant",
                    "context": "present only as supporting environment",
                },
                "tagging_rules": {
                    "coastal_vs_waterfront": (
                        "Use coastal only for ocean, beach, sea cliffs, or exposed ocean "
                        "coastline. Use waterfront for bay, marina, harbor, river, lake, "
                        "or urban water edge — including San Francisco Bay scenes."
                    ),
                    "establishing": (
                        "establishing is a common editorial purpose for aerial "
                        "place-establishing footage. It may appear often. Always also "
                        "tag the content being established (waterfront, architecture, "
                        "road, skyline, residential, bridge, etc.)."
                    ),
                },
                "limits": {
                    "max_tags": MAX_TAGS,
                    "max_named_subjects": MAX_NAMED_SUBJECTS,
                    "frame_hits": f"1-based indexes from 1 to {MAX_FRAME_INDEX}",
                },
                "context": context,
                "required_json": {
                    "caption": "one plain factual sentence",
                    "tags": [
                        {
                            "group": "one taxonomy group",
                            "tag": "one taxonomy id",
                            "strength": "primary|secondary|context",
                            "score": "0 to 1",
                            "frame_hits": f"1-based frame indexes in 1..{MAX_FRAME_INDEX}",
                            "rationale": "brief visible evidence",
                        }
                    ],
                    "named_subjects": [
                        {
                            "name": "possible visible named place or landmark",
                            "confidence": "possible|likely",
                        }
                    ],
                },
                "output_rule": (
                    "Return only JSON matching the provided schema. "
                    "No Markdown or commentary."
                ),
                "prompt_version": PROMPT_VERSION,
            },
            ensure_ascii=False,
        )

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            request = urllib.request.Request(
                self.endpoint,
                data=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.timeout_seconds,
                ) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                last_error = VClipError(
                    f"OpenAI visual analysis failed ({exc.code}): {detail[:1000]}"
                )
                if exc.code not in {408, 409, 429, 500, 502, 503, 504}:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt < self.retries:
                time.sleep(2 ** (attempt - 1))
        raise VClipError(f"OpenAI visual analysis failed after retries: {last_error}")

    @staticmethod
    def _response_text(response: dict[str, Any]) -> str:
        if isinstance(response.get("output_text"), str):
            return str(response["output_text"])
        parts: list[str] = []
        for item in response.get("output", []):
            for content in item.get("content", []):
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
        if not parts:
            raise VClipError("OpenAI response did not contain output text.")
        return "\n".join(parts)

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
        try:
            parsed = json.loads(cleaned.strip())
        except json.JSONDecodeError as exc:
            raise VClipError(f"Visual provider returned invalid JSON: {text[:1000]}") from exc
        if not isinstance(parsed, dict):
            raise VClipError("Visual provider returned JSON that is not an object.")
        return parsed

    @classmethod
    def parse_usage(
        cls,
        response: dict[str, Any],
        *,
        model: str,
        provider: str = "openai",
    ) -> ProviderUsage:
        """Parse Responses API usage; never raise when usage is absent."""
        raw_usage = response.get("usage")
        if not isinstance(raw_usage, dict):
            return ProviderUsage(
                provider=provider,
                model=model,
                usage_missing=True,
            )
        input_tokens = _optional_int(raw_usage.get("input_tokens"))
        output_tokens = _optional_int(raw_usage.get("output_tokens"))
        total_tokens = _optional_int(raw_usage.get("total_tokens"))
        input_details = raw_usage.get("input_tokens_details") or {}
        output_details = raw_usage.get("output_tokens_details") or {}
        cached_input_tokens = _optional_int(
            input_details.get("cached_tokens")
            if isinstance(input_details, dict)
            else None
        )
        reasoning_tokens = _optional_int(
            output_details.get("reasoning_tokens")
            if isinstance(output_details, dict)
            else None
        )
        # Bill output_tokens as reported. Reasoning is a breakdown, not additive.
        costs = estimate_token_costs(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return ProviderUsage(
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            total_tokens=total_tokens,
            estimated_input_cost_usd=costs.input_cost_usd,
            estimated_output_cost_usd=costs.output_cost_usd,
            estimated_total_cost_usd=costs.total_cost_usd,
            usage_missing=False,
        )

    def _normalize(self, payload: dict[str, Any]) -> VisualAnalysis:
        tags: list[VisualTag] = []
        for raw in payload.get("tags", []):
            if len(tags) >= MAX_TAGS:
                break
            if not isinstance(raw, dict):
                continue
            group = str(raw.get("group") or "")
            tag = str(raw.get("tag") or "")
            if not self.taxonomy.allows(group, tag):
                continue
            strength = str(raw.get("strength") or "context")
            if strength not in {"primary", "secondary", "context"}:
                strength = "context"
            score = raw.get("score")
            try:
                score_value = max(0.0, min(1.0, float(score)))
            except (TypeError, ValueError):
                score_value = None
            frame_hits = []
            for value in raw.get("frame_hits", []):
                try:
                    index = int(value)
                except (TypeError, ValueError):
                    continue
                if 1 <= index <= MAX_FRAME_INDEX:
                    frame_hits.append(index)
            tags.append(
                VisualTag(
                    group=group,
                    tag=tag,
                    strength=strength,
                    score=score_value,
                    frame_hits=tuple(sorted(set(frame_hits))),
                    rationale=str(raw.get("rationale") or "") or None,
                )
            )
        subjects: list[NamedSubject] = []
        for raw in payload.get("named_subjects", []):
            if len(subjects) >= MAX_NAMED_SUBJECTS:
                break
            if not isinstance(raw, dict) or not raw.get("name"):
                continue
            confidence = str(raw.get("confidence") or "possible")
            if confidence not in {"possible", "likely"}:
                confidence = "possible"
            subjects.append(
                self.entities.canonicalize_subject(
                    NamedSubject(
                        name=str(raw["name"]).strip(),
                        confidence=confidence,
                        verified=False,
                    )
                )
            )
        return VisualAnalysis(
            caption=str(payload.get("caption") or "").strip(),
            tags=tuple(tags),
            named_subjects=tuple(subjects),
            raw=payload,
        )


def _optional_int(value: object | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
