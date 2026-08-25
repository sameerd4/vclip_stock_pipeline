"""Suggest, freeze, and materialize small customer-facing clip collections."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import VClipError
from ..stockify.core import slugify
from ..util import ensure_empty_directory, safe_filename, utc_now
from .catalog import WorkflowCatalog
from .models import CollectionClip


@dataclass
class CollectionSuggestion:
    title: str
    slug: str
    description: str | None
    rule: dict[str, Any]
    candidate_count: int
    selected_count: int
    clips: list[dict[str, Any]] = field(default_factory=list)


class CollectionService:
    """Use catalog facets to propose diverse sets, then publish stable snapshots."""

    def __init__(self, catalog: WorkflowCatalog) -> None:
        self.catalog = catalog

    def suggest(
        self,
        *,
        title: str,
        slug: str | None,
        description: str | None,
        rule: dict[str, Any],
    ) -> CollectionSuggestion:
        normalized_slug = slugify(slug or title)
        markets = [str(value) for value in rule.get("markets", [])]
        required = [str(value) for value in rule.get("required_tags", [])]
        preferred = {str(value) for value in rule.get("preferred_tags", [])}
        orientation = rule.get("orientation")
        maximum = int(rule.get("maximum_clips", 10))
        minimum = int(rule.get("minimum_clips", 1))
        max_per_source = int(rule.get("maximum_per_source_media", 2))
        max_per_session = int(rule.get("maximum_per_session", maximum))
        rows = self.catalog.catalog_rows(
            markets=markets,
            required_tags=required,
            orientation=str(orientation) if orientation else None,
        )
        ranked = sorted(
            (
                CollectionClip(
                    stockify_run_id=str(row["stockify_run_id"]),
                    stock_clip_id=str(row["stock_clip_id"]),
                    export_id=str(row["export_id"]),
                    exported_path=Path(str(row["exported_path"])),
                    score=self._score(row, preferred),
                    source_media_id=(
                        str(row["source_media_id"])
                        if row.get("source_media_id")
                        else None
                    ),
                    session_id=(str(row["session_id"]) if row.get("session_id") else None),
                    metadata=row,
                )
                for row in rows
            ),
            key=lambda item: (-item.score, item.stock_clip_id),
        )
        selected: list[CollectionClip] = []
        source_counts: dict[str, int] = {}
        session_counts: dict[str, int] = {}
        for clip in ranked:
            source_key = clip.source_media_id or f"clip:{clip.stock_clip_id}"
            session_key = clip.session_id or "unknown"
            if source_counts.get(source_key, 0) >= max_per_source:
                continue
            if session_counts.get(session_key, 0) >= max_per_session:
                continue
            selected.append(clip)
            source_counts[source_key] = source_counts.get(source_key, 0) + 1
            session_counts[session_key] = session_counts.get(session_key, 0) + 1
            if len(selected) >= maximum:
                break
        suggestion = CollectionSuggestion(
            title=title,
            slug=normalized_slug,
            description=description,
            rule=rule,
            candidate_count=len(rows),
            selected_count=len(selected),
            clips=[
                {
                    "stockify_run_id": clip.stockify_run_id,
                    "stock_clip_id": clip.stock_clip_id,
                    "export_id": clip.export_id,
                    "exported_path": str(clip.exported_path),
                    "score": round(clip.score, 6),
                    "source_media_id": clip.source_media_id,
                    "session_id": clip.session_id,
                    "caption": clip.metadata.get("caption"),
                    "markets": clip.metadata.get("markets", []),
                    "tags": clip.metadata.get("tags", []),
                    "rationale": {
                        "required_tags": required,
                        "preferred_tags": sorted(preferred),
                        "orientation": orientation,
                    },
                }
                for clip in selected
            ],
        )
        if suggestion.selected_count < minimum:
            raise VClipError(
                f"Collection rule found only {suggestion.selected_count} diverse clip(s); "
                f"minimum is {minimum}."
            )
        return suggestion

    @staticmethod
    def _score(row: dict[str, Any], preferred: set[str]) -> float:
        score = 0.0
        strength_weight = {"primary": 3.0, "secondary": 1.5, "context": 0.25}
        # Establishing is a common aerial editorial purpose — useful to filter,
        # but weakly informative for ranking versus subject/style tags.
        low_signal_tags = {"establishing"}
        for tag in row.get("tags", []):
            tag_name = str(tag.get("tag") or "")
            confidence = tag.get("score")
            try:
                confidence_value = float(confidence) if confidence is not None else 0.75
            except (TypeError, ValueError):
                confidence_value = 0.75
            weight = strength_weight.get(str(tag.get("strength") or "context"), 0.25)
            if tag_name in low_signal_tags:
                weight *= 0.15
            score += weight * confidence_value
            if tag_name in preferred and tag_name not in low_signal_tags:
                score += 2.0 * confidence_value
            elif tag_name in preferred:
                score += 0.25 * confidence_value
        duration = (
            row.get("export_duration_seconds")
            or row.get("final_duration_seconds")
            or row.get("proposed_duration_seconds")
        )
        try:
            duration_value = float(duration)
        except (TypeError, ValueError):
            duration_value = 0.0
        if 5.0 <= duration_value <= 12.0:
            score += 1.0
        elif duration_value >= 3.0:
            score += 0.5
        if row.get("caption"):
            score += 0.25
        return score

    def publish(self, suggestion: CollectionSuggestion) -> dict[str, Any]:
        collection_id = self.catalog.save_collection_definition(
            slug=suggestion.slug,
            title=suggestion.title,
            description=suggestion.description,
            rule=suggestion.rule,
        )
        version = self.catalog.publish_collection_version(
            collection_id=collection_id,
            rule=suggestion.rule,
            metadata={
                "title": suggestion.title,
                "slug": suggestion.slug,
                "description": suggestion.description,
                "candidate_count": suggestion.candidate_count,
                "selected_count": suggestion.selected_count,
            },
            clips=suggestion.clips,
        )
        return {
            "collection_id": collection_id,
            "slug": suggestion.slug,
            **version,
        }

    def materialize(
        self,
        *,
        slug: str,
        output_directory: Path,
        version: int | None = None,
        mode: str = "hardlink",
        overwrite: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if mode not in {"copy", "hardlink", "symlink"}:
            raise VClipError(f"Unsupported collection transfer mode: {mode}")
        snapshot = self.catalog.collection_version(slug, version)
        definition = snapshot["definition"]
        version_row = snapshot["version"]
        destination = output_directory.expanduser().resolve() / safe_filename(
            f"{definition['title']} — v{version_row['version']}"
        )
        clips_dir = destination / "clips"
        if not dry_run:
            ensure_empty_directory(destination, overwrite=overwrite)
            clips_dir.mkdir(parents=True, exist_ok=True)
        manifest_clips: list[dict[str, Any]] = []
        for row in snapshot["clips"]:
            source = Path(str(row["exported_path"]))
            if not source.is_file():
                raise VClipError(f"Collection export is missing: {source}")
            output_name = safe_filename(
                f"{int(row['sort_order']):02d} — {source.name}"
            )
            target = clips_dir / output_name
            if not dry_run:
                self._transfer(source, target, mode)
            manifest_clips.append(
                {
                    "sort_order": row["sort_order"],
                    "stockify_run_id": row["stockify_run_id"],
                    "stock_clip_id": row["stock_clip_id"],
                    "filename": output_name,
                    "relative_path": str(Path("clips") / output_name),
                    "duration_seconds": row.get("duration_seconds"),
                    "sha256": row.get("sha256"),
                    "score": row.get("score"),
                    "rationale": row.get("rationale", {}),
                }
            )
        manifest = {
            "manifest_version": 1,
            "created_at": utc_now(),
            "collection_id": definition["id"],
            "collection_slug": definition["slug"],
            "collection_version_id": version_row["id"],
            "version": version_row["version"],
            "title": definition["title"],
            "description": definition.get("description"),
            "rule": version_row["rule"],
            "metadata": version_row["metadata"],
            "clip_count": len(manifest_clips),
            "clips": manifest_clips,
        }
        if not dry_run:
            (destination / "manifest.json").write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            (destination / "metadata.json").write_text(
                json.dumps(
                    {
                        "title": definition["title"],
                        "description": definition.get("description"),
                        "version": version_row["version"],
                        "clip_count": len(manifest_clips),
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        return {
            "output_directory": str(destination),
            "clip_count": len(manifest_clips),
            "manifest": manifest,
        }

    @staticmethod
    def _transfer(source: Path, destination: Path, mode: str) -> None:
        if destination.exists() or destination.is_symlink():
            destination.unlink()
        if mode == "copy":
            shutil.copy2(source, destination)
        elif mode == "hardlink":
            try:
                os.link(source, destination)
            except OSError as exc:
                raise VClipError(
                    f"Hardlinking failed from {source} to {destination}: {exc}. "
                    "Source and destination may be on different filesystems. "
                    "Choose --mode copy or --mode symlink explicitly."
                ) from exc
        elif mode == "symlink":
            destination.symlink_to(source.resolve())
        else:  # pragma: no cover
            raise AssertionError(mode)


def load_rule(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise VClipError(f"Could not read collection rule {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise VClipError("Collection rule must be a JSON object.")
    return payload
