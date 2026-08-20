"""OpenAI vision rights-evidence extractor. JPEG stills only — never video."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...errors import VClipError
from ...publishing.rights_evidence import (
    FORBIDDEN_CONCLUSION_KEYS,
    OBSERVATION_FIELDS,
    PROMINENCE_FIELDS,
    PROMPT_VERSION,
    SAMPLED_FRAMES_LIMITATION,
    normalize_observations,
)
from ...publishing.rights_frames import RIGHTS_SAMPLER_VERSION
from ..models import ProviderUsage
from .openai import OpenAIResponsesClient, OpenAIVisualAnalyzer

SCHEMA_VERSION = 1
MAX_OUTPUT_TOKENS = 4000
MAX_NOTES = 8
MAX_CANDIDATES = 8
IMAGE_DETAIL = "high"
ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg"}
REJECTED_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mpg", ".mpeg"}

RIGHTS_EVIDENCE_INSTRUCTIONS = (
    """You are analyzing sampled still frames from stock footage.

Report observable visual evidence and uncertainty only.

You are NOT making legal determinations.

Do NOT determine or claim:
- whether the footage is legally safe
- whether the footage is commercially cleared
- whether a model release is legally required
- whether a property release is legally required
- whether a trademark creates a legal problem
- whether a depicted building/property may legally be licensed
- whether the original filming or drone flight was lawful
- whether the footage should be sold
- whether the footage should be classified as commercial or editorial
- whether any person, company, team, property owner, or organization authorized
  the footage

Prefer 'possible', 'unknown', or 'incidental' over an unjustifiably confident
or dominant interpretation. If there is clear visual evidence, report it.
We want conservative specificity, not avoidance.

Do not infer a legal conclusion from the mere presence of a brand, building,
person, artwork, or landmark.

"""
    + SAMPLED_FRAMES_LIMITATION
    + """

Inspect these sampled frames and report potentially rights-relevant observable
visual facts for a human stock-footage reviewer.

Categories:

1. recognizable_people — A recognizable person is someone who appears potentially
   visually identifiable as an individual from the supplied frames. Tiny pedestrians,
   crowds, distant people, or generic human figures should not automatically be
   called recognizable. If uncertain, use possible.

2. trademarks — logos, brand names, company marks, sports-team marks, commercial
   signage, branded products/vehicles where visually meaningful. Do not invent the
   exact brand if it cannot be read confidently.

For trademarks, determine prominence based on the visibility and visual
significance of the trademark or branding itself, not the prominence of the
object or property containing it.

A large stadium, building, vehicle, storefront, or product filling much of the
frame does not make trademarks prominent if the actual logos, brand names, or
marks are small, peripheral, unreadable, ambiguous, or visually secondary.

Use:

prominence = none
    when no trademark/branding is detected.

prominence = incidental
    when possible or detected branding is small, peripheral, partially
    obscured, unreadable, ambiguous, or secondary to the overall scene.

prominence = prominent
    only when the logo, brand name, team mark, company mark, or branded message
    itself is clearly visible/readable or otherwise visually dominant/significant.

If branding may exist on signage but the actual mark cannot be read or
identified confidently, prefer:
    status = possible
    prominence = incidental

Do not promote trademark prominence merely because the associated property is
prominent.

3. copyrighted_artwork — murals, sculptures, public art, installations, paintings,
   displayed/projection media, or other visually prominent creative works.

Do not mark copyrighted_artwork as possible merely because decorative colors,
architectural ornament, banners, advertisements, graphics, landscaping,
structures, or unidentified objects are visible.

copyrighted_artwork should be used only when there is a reasonable visual basis
to believe a distinct creative work is actually present.

Relevant examples:
- mural
- sculpture
- public-art installation
- painting
- clearly artistic graphic work
- displayed/projection media
- distinct creative installation

Do NOT classify ordinary:
- architecture
- building facades
- stadium structures
- decorative architectural features
- commercial signage
- generic banners/advertisements
- landscaping
as copyrighted artwork solely because they may contain creative design.

If an object could be either ordinary infrastructure/decor or artwork and there
is insufficient visual evidence, prefer:
    status = unknown
or:
    status = none_detected
depending on whether there is any concrete evidence of a distinct artwork.

Use status = possible only when there is an affirmative visual reason to
suspect a distinct creative work.

A trademark/advertisement is primarily a trademarks observation, not
automatically copyrighted_artwork.

A recognizable sculpture can be copyrighted_artwork even when located at an
identifiable property.

The same visual element may appear in multiple categories only when there is a
real independent basis for both observations.

4. identifiable_property — recognizable landmarks, stadiums, monuments, distinctive
   commercial properties, named buildings, or other readily identifiable built
   properties. This is an observation only. It does NOT mean the property requires
   clearance.

Property prominence describes the prominence of the identifiable property
itself.

Trademark prominence describes the mark itself.

These are independent dimensions.

5. identifying_information — visibly readable license plates, personal IDs, phone
   numbers, personal/private addresses, or other information that could identify a
   private individual. Do not treat generic street signs or public business addresses
   as private identifying information merely because text is visible.

6. professional_event_content — evidence that the footage actually depicts a
   professional sporting event, concert/performance, ticketed event, broadcast
   content, identifiable professional players/performers, or active event
   presentation. An empty stadium is NOT professional event content. A stadium
   existing in the frame is an identifiable-property observation, not event content.

Status values: none_detected, possible, detected, unknown.
Prominence values (trademarks, copyrighted_artwork, identifiable_property):
none, incidental, prominent, unknown.

none_detected means none detected in the sampled frames, not that none exist in
the complete video.

If you cannot reliably name a specific landmark or brand, prefer a generic
candidate (for example "baseball stadium") with status possible rather than a
confident specific name.
"""
)


def _enum_object(
    *,
    with_prominence: bool,
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "status": {
            "type": "string",
            "enum": ["none_detected", "possible", "detected", "unknown"],
        },
        "confidence": {"type": ["number", "null"]},
        "notes": {
            "type": "array",
            "maxItems": MAX_NOTES,
            "items": {"type": "string"},
        },
    }
    required = ["status", "confidence", "notes"]
    if with_prominence:
        properties["prominence"] = {
            "type": "string",
            "enum": ["none", "incidental", "prominent", "unknown"],
        }
        properties["candidates"] = {
            "type": "array",
            "maxItems": MAX_CANDIDATES,
            "items": {"type": "string"},
        }
        required.extend(["prominence", "candidates"])
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


RIGHTS_EVIDENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        field: _enum_object(with_prominence=field in PROMINENCE_FIELDS)
        for field in OBSERVATION_FIELDS
    },
    "required": list(OBSERVATION_FIELDS),
}


@dataclass(frozen=True)
class RightsEvidenceAnalysisResult:
    observations: dict[str, Any]
    usage: ProviderUsage | None = None
    prompt_version: str = PROMPT_VERSION
    schema_version: int = SCHEMA_VERSION
    sampler_version: str = RIGHTS_SAMPLER_VERSION


def require_jpeg_frames(frames: tuple[Path, ...] | list[Path]) -> tuple[Path, ...]:
    """Accept only local JPEG stills. Fail loudly on video or other non-images."""
    if not frames:
        raise VClipError("Rights visual provider requires at least one JPEG frame.")
    accepted: list[Path] = []
    for raw in frames:
        path = Path(raw)
        suffix = path.suffix.lower()
        if suffix in REJECTED_VIDEO_SUFFIXES:
            raise VClipError(
                "Rights visual provider rejected video input; only locally extracted "
                f"JPEG frames are allowed: {path.name}"
            )
        if suffix not in ALLOWED_IMAGE_SUFFIXES:
            raise VClipError(
                "Rights visual provider rejected non-image input; only JPEG frames "
                f"are allowed: {path.name}"
            )
        accepted.append(path)
    return tuple(accepted)


def assert_image_only_payload(payload: dict[str, Any]) -> None:
    """Fail if an OpenAI request contains anything other than JPEG image parts."""
    if payload.get("file") is not None or payload.get("files") is not None:
        raise VClipError("Rights visual provider refused a file-upload payload.")
    inputs = payload.get("input")
    if not isinstance(inputs, list):
        raise VClipError("Rights visual provider payload is missing input content.")
    image_parts = 0
    for item in inputs:
        if not isinstance(item, dict):
            raise VClipError("Rights visual provider payload input is malformed.")
        content = item.get("content")
        if not isinstance(content, list):
            raise VClipError("Rights visual provider payload content is malformed.")
        for part in content:
            if not isinstance(part, dict):
                raise VClipError("Rights visual provider payload part is malformed.")
            part_type = part.get("type")
            if part_type == "input_text":
                continue
            if part_type != "input_image":
                raise VClipError(
                    f"Rights visual provider rejected non-image input type: {part_type!r}"
                )
            image_url = str(part.get("image_url") or "")
            if not image_url.startswith("data:image/jpeg"):
                raise VClipError("Rights visual provider rejected a non-JPEG image_url.")
            image_parts += 1
    if image_parts < 1:
        raise VClipError("Rights visual provider payload contained no JPEG images.")


class OpenAIRightsEvidenceAnalyzer:
    """Inspect sampled JPEG stills and return structured rights observations."""

    provider_name = "openai"
    prompt_version = PROMPT_VERSION

    def __init__(
        self,
        *,
        model: str = "gpt-5-mini",
        api_key: str | None = None,
        endpoint: str = "https://api.openai.com/v1/responses",
        timeout_seconds: float = 120.0,
        retries: int = 3,
        http: OpenAIResponsesClient | None = None,
    ) -> None:
        self.model = model
        self._http = http or OpenAIResponsesClient(
            api_key=api_key,
            endpoint=endpoint,
            timeout_seconds=timeout_seconds,
            retries=retries,
        )
        self.api_key = self._http.api_key
        self.endpoint = self._http.endpoint
        self.timeout_seconds = self._http.timeout_seconds
        self.retries = self._http.retries

    def analyze(
        self,
        frames: tuple[Path, ...],
        *,
        context: dict[str, Any],
    ) -> RightsEvidenceAnalysisResult:
        payload = self.build_payload(frames, context=context)
        response = self._request(payload)
        text = OpenAIVisualAnalyzer._response_text(response)
        parsed = OpenAIVisualAnalyzer._parse_json(text)
        observations = self._normalize(parsed)
        usage = OpenAIVisualAnalyzer.parse_usage(response, model=self.model)
        return RightsEvidenceAnalysisResult(
            observations=observations,
            usage=usage,
        )

    def build_payload(
        self,
        frames: tuple[Path, ...],
        *,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        jpeg_frames = require_jpeg_frames(frames)
        content: list[dict[str, Any]] = [
            {"type": "input_text", "text": self._prompt(jpeg_frames, context)}
        ]
        for frame in jpeg_frames:
            encoded = base64.b64encode(frame.read_bytes()).decode("ascii")
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:image/jpeg;base64,{encoded}",
                    "detail": IMAGE_DETAIL,
                }
            )
        payload = {
            "model": self.model,
            "input": [{"role": "user", "content": content}],
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "rights_evidence_v1",
                    "strict": True,
                    "schema": RIGHTS_EVIDENCE_SCHEMA,
                }
            },
        }
        assert_image_only_payload(payload)
        return payload

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        assert_image_only_payload(payload)
        return self._http.post(payload)

    def _prompt(self, frames: tuple[Path, ...], context: dict[str, Any]) -> str:
        safe_context = {
            "duration_seconds": context.get("duration_seconds"),
            "caption": context.get("caption"),
            "market_label": context.get("market_label"),
            "sampled_frame_count": len(frames),
            "sampled_frames_limitation": SAMPLED_FRAMES_LIMITATION,
        }
        return (
            RIGHTS_EVIDENCE_INSTRUCTIONS
            + "\n\nContext:\n"
            + _json_context(safe_context)
            + "\n\nReturn only JSON matching the provided schema. "
            "No Markdown, commentary, or legal-conclusion fields."
        )

    def _normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise VClipError("Rights evidence provider returned JSON that is not an object.")
        forbidden = FORBIDDEN_CONCLUSION_KEYS.intersection(payload)
        if forbidden:
            raise VClipError(
                "Rights evidence contains prohibited legal-conclusion fields: "
                + ", ".join(sorted(forbidden))
            )
        if not any(isinstance(payload.get(field), dict) for field in OBSERVATION_FIELDS):
            raise VClipError("Rights evidence provider returned no observation fields.")
        try:
            return normalize_observations(payload)
        except VClipError:
            raise
        except (TypeError, ValueError) as exc:
            raise VClipError("Rights evidence provider returned an unusable payload.") from exc


def _json_context(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
