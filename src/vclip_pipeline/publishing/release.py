"""Compile a logical package release from a frozen collection version.

This layer validates masters and writes ``package-release.json`` only. It never
copies, moves, hardlinks, or symlinks master media, and it does not generate
previews/thumbnails or call network services.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

from ..errors import VClipError
from ..util import safe_filename, sha256_file, stable_id, utc_now
from ..workflow.catalog import WorkflowCatalog

PACKAGE_REVISION = 1
RELEASE_STATUS = "release_core_ready"
MANIFEST_VERSION = 1


class PackageReleaseService:
    """Compile and validate a logical package release for a collection version."""

    def __init__(self, catalog: WorkflowCatalog) -> None:
        self.catalog = catalog

    def build(
        self,
        *,
        slug: str,
        version: int | None,
        output_directory: Path,
        overwrite: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        snapshot = self.catalog.collection_version(slug, version)
        definition = snapshot["definition"]
        version_row = snapshot["version"]
        clips = snapshot["clips"]
        if not clips:
            raise VClipError(
                f"Collection {definition['slug']} version {version_row['version']} has no clips."
            )

        title = str(definition["title"])
        collection_slug = str(definition["slug"])
        collection_version_id = str(version_row["id"])
        collection_version = int(version_row["version"])
        package_id = stable_id("PACKAGE", collection_version_id)

        compiled_clips: list[dict[str, Any]] = []
        orientations: set[str] = set()
        resolutions: set[str] = set()
        frame_rates: set[float] = set()
        codecs: set[str] = set()
        total_duration = 0.0
        total_size = 0

        for row in clips:
            compiled = self._compile_clip(row, title=title)
            compiled_clips.append(compiled)
            orientations.add(compiled["orientation"])
            resolutions.add(f"{compiled['width']}x{compiled['height']}")
            frame_rates.add(float(compiled["frame_rate"]))
            codecs.add(str(compiled["codec_name"]))
            total_duration += float(compiled["duration_seconds"])
            total_size += int(compiled["file_size_bytes"])

        # Orientation is derived for aggregates; omit from per-clip customer fields
        # that the product model lists (clip payload still needs technical dims).
        for clip in compiled_clips:
            clip.pop("orientation", None)

        manifest: dict[str, Any] = {
            "manifest_version": MANIFEST_VERSION,
            "package_id": package_id,
            "package_revision": PACKAGE_REVISION,
            "created_at": utc_now(),
            "status": RELEASE_STATUS,
            "collection_id": str(definition["id"]),
            "collection_slug": collection_slug,
            "collection_version_id": collection_version_id,
            "collection_version": collection_version,
            "title": title,
            "description": definition.get("description"),
            "clip_count": len(compiled_clips),
            "total_duration_seconds": total_duration,
            "total_size_bytes": total_size,
            "formats": {
                "orientations": sorted(orientations),
                "resolutions": sorted(resolutions),
                "frame_rates": sorted(frame_rates),
                "codecs": sorted(codecs),
            },
            "clips": compiled_clips,
        }

        if dry_run:
            return manifest

        destination = (
            output_directory.expanduser().resolve()
            / collection_slug
            / f"v{collection_version}"
            / "package-release.json"
        )
        if destination.exists() and not overwrite:
            raise VClipError(
                f"Package release already exists at {destination}. "
                "Pass overwrite=True to replace it."
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        manifest["manifest_path"] = str(destination)
        return manifest

    def _compile_clip(self, row: dict[str, Any], *, title: str) -> dict[str, Any]:
        stockify_run_id = str(row.get("stockify_run_id") or "").strip()
        stock_clip_id = str(row.get("stock_clip_id") or "").strip()
        export_id = str(row.get("export_id") or "").strip()
        if not stock_clip_id:
            raise VClipError("Collection clip is missing stock_clip_id.")
        if not export_id:
            raise VClipError(f"Collection clip {stock_clip_id} is missing export_id.")
        if not stockify_run_id:
            raise VClipError(f"Collection clip {stock_clip_id} is missing stockify_run_id.")

        exported_path_raw = row.get("exported_path")
        if not exported_path_raw:
            raise VClipError(f"Collection clip {stock_clip_id} has no exported_path.")
        master_path = Path(str(exported_path_raw))
        try:
            mode = master_path.lstat().st_mode
        except FileNotFoundError as exc:
            raise VClipError(
                f"Master export is missing for {stock_clip_id}: {master_path}"
            ) from exc
        if not stat.S_ISREG(mode):
            raise VClipError(
                f"Master export for {stock_clip_id} is not a regular file: {master_path}"
            )

        stored_sha = row.get("sha256")
        if not stored_sha:
            raise VClipError(f"Export {export_id} for {stock_clip_id} is missing sha256.")
        duration = row.get("duration_seconds")
        if duration is None:
            raise VClipError(f"Export {export_id} for {stock_clip_id} is missing duration_seconds.")
        try:
            duration_value = float(duration)
        except (TypeError, ValueError) as exc:
            raise VClipError(
                f"Export {export_id} for {stock_clip_id} has invalid duration_seconds."
            ) from exc
        if duration_value <= 0:
            raise VClipError(
                f"Export {export_id} for {stock_clip_id} has non-positive "
                f"duration_seconds ({duration_value})."
            )

        media = self._export_media(export_id)
        if media is None:
            raise VClipError(
                f"Export {export_id} for {stock_clip_id} is missing export_media_metadata."
            )
        width = media.get("width")
        height = media.get("height")
        codec_name = media.get("codec_name")
        frame_rate = media.get("frame_rate")
        missing_fields = [
            name
            for name, value in (
                ("width", width),
                ("height", height),
                ("codec_name", codec_name),
                ("frame_rate", frame_rate),
            )
            if value is None or value == ""
        ]
        if missing_fields:
            raise VClipError(
                f"Export {export_id} for {stock_clip_id} has incomplete "
                f"export_media_metadata (missing {', '.join(missing_fields)})."
            )

        physical_sha = sha256_file(master_path)
        if physical_sha != str(stored_sha):
            raise VClipError(
                f"SHA-256 mismatch for {stock_clip_id} ({export_id}): "
                f"stored={stored_sha} physical={physical_sha}."
            )

        sort_order = int(row["sort_order"])
        customer_filename = (
            safe_filename(f"{title} — Clip {sort_order:02d}") + master_path.suffix.lower()
        )
        orientation = WorkflowCatalog._orientation(int(width), int(height))
        if orientation is None:
            raise VClipError(
                f"Export {export_id} for {stock_clip_id} has invalid dimensions {width}x{height}."
            )

        return {
            "sort_order": sort_order,
            "stockify_run_id": stockify_run_id,
            "stock_clip_id": stock_clip_id,
            "export_id": export_id,
            "customer_filename": customer_filename,
            "master_path": str(master_path.resolve()),
            "master_sha256": str(stored_sha),
            "file_size_bytes": master_path.stat().st_size,
            "duration_seconds": duration_value,
            "width": int(width),
            "height": int(height),
            "frame_rate": float(frame_rate),
            "codec_name": str(codec_name),
            "orientation": orientation,
        }

    def _export_media(self, export_id: str) -> dict[str, Any] | None:
        with self.catalog.database.connect() as connection:
            row = connection.execute(
                """
                SELECT width, height, codec_name, frame_rate
                FROM export_media_metadata
                WHERE export_id=?
                """,
                (export_id,),
            ).fetchone()
        return dict(row) if row is not None else None
