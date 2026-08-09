"""Controlled customer-facing visual taxonomy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..errors import VClipError


class VisualTaxonomy:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.version = int(payload.get("version", 1))
        self.groups: dict[str, dict[str, dict[str, Any]]] = {}
        for group, rows in payload.get("groups", {}).items():
            self.groups[str(group)] = {
                str(row["id"]): dict(row) for row in rows
            }

    @classmethod
    def from_path(cls, path: Path) -> "VisualTaxonomy":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise VClipError(f"Could not read visual taxonomy {path}: {exc}") from exc
        return cls(payload)

    def allows(self, group: str, tag: str) -> bool:
        return tag in self.groups.get(group, {})

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
