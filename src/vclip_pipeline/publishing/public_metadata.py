"""Compile customer-safe public descriptive metadata for a package release."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import VClipError
from ..workflow.catalog import WorkflowCatalog
from .paths import (
    load_package_release,
    public_metadata_path,
    release_directory,
    write_json,
)


class PublicMetadataService:
    """Build ``public-metadata.json`` from a frozen package release + catalog."""

    def __init__(self, catalog: WorkflowCatalog) -> None:
        self.catalog = catalog

    def prepare(
        self,
        *,
        slug: str,
        version: int,
        release_root: Path,
    ) -> dict[str, Any]:
        release_dir = release_directory(release_root, slug, version)
        package = load_package_release(release_dir)
        if str(package.get("collection_slug")) != slug:
            raise VClipError(
                f"Package release slug {package.get('collection_slug')!r} does not "
                f"match requested slug {slug!r}."
            )
        if int(package.get("collection_version")) != version:
            raise VClipError(
                f"Package release version {package.get('collection_version')!r} does "
                f"not match requested version {version}."
            )

        public = self.build_from_package(package)
        failures = validate_public_metadata(public, package)
        if failures:
            raise VClipError(
                "Public metadata is incomplete:\n- " + "\n- ".join(failures)
            )

        path = public_metadata_path(release_dir)
        write_json(path, public)
        public["path"] = str(path)
        return public

    def build_from_package(self, package: dict[str, Any]) -> dict[str, Any]:
        clips: list[dict[str, Any]] = []
        for package_clip in package["clips"]:
            clips.append(self._public_clip(package_clip))
        return {
            "title": package.get("title"),
            "description": package.get("description"),
            "collection_slug": package.get("collection_slug"),
            "collection_version": package.get("collection_version"),
            "clip_count": len(clips),
            "total_duration_seconds": package.get("total_duration_seconds"),
            "formats": package.get("formats") or {},
            "clips": clips,
        }

    def _public_clip(self, package_clip: dict[str, Any]) -> dict[str, Any]:
        run_id = str(package_clip.get("stockify_run_id") or "")
        clip_id = str(package_clip.get("stock_clip_id") or "")
        enrichment = self._clip_enrichment(run_id, clip_id)
        return {
            "sort_order": int(package_clip["sort_order"]),
            "customer_filename": package_clip.get("customer_filename"),
            "duration_seconds": package_clip.get("duration_seconds"),
            "width": package_clip.get("width"),
            "height": package_clip.get("height"),
            "frame_rate": package_clip.get("frame_rate"),
            "codec_name": package_clip.get("codec_name"),
            "caption": enrichment["caption"],
            "tags": enrichment["tags"],
            "markets": enrichment["markets"],
        }

    def _clip_enrichment(self, run_id: str, clip_id: str) -> dict[str, Any]:
        with self.catalog.database.connect() as connection:
            analysis = connection.execute(
                """
                SELECT caption
                FROM clip_visual_analysis
                WHERE stockify_run_id=? AND stock_clip_id=? AND status='complete'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (run_id, clip_id),
            ).fetchone()
            tag_rows = connection.execute(
                """
                SELECT tag
                FROM clip_tags
                WHERE stockify_run_id=? AND stock_clip_id=?
                ORDER BY tag COLLATE NOCASE, tag_group COLLATE NOCASE, source
                """,
                (run_id, clip_id),
            ).fetchall()
            market_rows = connection.execute(
                """
                SELECT market_label
                FROM clip_markets
                WHERE stockify_run_id=? AND stock_clip_id=?
                ORDER BY market_label COLLATE NOCASE, market_id
                """,
                (run_id, clip_id),
            ).fetchall()

        caption = ""
        if analysis is not None and analysis["caption"]:
            caption = str(analysis["caption"]).strip()

        tags: list[str] = []
        seen_tags: set[str] = set()
        for row in tag_rows:
            tag = str(row["tag"] or "").strip()
            key = tag.casefold()
            if not tag or key in seen_tags:
                continue
            seen_tags.add(key)
            tags.append(tag)

        markets: list[str] = []
        seen_markets: set[str] = set()
        for row in market_rows:
            label = str(row["market_label"] or "").strip()
            key = label.casefold()
            if not label or key in seen_markets:
                continue
            seen_markets.add(key)
            markets.append(label)

        return {"caption": caption, "tags": tags, "markets": markets}


def validate_public_metadata(
    public: dict[str, Any],
    package: dict[str, Any],
) -> list[str]:
    """Return human-readable failure reasons; empty means ready."""
    failures: list[str] = []
    package_clips = {
        int(clip["sort_order"]): clip for clip in package.get("clips", [])
    }
    public_clips = public.get("clips")
    if not isinstance(public_clips, list) or not public_clips:
        return ["public metadata has no clips"]

    if len(public_clips) != len(package_clips):
        failures.append(
            f"public metadata clip_count {len(public_clips)} does not match "
            f"package release clip_count {len(package_clips)}"
        )

    for clip in public_clips:
        sort_order = int(clip.get("sort_order") or 0)
        label = f"clip sort_order={sort_order}"
        package_clip = package_clips.get(sort_order)
        if package_clip is None:
            failures.append(f"{label}: no matching package-release clip")
            continue

        if clip.get("customer_filename") != package_clip.get("customer_filename"):
            failures.append(
                f"{label}: customer_filename {clip.get('customer_filename')!r} "
                f"does not match package-release "
                f"{package_clip.get('customer_filename')!r}"
            )

        caption = str(clip.get("caption") or "").strip()
        if not caption:
            failures.append(f"{label}: missing caption")

        tags = clip.get("tags")
        if not isinstance(tags, list) or not tags:
            failures.append(f"{label}: missing public tags")

        markets = clip.get("markets")
        if not isinstance(markets, list) or not markets:
            failures.append(f"{label}: missing markets")

        for field in (
            "duration_seconds",
            "width",
            "height",
            "frame_rate",
            "codec_name",
        ):
            value = clip.get(field)
            if value is None or value == "":
                failures.append(f"{label}: missing technical field {field}")
            elif field == "duration_seconds":
                try:
                    if float(value) <= 0:
                        failures.append(f"{label}: non-positive duration_seconds")
                except (TypeError, ValueError):
                    failures.append(f"{label}: invalid duration_seconds")

        forbidden = _forbidden_public_keys(clip)
        if forbidden:
            failures.append(
                f"{label}: exposes non-public fields ({', '.join(sorted(forbidden))})"
            )

    top_forbidden = _forbidden_public_keys(public, top_level=True)
    if top_forbidden:
        failures.append(
            "public metadata exposes non-public fields "
            f"({', '.join(sorted(top_forbidden))})"
        )
    return failures


_FORBIDDEN_CLIP_KEYS = {
    "master_path",
    "exported_path",
    "source_media_path",
    "source_srt_path",
    "srt_path",
    "latitude",
    "longitude",
    "exact_gps",
    "gps",
    "rationale",
    "stock_clip_id",
    "export_id",
    "stockify_run_id",
    "master_sha256",
    "sha256",
    "source_project_name",
    "source_event_name",
    "filesystem",
}


def _forbidden_public_keys(
    payload: dict[str, Any],
    *,
    top_level: bool = False,
) -> list[str]:
    found = [key for key in payload if key in _FORBIDDEN_CLIP_KEYS]
    if top_level:
        return found
    # Nested location/GPS blobs are also forbidden if present.
    for key, value in payload.items():
        if key in {"location", "capture", "gps", "coordinates"} and isinstance(
            value, dict
        ):
            if any(
                coord in value
                for coord in ("latitude", "longitude", "lat", "lon", "exact_gps")
            ):
                found.append(key)
    return found
