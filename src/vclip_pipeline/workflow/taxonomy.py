"""Controlled customer-facing visual taxonomy."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..errors import VClipError

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_taxonomy_key(value: str) -> str:
    text = (value or "").casefold().replace("&", " ")
    text = text.replace("–", "-").replace("—", "-")
    return _WHITESPACE_RE.sub(" ", text).strip()


class VisualTaxonomy:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.version = int(payload.get("version", 1))
        self.groups: dict[str, dict[str, dict[str, Any]]] = {}
        for group, rows in payload.get("groups", {}).items():
            self.groups[str(group)] = {
                str(row["id"]): dict(row) for row in rows
            }
        label_map: dict[str, str] = {}
        id_map: dict[str, str] = {}
        for group, tags in self.groups.items():
            for tag_id, row in tags.items():
                id_map[normalize_taxonomy_key(tag_id)] = tag_id
                label = str(row.get("label") or "")
                if label:
                    key = normalize_taxonomy_key(label)
                    existing = label_map.get(key)
                    if existing is not None and existing != tag_id:
                        raise ValueError(
                            f"Ambiguous taxonomy label {label!r}: {existing} vs {tag_id}"
                        )
                    label_map[key] = tag_id
        self._id_map = id_map
        self._label_map = label_map

    @classmethod
    def from_path(cls, path: Path) -> "VisualTaxonomy":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise VClipError(f"Could not read visual taxonomy {path}: {exc}") from exc
        return cls(payload)

    @classmethod
    def default(cls) -> "VisualTaxonomy":
        return cls.from_path(
            Path(__file__).resolve().parents[1] / "data" / "visual_taxonomy.json"
        )

    def allows(self, group: str, tag: str) -> bool:
        return tag in self.groups.get(group, {})

    def all_tags(self) -> list[tuple[str, str, dict[str, Any]]]:
        rows: list[tuple[str, str, dict[str, Any]]] = []
        for group, tags in self.groups.items():
            for tag_id, row in tags.items():
                rows.append((group, tag_id, row))
        return rows

    def label_for(self, tag_id: str) -> str | None:
        for tags in self.groups.values():
            row = tags.get(tag_id)
            if row is not None:
                label = row.get("label")
                return str(label) if label else None
        return None

    def resolve_query_term(self, term: str) -> str | None:
        """Resolve a customer-facing label or internal tag id to a tag id."""
        key = normalize_taxonomy_key(term)
        if not key:
            return None
        return self._id_map.get(key) or self._label_map.get(key)

    def prompt_catalog(self) -> dict[str, Any]:
        return {
            group: [
                {
                    "id": tag_id,
                    "label": row.get("label"),
                    "description": row.get("description"),
                }
                for tag_id, row in tags.items()
            ]
            for group, tags in self.groups.items()
        }
