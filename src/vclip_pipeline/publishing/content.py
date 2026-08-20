"""Package content readiness: public metadata, rights review, and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import VClipError
from ..util import utc_now
from ..workflow.catalog import WorkflowCatalog
from .paths import (
    content_validation_path,
    load_json,
    load_package_release,
    public_metadata_path,
    release_directory,
    resolve_collection_version,
    rights_review_path,
    write_json,
)
from .public_metadata import PublicMetadataService, validate_public_metadata
from .rights_review import RightsReviewService, validate_rights_review


class ContentReadinessService:
    """Prepare and validate package content readiness for one release directory."""

    def __init__(self, catalog: WorkflowCatalog) -> None:
        self.catalog = catalog
        self.public_metadata = PublicMetadataService(catalog)
        self.rights_review = RightsReviewService()

    def prepare(
        self,
        *,
        slug: str,
        version: int | None,
        release_root: Path,
    ) -> dict[str, Any]:
        resolved_version = resolve_collection_version(
            self.catalog, slug=slug, version=version
        )
        release_dir = release_directory(release_root, slug, resolved_version)
        if not (release_dir / "package-release.json").is_file():
            raise VClipError(
                f"Package release core is missing at {release_dir / 'package-release.json'}. "
                "Run `vclip-workflow publish release` first."
            )

        public = self.public_metadata.prepare(
            slug=slug,
            version=resolved_version,
            release_root=release_root,
        )
        rights = self.rights_review.prepare(
            slug=slug,
            version=resolved_version,
            release_root=release_root,
        )
        return {
            "collection_slug": slug,
            "collection_version": resolved_version,
            "release_directory": str(release_dir),
            "public_metadata_path": public.get("path"),
            "rights_review_path": rights.get("path"),
            "clip_count": public.get("clip_count"),
            "status": "prepared",
            "note": (
                "Public metadata and rights-review template are ready. "
                "Content readiness still requires human rights approval and validate."
            ),
        }

    def validate(
        self,
        *,
        slug: str,
        version: int | None,
        release_root: Path,
    ) -> dict[str, Any]:
        resolved_version = resolve_collection_version(
            self.catalog, slug=slug, version=version
        )
        release_dir = release_directory(release_root, slug, resolved_version)
        package = load_package_release(release_dir)

        public_path = public_metadata_path(release_dir)
        rights_path = rights_review_path(release_dir)
        failures: list[str] = []

        public_ready = False
        rights_ready = False
        if not public_path.is_file():
            failures.append(f"public-metadata.json is missing at {public_path}")
            public = None
        else:
            public = load_json(public_path)
            public_failures = validate_public_metadata(public, package)
            failures.extend(public_failures)
            public_ready = not public_failures

        if not rights_path.is_file():
            failures.append(f"rights-review.json is missing at {rights_path}")
        else:
            rights = load_json(rights_path)
            rights_failures = validate_rights_review(rights, package)
            failures.extend(rights_failures)
            rights_ready = not rights_failures

        status = "content_ready" if not failures else "not_content_ready"
        result = {
            "status": status,
            "public_metadata_ready": public_ready,
            "rights_review_ready": rights_ready,
            "clip_count": int(package.get("clip_count") or 0),
            "collection_slug": slug,
            "collection_version": resolved_version,
            "package_id": package.get("package_id"),
            "validated_at": utc_now(),
            "failures": failures,
        }
        path = content_validation_path(release_dir)
        write_json(path, result)
        result["path"] = str(path)
        return result
