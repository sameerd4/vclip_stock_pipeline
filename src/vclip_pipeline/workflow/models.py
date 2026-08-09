"""Domain models for the post-Stockify VClip workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ShardProject:
    event_name: str
    event_uid: str | None
    project_name: str
    project_uid: str | None
    representation: str
    stockify_run_id: str
    source_project_id: str
    stock_clip_ids: tuple[str, ...]
    market_id: str
    market_label: str
    element: Any = field(repr=False, compare=False)


@dataclass(frozen=True)
class ReviewShard:
    shard_id: str
    market_id: str
    market_label: str
    part: int
    path: Path
    manifest_path: Path
    project_count: int
    scope_project_count: int
    stock_clip_ids: tuple[str, ...]
    source_project_ids: tuple[str, ...]
    resource_ids: tuple[str, ...]
    size_bytes: int


@dataclass(frozen=True)
class FrameSampleSet:
    cache_key: str
    export_path: Path
    export_sha256: str
    duration_seconds: float
    frames: tuple[Path, ...]
    positions: tuple[float, ...]
    cache_directory: Path


@dataclass(frozen=True)
class VisualTag:
    group: str
    tag: str
    strength: str
    score: float | None = None
    frame_hits: tuple[int, ...] = ()
    rationale: str | None = None


@dataclass(frozen=True)
class NamedSubject:
    """A named landmark/place suggestion from vision (or human override).

    ``name`` is always the raw model suggestion. Canonical fields are optional
    normalization only — ``verified`` remains false unless a human confirms.
    """

    name: str
    confidence: str = "possible"
    verified: bool = False
    canonical_entity_id: str | None = None
    canonical_label: str | None = None
    resolution_source: str | None = None

    @property
    def raw_name(self) -> str:
        return self.name


@dataclass(frozen=True)
class VisualAnalysis:
    caption: str
    tags: tuple[VisualTag, ...]
    named_subjects: tuple[NamedSubject, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderUsage:
    """Token and estimated-cost metadata from one provider request."""

    provider: str
    model: str
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    estimated_input_cost_usd: float | None = None
    estimated_output_cost_usd: float | None = None
    estimated_total_cost_usd: float | None = None
    usage_missing: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "estimated_input_cost_usd": self.estimated_input_cost_usd,
            "estimated_output_cost_usd": self.estimated_output_cost_usd,
            "estimated_total_cost_usd": self.estimated_total_cost_usd,
            "usage_missing": self.usage_missing,
        }


@dataclass(frozen=True)
class VisualAnalysisResult:
    """Normalized analysis plus optional provider usage/cost metadata."""

    analysis: VisualAnalysis
    usage: ProviderUsage | None = None


@dataclass(frozen=True)
class CollectionClip:
    stockify_run_id: str
    stock_clip_id: str
    export_id: str
    exported_path: Path
    score: float
    source_media_id: str | None
    session_id: str | None
    metadata: dict[str, Any]
