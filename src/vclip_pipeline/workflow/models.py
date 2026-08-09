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
    name: str
    confidence: str = "possible"
    verified: bool = False


@dataclass(frozen=True)
class VisualAnalysis:
    caption: str
    tags: tuple[VisualTag, ...]
    named_subjects: tuple[NamedSubject, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)


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
