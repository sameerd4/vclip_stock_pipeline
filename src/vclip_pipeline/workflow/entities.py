"""Deterministic canonicalization of visual named-subject suggestions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import NamedSubject

_WHITESPACE_RE = re.compile(r"\s+")
_TRAILING_PUNCT_RE = re.compile(r"[\s,.;:]+$")


@dataclass(frozen=True)
class VisualEntity:
    id: str
    label: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class EntityResolution:
    raw_name: str
    canonical_entity_id: str | None
    canonical_label: str | None
    resolution_source: str | None
    confidence: str
    verified: bool = False


class EntityCatalog:
    """Exact-alias entity registry. Prefers unresolved over risky merges."""

    def __init__(self, entities: list[VisualEntity]) -> None:
        self.entities = list(entities)
        self._by_id = {entity.id: entity for entity in entities}
        alias_map: dict[str, VisualEntity] = {}
        for entity in entities:
            for alias in (entity.id, entity.label, *entity.aliases):
                key = normalize_entity_alias(alias)
                if not key:
                    continue
                existing = alias_map.get(key)
                if existing is not None and existing.id != entity.id:
                    raise ValueError(
                        f"Ambiguous entity alias {alias!r}: "
                        f"{existing.id} vs {entity.id}"
                    )
                alias_map[key] = entity
        self._alias_map = alias_map

    @classmethod
    def from_path(cls, path: Path) -> "EntityCatalog":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_payload(payload)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "EntityCatalog":
        entities = [
            VisualEntity(
                id=str(row["id"]),
                label=str(row["label"]),
                aliases=tuple(str(alias) for alias in (row.get("aliases") or [])),
            )
            for row in payload.get("entities", [])
        ]
        return cls(entities)

    @classmethod
    def default(cls) -> "EntityCatalog":
        return cls.from_path(
            Path(__file__).resolve().parents[1] / "data" / "visual_entities.json"
        )

    def get(self, entity_id: str) -> VisualEntity | None:
        return self._by_id.get(entity_id)

    def resolve_alias(self, value: str) -> VisualEntity | None:
        """Resolve label, alias, or entity id via exact normalized match."""
        key = normalize_entity_alias(value)
        if not key:
            return None
        entity = self._alias_map.get(key)
        if entity is not None:
            return entity
        # Allow searching by canonical id (any case).
        return self._by_id.get(value.strip()) or self._by_id.get(value.strip().upper())

    def aliases_for(self, entity_id: str) -> tuple[str, ...]:
        entity = self._by_id.get(entity_id)
        if entity is None:
            return ()
        return (entity.label, *entity.aliases)

    def resolve_raw_name(
        self,
        raw_name: str,
        *,
        confidence: str = "possible",
        verified: bool = False,
    ) -> EntityResolution:
        """Resolve a raw model suggestion via exact normalized alias match."""
        key = normalize_entity_alias(raw_name)
        entity = self._alias_map.get(key) if key else None
        if entity is None:
            return EntityResolution(
                raw_name=raw_name.strip(),
                canonical_entity_id=None,
                canonical_label=None,
                resolution_source=None,
                confidence=confidence,
                verified=False if not verified else verified,
            )
        return EntityResolution(
            raw_name=raw_name.strip(),
            canonical_entity_id=entity.id,
            canonical_label=entity.label,
            resolution_source="alias_catalog",
            confidence=confidence,
            verified=False,  # Canonicalization is not factual verification.
        )

    def canonicalize_subject(self, subject: NamedSubject) -> NamedSubject:
        """Return a NamedSubject with canonical fields filled when resolvable."""
        if subject.verified and subject.canonical_entity_id:
            # Preserve human-verified truth; do not overwrite.
            return subject
        resolution = self.resolve_raw_name(
            subject.name,
            confidence=subject.confidence,
            verified=subject.verified,
        )
        return NamedSubject(
            name=resolution.raw_name,
            confidence=resolution.confidence,
            verified=False,
            canonical_entity_id=resolution.canonical_entity_id,
            canonical_label=resolution.canonical_label,
            resolution_source=resolution.resolution_source,
        )


def normalize_entity_alias(value: str) -> str:
    """Conservative alias key: casefold, collapse whitespace, strip edges."""
    text = (value or "").casefold().replace("–", "-").replace("—", "-")
    text = _WHITESPACE_RE.sub(" ", text).strip()
    text = _TRAILING_PUNCT_RE.sub("", text)
    return text
