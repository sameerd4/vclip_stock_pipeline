"""Machine-observed rights evidence compiled from stored visual analysis."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Protocol

from ..errors import VClipError
from ..util import json_loads, utc_now
from ..workflow.catalog import WorkflowCatalog
from .paths import (
    load_package_release,
    release_directory,
    rights_evidence_path,
    write_json,
)

SCHEMA_VERSION = 1
OBSERVATION_FIELDS = (
    "recognizable_people",
    "trademarks",
    "copyrighted_artwork",
    "identifiable_property",
    "identifying_information",
    "professional_event_content",
)
OBSERVATION_STATUSES = {"none_detected", "possible", "detected", "unknown"}
PROMINENCE_FIELDS = {
    "trademarks",
    "copyrighted_artwork",
    "identifiable_property",
}
PROMINENCE_VALUES = {"none", "incidental", "prominent", "unknown"}
FORBIDDEN_CONCLUSION_KEYS = {
    "legal_safe",
    "cleared",
    "approved",
    "release_required",
    "lawful_capture",
    "commercially_cleared",
    "non_infringing",
    "commercial_use_approved",
}


class RightsEvidenceProvider(Protocol):
    """Boundary for deterministic stored or future dedicated visual evidence."""

    provider_name: str
    prompt_version: str | None

    def observations_for_clip(
        self,
        *,
        run_id: str,
        clip_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return normalized observations and machine provenance."""


class StoredVisualRightsEvidenceProvider:
    """Read explicit rights observations from stored visual-analysis payloads.

    The current general visual prompt does not request rights observations.
    Therefore tags, captions, and named-subject guesses are not promoted into
    rights facts. Only an explicit ``raw.rights_evidence.observations`` payload
    is consumed; all unsupported categories remain ``unknown``.
    """

    provider_name = "stored_visual_analysis"
    prompt_version = None
    adapter_version = "stored-explicit-rights-evidence-v1"

    def __init__(self, catalog: WorkflowCatalog) -> None:
        self.catalog = catalog

    def observations_for_clip(
        self,
        *,
        run_id: str,
        clip_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        with self.catalog.database.connect() as connection:
            row = connection.execute(
                """
                SELECT analysis_key, provider, model, result_json, updated_at
                FROM clip_visual_analysis
                WHERE stockify_run_id=? AND stock_clip_id=? AND status='complete'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (run_id, clip_id),
            ).fetchone()

        if row is None:
            return unknown_observations(), {
                "analysis_found": False,
                "explicit_rights_payload_found": False,
            }

        result = json_loads(row["result_json"], {})
        raw = result.get("raw") if isinstance(result, dict) else None
        explicit = raw.get("rights_evidence") if isinstance(raw, dict) else None
        observations = explicit.get("observations") if isinstance(explicit, dict) else None
        provenance = {
            "analysis_found": True,
            "explicit_rights_payload_found": isinstance(observations, dict),
            "analysis_key": row["analysis_key"],
            "provider": row["provider"],
            "model": row["model"],
            "analysis_updated_at": row["updated_at"],
        }
        if not isinstance(observations, dict):
            return unknown_observations(), provenance
        _reject_legal_conclusions(observations)
        return normalize_observations(observations), provenance


class RightsEvidenceService:
    """Compile ``rights-evidence.json`` without making legal conclusions."""

    def __init__(
        self,
        catalog: WorkflowCatalog,
        *,
        provider: RightsEvidenceProvider | None = None,
    ) -> None:
        self.catalog = catalog
        self.provider = provider or StoredVisualRightsEvidenceProvider(catalog)

    def prepare(
        self,
        *,
        slug: str,
        version: int,
        release_root: Path,
    ) -> dict[str, Any]:
        release_dir = release_directory(release_root, slug, version)
        package = load_package_release(release_dir)
        clips: list[dict[str, Any]] = []
        for package_clip in package["clips"]:
            observations, provenance = self.provider.observations_for_clip(
                run_id=str(package_clip["stockify_run_id"]),
                clip_id=str(package_clip["stock_clip_id"]),
            )
            clips.append(
                {
                    "sort_order": int(package_clip["sort_order"]),
                    "stock_clip_id": package_clip["stock_clip_id"],
                    "export_id": package_clip["export_id"],
                    "customer_filename": package_clip["customer_filename"],
                    "observations": observations,
                    "source_analysis": provenance,
                }
            )

        document = {
            "schema_version": SCHEMA_VERSION,
            "collection_slug": package["collection_slug"],
            "collection_version": package["collection_version"],
            "clip_count": len(clips),
            "generated_at": utc_now(),
            "source": {
                "provider": self.provider.provider_name,
                "prompt_version": self.provider.prompt_version,
                "adapter_version": getattr(self.provider, "adapter_version", None),
                "mode": "stored_analysis_only",
                "network_calls": False,
                "principle": "observable_visual_evidence_only_no_legal_conclusions",
            },
            "clips": clips,
        }
        _reject_legal_conclusions(document)
        path = rights_evidence_path(release_dir)
        write_json(path, document)
        document["path"] = str(path)
        return document


def unknown_observations() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in OBSERVATION_FIELDS:
        item: dict[str, Any] = {
            "status": "unknown",
            "confidence": None,
            "notes": [],
        }
        if field in PROMINENCE_FIELDS:
            item["prominence"] = "unknown"
            item["candidates"] = []
        result[field] = item
    return result


def normalize_observations(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = unknown_observations()
    for field in OBSERVATION_FIELDS:
        raw = payload.get(field)
        if not isinstance(raw, dict):
            continue
        status = raw.get("status")
        normalized[field]["status"] = status if status in OBSERVATION_STATUSES else "unknown"
        confidence = raw.get("confidence")
        try:
            normalized[field]["confidence"] = (
                max(0.0, min(1.0, float(confidence))) if confidence is not None else None
            )
        except (TypeError, ValueError):
            normalized[field]["confidence"] = None
        notes = raw.get("notes")
        normalized[field]["notes"] = (
            [str(item) for item in notes if str(item).strip()] if isinstance(notes, list) else []
        )
        if field in PROMINENCE_FIELDS:
            prominence = raw.get("prominence")
            normalized[field]["prominence"] = (
                prominence if prominence in PROMINENCE_VALUES else "unknown"
            )
            candidates = raw.get("candidates")
            normalized[field]["candidates"] = (
                [deepcopy(item) for item in candidates] if isinstance(candidates, list) else []
            )
    return normalized


def evidence_risk(clip: dict[str, Any]) -> str:
    """Return deterministic triage only: LOW, REVIEW, HIGH, or UNKNOWN."""
    observations = clip.get("observations") or {}

    people = (observations.get("recognizable_people") or {}).get("status")
    event = (observations.get("professional_event_content") or {}).get("status")
    identifying = (observations.get("identifying_information") or {}).get("status")
    trademarks = observations.get("trademarks") or {}
    artwork = observations.get("copyrighted_artwork") or {}
    property_obs = observations.get("identifiable_property") or {}

    if people in {"possible", "detected"} or event in {"possible", "detected"}:
        return "HIGH"
    if identifying == "detected":
        return "HIGH"
    if trademarks.get("prominence") == "prominent":
        return "HIGH"
    if artwork.get("prominence") == "prominent":
        return "HIGH"

    if any(
        (observations.get(field) or {}).get("status") == "unknown" for field in OBSERVATION_FIELDS
    ):
        return "UNKNOWN"
    if any(
        (observations.get(field) or {}).get("status") == "none_detected"
        and not _reasonable_confidence((observations.get(field) or {}).get("confidence"))
        for field in OBSERVATION_FIELDS
    ):
        return "UNKNOWN"

    for item in (trademarks, artwork, property_obs):
        if item.get("status") in {"possible", "detected"} and item.get("prominence") in {
            "incidental",
            "none",
            "unknown",
        }:
            return "REVIEW"

    if all(
        (observations.get(field) or {}).get("status") == "none_detected"
        and _reasonable_confidence((observations.get(field) or {}).get("confidence"))
        for field in OBSERVATION_FIELDS
    ):
        return "LOW"
    return "REVIEW"


def validate_evidence_document(
    document: dict[str, Any],
    package: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    if document.get("schema_version") != SCHEMA_VERSION:
        failures.append(f"rights-evidence.json schema_version must be {SCHEMA_VERSION}")
    clips = document.get("clips")
    if not isinstance(clips, list):
        return [*failures, "rights-evidence.json clips must be a list"]
    if document.get("clip_count") != len(clips):
        failures.append(
            f"rights-evidence.json clip_count {document.get('clip_count')!r} does "
            f"not match clips length {len(clips)}"
        )
    try:
        _reject_legal_conclusions(document)
    except VClipError as exc:
        failures.append(str(exc))

    evidence_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for clip in clips:
        if not isinstance(clip, dict):
            failures.append("rights-evidence.json contains a non-object clip")
            continue
        key = (str(clip.get("stock_clip_id")), str(clip.get("export_id")))
        if key in evidence_by_pair:
            failures.append(f"duplicate rights-evidence identity {key[0]}/{key[1]}")
        evidence_by_pair[key] = clip
        observations = clip.get("observations")
        if not isinstance(observations, dict):
            failures.append(f"clip {key[0]}: observations must be an object")
            continue
        for field in OBSERVATION_FIELDS:
            item = observations.get(field)
            if not isinstance(item, dict):
                failures.append(f"clip {key[0]}: missing observation {field}")
                continue
            if item.get("status") not in OBSERVATION_STATUSES:
                failures.append(f"clip {key[0]}: invalid {field}.status {item.get('status')!r}")
            if field in PROMINENCE_FIELDS and item.get("prominence") not in PROMINENCE_VALUES:
                failures.append(
                    f"clip {key[0]}: invalid {field}.prominence {item.get('prominence')!r}"
                )

    for package_clip in package.get("clips", []):
        key = (
            str(package_clip.get("stock_clip_id")),
            str(package_clip.get("export_id")),
        )
        if key not in evidence_by_pair:
            failures.append(f"clip {key[0]}: missing rights-evidence entry for {key[1]}")
    return failures


def _reasonable_confidence(value: Any) -> bool:
    try:
        return float(value) >= 0.5
    except (TypeError, ValueError):
        return False


def _reject_legal_conclusions(payload: Any) -> None:
    if isinstance(payload, dict):
        forbidden = FORBIDDEN_CONCLUSION_KEYS.intersection(payload)
        if forbidden:
            raise VClipError(
                "Rights evidence contains prohibited legal-conclusion fields: "
                + ", ".join(sorted(forbidden))
            )
        for value in payload.values():
            _reject_legal_conclusions(value)
    elif isinstance(payload, list):
        for value in payload:
            _reject_legal_conclusions(value)
