"""Reviewer-facing orchestration for rights evidence, confirmation, and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import VClipError
from ..util import utc_now
from ..workflow.catalog import WorkflowCatalog
from .paths import (
    load_json,
    load_package_release,
    public_metadata_path,
    release_directory,
    resolve_collection_version,
    review_validation_path,
    rights_evidence_path,
    rights_review_path,
    write_json,
)
from .public_metadata import PublicMetadataService, apply_public_rights
from .rights_evidence import (
    RightsEvidenceService,
    evidence_risk,
    validate_evidence_document,
)
from .rights_review import (
    RightsReviewService,
    validate_review_document,
)

PASSING_CLASSIFICATIONS = {"standard", "standard_with_notice"}


class ReviewService:
    """Manage the non-web human rights-review workflow."""

    def __init__(
        self,
        catalog: WorkflowCatalog,
        *,
        evidence_service: RightsEvidenceService | None = None,
    ) -> None:
        self.catalog = catalog
        self.public_metadata_service = PublicMetadataService(catalog)
        self.evidence_service = evidence_service or RightsEvidenceService(catalog)
        self.review_service = RightsReviewService()

    def prepare(
        self,
        *,
        slug: str,
        version: int | None,
        release_root: Path,
        provider: str = "existing",
        model: str = "gpt-5-mini",
        cache: Path | None = None,
        refresh: bool = False,
        clip: int | None = None,
    ) -> dict[str, Any]:
        resolved = resolve_collection_version(self.catalog, slug=slug, version=version)
        public = self.public_metadata_service.prepare(
            slug=slug, version=resolved, release_root=release_root
        )
        evidence = self.evidence_service.prepare(
            slug=slug,
            version=resolved,
            release_root=release_root,
            provider=provider,
            model=model,
            cache_root=cache,
            refresh=refresh,
            clip=clip,
        )
        review = self.review_service.prepare(slug=slug, version=resolved, release_root=release_root)
        source = evidence.get("source") or {}
        return {
            "status": "review_prepared",
            "collection_slug": slug,
            "collection_version": resolved,
            "clip": clip,
            "clip_count": review["clip_count"],
            "public_metadata_path": public["path"],
            "rights_evidence_path": evidence["path"],
            "rights_review_path": review["path"],
            "provider": source.get("provider", provider),
            "model": source.get("model"),
            "openai_requests": source.get("openai_requests", 0),
            "cached_clips": source.get("cached_clips", 0),
            "sampled_frame_count_total": source.get("sampled_frame_count_total", 0),
            "network_calls": source.get("network_calls", False),
            "usage": source.get("usage"),
        }

    def list_clips(
        self,
        *,
        slug: str,
        version: int | None,
        release_root: Path,
        pending_only: bool = False,
        risk: str | None = None,
    ) -> dict[str, Any]:
        resolved, release_dir, package, public, evidence, review = self._documents(
            slug, version, release_root
        )
        public_by_order = _by_order(public)
        evidence_by_order = _by_order(evidence)
        review_by_order = _by_order(review)
        all_rows: list[dict[str, Any]] = []
        for package_clip in package["clips"]:
            order = int(package_clip["sort_order"])
            review_clip = review_by_order.get(order)
            evidence_clip = evidence_by_order.get(order)
            if review_clip is None or evidence_clip is None:
                raise VClipError(f"Review documents are missing clip {order}.")
            human_status = review_clip["human_review"]["status"]
            row_risk = evidence_risk(evidence_clip)
            public_clip = public_by_order.get(order, {})
            all_rows.append(
                {
                    "sort_order": order,
                    "human_review_status": human_status,
                    "classification": review_clip["classification"]["value"],
                    "caption": public_clip.get("caption") or "",
                    "risk": row_risk,
                }
            )
        rows = [
            row
            for row in all_rows
            if (not pending_only or row["human_review_status"] == "pending")
            and (not risk or row["risk"] == risk)
        ]
        totals = {
            value: sum(1 for row in all_rows if row["risk"] == value)
            for value in ("LOW", "REVIEW", "HIGH", "UNKNOWN")
        }
        return {
            "collection_slug": slug,
            "collection_version": resolved,
            "rows": rows,
            "totals": totals,
            "total": len(rows),
            "release_directory": str(release_dir),
        }

    def show(
        self,
        *,
        slug: str,
        version: int | None,
        release_root: Path,
        clip: int,
    ) -> dict[str, Any]:
        resolved, _, package, public, evidence, review = self._documents(
            slug, version, release_root
        )
        package_clip = _required_by_order(package, clip, "package-release.json")
        public_clip = _required_by_order(public, clip, "public-metadata.json")
        evidence_clip = _required_by_order(evidence, clip, "rights-evidence.json")
        review_clip = _required_by_order(review, clip, "rights-review.json")
        identities = {
            (
                str(item.get("stock_clip_id")),
                str(item.get("export_id")),
            )
            for item in (package_clip, evidence_clip, review_clip)
        }
        if len(identities) != 1:
            raise VClipError(f"Review identity conflict for clip {clip}.")
        return {
            "collection_slug": slug,
            "collection_version": resolved,
            "sort_order": clip,
            "customer_filename": package_clip["customer_filename"],
            "master_path": package_clip["master_path"],
            "duration_seconds": package_clip["duration_seconds"],
            "caption": public_clip["caption"],
            "tags": public_clip["tags"],
            "markets": public_clip["markets"],
            "machine_risk": evidence_risk(evidence_clip),
            "machine_evidence": evidence_clip["observations"],
            "human_confirmed_facts": review_clip["facts"],
            "capture_provenance": review_clip["capture_provenance"],
            "human_review": review_clip["human_review"],
            "classification": review_clip["classification"],
        }

    def confirm(self, **kwargs: Any) -> dict[str, Any]:
        version = resolve_collection_version(
            self.catalog, slug=kwargs["slug"], version=kwargs.pop("version")
        )
        return self.review_service.confirm(version=version, **kwargs)

    def validate(
        self,
        *,
        slug: str,
        version: int | None,
        release_root: Path,
    ) -> dict[str, Any]:
        resolved = resolve_collection_version(self.catalog, slug=slug, version=version)
        release_dir = release_directory(release_root, slug, resolved)
        package = load_package_release(release_dir)
        evidence = load_json(rights_evidence_path(release_dir))
        review = load_json(rights_review_path(release_dir))
        failures = [
            *validate_evidence_document(evidence, package),
            *validate_review_document(review, package),
        ]
        clip_results: list[dict[str, Any]] = []
        review_by_order = _by_order(review)
        for package_clip in package["clips"]:
            order = int(package_clip["sort_order"])
            entry = review_by_order.get(order)
            if entry is None:
                continue
            human_status = (entry.get("human_review") or {}).get("status")
            classification = (entry.get("classification") or {}).get("value")
            clip_failures: list[str] = []
            if human_status != "confirmed":
                clip_failures.append(
                    f"human_review.status is {human_status!r}; expected 'confirmed'"
                )
            if classification not in PASSING_CLASSIFICATIONS:
                clip_failures.append(
                    f"classification {classification!r} is not eligible for the standard package"
                )
            for failure in clip_failures:
                failures.append(f"clip {order}: {failure}")
            clip_results.append(
                {
                    "sort_order": order,
                    "stock_clip_id": package_clip["stock_clip_id"],
                    "export_id": package_clip["export_id"],
                    "human_review_status": human_status,
                    "classification": classification,
                    "ready": not clip_failures,
                    "failures": clip_failures,
                }
            )

        result = {
            "schema_version": 1,
            "status": "review_ready" if not failures else "not_review_ready",
            "review_ready": not failures,
            "collection_slug": slug,
            "collection_version": resolved,
            "clip_count": len(package["clips"]),
            "validated_at": utc_now(),
            "failures": failures,
            "clips": clip_results,
        }
        path = review_validation_path(release_dir)
        write_json(path, result)
        result["path"] = str(path)

        public_path = public_metadata_path(release_dir)
        if public_path.is_file():
            public = load_json(public_path)
            apply_public_rights(public, review, package)
            write_json(public_path, public)
        return result

    def _documents(
        self,
        slug: str,
        version: int | None,
        release_root: Path,
    ) -> tuple[
        int,
        Path,
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
    ]:
        resolved = resolve_collection_version(self.catalog, slug=slug, version=version)
        release_dir = release_directory(release_root, slug, resolved)
        return (
            resolved,
            release_dir,
            load_package_release(release_dir),
            load_json(public_metadata_path(release_dir)),
            load_json(rights_evidence_path(release_dir)),
            load_json(rights_review_path(release_dir)),
        )


def _by_order(document: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {
        int(item["sort_order"]): item
        for item in document.get("clips", [])
        if isinstance(item, dict)
    }


def _required_by_order(
    document: dict[str, Any],
    order: int,
    label: str,
) -> dict[str, Any]:
    item = _by_order(document).get(order)
    if item is None:
        raise VClipError(f"{label} has no clip with sort_order {order}.")
    return item
