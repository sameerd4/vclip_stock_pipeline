"""Shared filesystem layout helpers for package release directories."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..errors import VClipError
from .release import RELEASE_STATUS

PACKAGE_RELEASE_FILENAME = "package-release.json"
PUBLIC_METADATA_FILENAME = "public-metadata.json"
RIGHTS_REVIEW_FILENAME = "rights-review.json"
CONTENT_VALIDATION_FILENAME = "content-validation.json"


def release_directory(release_root: Path, slug: str, version: int) -> Path:
    return release_root.expanduser().resolve() / slug / f"v{version}"


def package_release_path(release_dir: Path) -> Path:
    return release_dir / PACKAGE_RELEASE_FILENAME


def public_metadata_path(release_dir: Path) -> Path:
    return release_dir / PUBLIC_METADATA_FILENAME


def rights_review_path(release_dir: Path) -> Path:
    return release_dir / "internal" / RIGHTS_REVIEW_FILENAME


def content_validation_path(release_dir: Path) -> Path:
    return release_dir / "internal" / CONTENT_VALIDATION_FILENAME


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VClipError(f"Required release file is missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise VClipError(f"Could not read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise VClipError(f"{path} must contain a JSON object.")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def load_package_release(release_dir: Path) -> dict[str, Any]:
    path = package_release_path(release_dir)
    manifest = load_json(path)
    status = manifest.get("status")
    if status != RELEASE_STATUS:
        raise VClipError(
            f"Package release at {path} has status {status!r}; "
            f"expected {RELEASE_STATUS!r}."
        )
    if not isinstance(manifest.get("clips"), list) or not manifest["clips"]:
        raise VClipError(f"Package release at {path} has no clips.")
    return manifest


def resolve_collection_version(
    catalog: Any,
    *,
    slug: str,
    version: int | None,
) -> int:
    snapshot = catalog.collection_version(slug, version)
    return int(snapshot["version"]["version"])
