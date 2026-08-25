"""Machine-observed rights evidence compiled from stored visual analysis."""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any, Protocol

from ..errors import VClipError
from ..util import json_loads, stable_id, utc_now
from ..workflow.catalog import WorkflowCatalog
from ..workflow.enrichment import UsageTotals
from ..workflow.frames import FrameSampler
from ..workflow.models import FrameSampleSet
from .paths import (
    load_json,
    load_package_release,
    public_metadata_path,
    release_directory,
    rights_evidence_path,
    write_json,
)
from .rights_frames import (
    RIGHTS_SAMPLER_VERSION,
    rights_sample_timestamps,
    rights_sampler_config,
    rights_sampling_identity,
)

SCHEMA_VERSION = 1
PROMPT_VERSION = "rights-evidence-v1.1"
DEFAULT_MODEL = "gpt-5-mini"
CACHE_KIND = "rights-evidence-v1"
SAMPLED_FRAMES_LIMITATION = (
    "These are sampled frames, not the complete video. Absence from the sampled "
    "frames does not prove absence from the complete video."
)
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


def machine_evidence_identity(
    *,
    export_sha256: str,
    provider: str,
    model: str,
    duration_seconds: float,
) -> dict[str, Any]:
    return {
        "export_sha256": export_sha256,
        "provider": provider,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "sampler": rights_sampling_identity(duration_seconds),
    }


def machine_evidence_cache_key(identity: dict[str, Any]) -> str:
    return stable_id("RIGHTSEVIDENCE", json.dumps(identity, sort_keys=True))


class RightsEvidenceService:
    """Compile ``rights-evidence.json`` without making legal conclusions."""

    def __init__(
        self,
        catalog: WorkflowCatalog,
        *,
        provider: RightsEvidenceProvider | None = None,
        analyzer: Any | None = None,
        sample_frames: Callable[..., FrameSampleSet] | None = None,
    ) -> None:
        self.catalog = catalog
        self.provider = provider or StoredVisualRightsEvidenceProvider(catalog)
        self.analyzer = analyzer
        self.sample_frames = sample_frames

    def prepare(
        self,
        *,
        slug: str,
        version: int,
        release_root: Path,
        provider: str = "existing",
        model: str = DEFAULT_MODEL,
        cache_root: Path | None = None,
        refresh: bool = False,
        clip: int | None = None,
    ) -> dict[str, Any]:
        if provider not in {"existing", "openai"}:
            raise VClipError(f"Unsupported rights-evidence provider: {provider!r}")
        if provider == "openai" and cache_root is None:
            raise VClipError("--cache is required when --provider openai.")

        release_dir = release_directory(release_root, slug, version)
        package = load_package_release(release_dir)
        selected = _require_sort_order(package, clip)
        existing_by_pair = (
            _existing_evidence_by_identity(release_dir) if selected is not None else {}
        )
        public_by_order = _public_by_order(release_dir)
        cache_dir = (
            cache_root.expanduser().resolve() / CACHE_KIND if cache_root is not None else None
        )
        usage_totals = UsageTotals()
        openai_requests = 0
        cached_clips = 0
        sampled_frame_total = 0
        used_rights_cache = False
        clips: list[dict[str, Any]] = []

        for package_clip in package["clips"]:
            order = int(package_clip["sort_order"])
            if selected is not None and order != selected:
                clips.append(_retained_or_unknown_clip(package_clip, existing_by_pair))
                continue
            prepared, stats = self._prepare_clip(
                package_clip,
                provider=provider,
                model=model,
                cache_dir=cache_dir,
                refresh=refresh,
                public_by_order=public_by_order,
            )
            clips.append(prepared)
            openai_requests += int(stats["openai_requests"])
            cached_clips += int(stats["cached"])
            sampled_frame_total += int(stats["sampled_frames"])
            used_rights_cache = used_rights_cache or bool(stats["used_rights_cache"])
            if stats["usage"] is not None:
                usage_totals.add(stats["usage"])

        source: dict[str, Any] = {
            "provider": provider,
            "model": model if provider == "openai" or used_rights_cache else None,
            "prompt_version": (
                PROMPT_VERSION
                if provider == "openai" or used_rights_cache
                else self.provider.prompt_version
            ),
            "schema_version": SCHEMA_VERSION,
            "sampler_version": (
                RIGHTS_SAMPLER_VERSION if provider == "openai" or used_rights_cache else None
            ),
            "adapter_version": getattr(self.provider, "adapter_version", None),
            "mode": (
                "openai_sampled_stills" if provider == "openai" else "existing_cache_or_unknown"
            ),
            "network_calls": openai_requests > 0,
            "openai_requests": openai_requests,
            "cached_clips": cached_clips,
            "sampled_frame_count_total": sampled_frame_total,
            "image_only": True,
            "video_inputs": False,
            "sampled_frames_limitation": SAMPLED_FRAMES_LIMITATION,
            "principle": "observable_visual_evidence_only_no_legal_conclusions",
            "usage": _usage_totals_payload(usage_totals),
        }
        document = {
            "schema_version": SCHEMA_VERSION,
            "collection_slug": package["collection_slug"],
            "collection_version": package["collection_version"],
            "clip_count": len(clips),
            "generated_at": utc_now(),
            "source": source,
            "clips": clips,
        }
        _reject_legal_conclusions(document)
        path = rights_evidence_path(release_dir)
        write_json(path, document)
        document["path"] = str(path)
        return document

    def _prepare_clip(
        self,
        package_clip: dict[str, Any],
        *,
        provider: str,
        model: str,
        cache_dir: Path | None,
        refresh: bool,
        public_by_order: dict[int, dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        duration = float(package_clip["duration_seconds"])
        export_sha = str(package_clip["master_sha256"])
        identity = machine_evidence_identity(
            export_sha256=export_sha,
            provider="openai",
            model=model,
            duration_seconds=duration,
        )
        cache_key = machine_evidence_cache_key(identity)
        cached = _read_rights_cache(cache_dir, cache_key, identity) if cache_dir else None
        stats = {
            "openai_requests": 0,
            "cached": 0,
            "sampled_frames": 0,
            "used_rights_cache": False,
            "usage": None,
        }

        if provider == "existing":
            if cached is not None:
                stats["cached"] = 1
                stats["used_rights_cache"] = True
                return _clip_entry(package_clip, cached), stats
            observations, provenance = self.provider.observations_for_clip(
                run_id=str(package_clip["stockify_run_id"]),
                clip_id=str(package_clip["stock_clip_id"]),
            )
            return _clip_entry(
                package_clip,
                {
                    "observations": observations,
                    "provenance": provenance,
                },
            ), stats

        if cached is not None and not refresh:
            stats["cached"] = 1
            stats["used_rights_cache"] = True
            return _clip_entry(package_clip, cached), stats

        samples = self._sample_clip(
            package_clip,
            duration=duration,
            export_sha256=export_sha,
            cache_dir=cache_dir,
            overwrite=refresh,
        )
        stats["sampled_frames"] = len(samples.frames)
        context = _clip_context(
            package_clip, public_by_order, sampled_frame_count=len(samples.frames)
        )
        result = self._get_analyzer(model).analyze(samples.frames, context=context)
        stats["openai_requests"] = 1
        stats["used_rights_cache"] = True
        stats["usage"] = result.usage
        generated_at = utc_now()
        timestamps = rights_sample_timestamps(duration, samples.positions)
        payload = {
            "cache_key": cache_key,
            "identity": identity,
            "observations": result.observations,
            "generated_at": generated_at,
            "usage": result.usage.as_dict() if result.usage is not None else None,
            "provenance": {
                "provider": "openai",
                "model": model,
                "prompt_version": PROMPT_VERSION,
                "schema_version": SCHEMA_VERSION,
                "sampler_version": RIGHTS_SAMPLER_VERSION,
                "export_sha256": export_sha,
                "generated_at": generated_at,
                "sampled_frame_count": len(samples.frames),
                "sample_positions": list(samples.positions),
                "sample_timestamps_seconds": list(timestamps),
                "frame_cache_key": samples.cache_key,
                "sampled_frames_limitation": SAMPLED_FRAMES_LIMITATION,
                "image_only": True,
                "video_inputs": False,
                "cache_key": cache_key,
                "cached": False,
            },
        }
        if cache_dir is not None:
            _write_rights_cache(cache_dir, cache_key, payload)
        return _clip_entry(package_clip, payload), stats

    def _sample_clip(
        self,
        package_clip: dict[str, Any],
        *,
        duration: float,
        export_sha256: str,
        cache_dir: Path | None,
        overwrite: bool,
    ) -> FrameSampleSet:
        master_path = Path(str(package_clip["master_path"]))
        if not master_path.is_file():
            raise VClipError(f"Master export is missing for rights sampling: {master_path}")
        if self.sample_frames is not None:
            return self.sample_frames(
                master_path,
                export_sha256=export_sha256,
                duration_seconds=duration,
                overwrite=overwrite,
            )
        if cache_dir is None:
            raise VClipError("--cache is required when sampling rights-review frames.")
        sampler = FrameSampler(cache_dir / "frames", rights_sampler_config(duration))
        return sampler.sample(
            master_path,
            export_sha256=export_sha256,
            overwrite=overwrite,
        )

    def _get_analyzer(self, model: str) -> Any:
        if self.analyzer is not None:
            return self.analyzer
        from ..workflow.providers.openai_rights import OpenAIRightsEvidenceAnalyzer

        self.analyzer = OpenAIRightsEvidenceAnalyzer(model=model)
        return self.analyzer


def _clip_entry(package_clip: dict[str, Any], cached: dict[str, Any]) -> dict[str, Any]:
    provenance = deepcopy(cached.get("provenance") or {})
    if cached.get("cache_key"):
        provenance.setdefault("cache_key", cached["cache_key"])
    if cached.get("generated_at"):
        provenance.setdefault("generated_at", cached["generated_at"])
    if cached.get("usage") is not None:
        provenance.setdefault("usage", cached["usage"])
    return {
        "sort_order": int(package_clip["sort_order"]),
        "stock_clip_id": package_clip["stock_clip_id"],
        "export_id": package_clip["export_id"],
        "customer_filename": package_clip["customer_filename"],
        "observations": deepcopy(cached["observations"]),
        "source_analysis": provenance,
    }


def _clip_context(
    package_clip: dict[str, Any],
    public_by_order: dict[int, dict[str, Any]],
    *,
    sampled_frame_count: int,
) -> dict[str, Any]:
    public_clip = public_by_order.get(int(package_clip["sort_order"]), {})
    markets = public_clip.get("markets") if isinstance(public_clip, dict) else None
    market_label = None
    if isinstance(markets, list) and markets:
        market_label = str(markets[0])
    caption = public_clip.get("caption") if isinstance(public_clip, dict) else None
    caption = str(caption).strip() if caption else None
    return {
        "duration_seconds": float(package_clip["duration_seconds"]),
        "caption": caption or None,
        "market_label": market_label,
        "sampled_frame_count": sampled_frame_count,
        "sampled_frames_limitation": SAMPLED_FRAMES_LIMITATION,
    }


def _public_by_order(release_dir: Path) -> dict[int, dict[str, Any]]:
    path = public_metadata_path(release_dir)
    if not path.is_file():
        return {}
    document = load_json(path)
    result: dict[int, dict[str, Any]] = {}
    for item in document.get("clips", []):
        if isinstance(item, dict) and item.get("sort_order") is not None:
            result[int(item["sort_order"])] = item
    return result


def _require_sort_order(package: dict[str, Any], clip: int | None) -> int | None:
    if clip is None:
        return None
    orders = {int(item["sort_order"]) for item in package.get("clips", [])}
    if clip not in orders:
        raise VClipError(f"Collection has no clip with sort_order {clip}.")
    return clip


def _existing_evidence_by_identity(release_dir: Path) -> dict[tuple[str, str], dict[str, Any]]:
    path = rights_evidence_path(release_dir)
    if not path.is_file():
        return {}
    try:
        document = load_json(path)
    except VClipError:
        return {}
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for item in document.get("clips", []):
        if not isinstance(item, dict):
            continue
        stock_id = str(item.get("stock_clip_id") or "").strip()
        export_id = str(item.get("export_id") or "").strip()
        if not stock_id or not export_id:
            continue
        result[(stock_id, export_id)] = item
    return result


def _retained_or_unknown_clip(
    package_clip: dict[str, Any],
    existing_by_pair: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    key = (str(package_clip["stock_clip_id"]), str(package_clip["export_id"]))
    existing = existing_by_pair.get(key)
    if existing is not None and isinstance(existing.get("observations"), dict):
        retained = deepcopy(existing)
        retained["sort_order"] = int(package_clip["sort_order"])
        retained["customer_filename"] = package_clip["customer_filename"]
        return retained
    return {
        "sort_order": int(package_clip["sort_order"]),
        "stock_clip_id": package_clip["stock_clip_id"],
        "export_id": package_clip["export_id"],
        "customer_filename": package_clip["customer_filename"],
        "observations": unknown_observations(),
        "source_analysis": {
            "analysis_found": False,
            "explicit_rights_payload_found": False,
        },
    }


def _read_rights_cache(
    cache_dir: Path | None,
    cache_key: str,
    identity: dict[str, Any],
) -> dict[str, Any] | None:
    if cache_dir is None:
        return None
    path = cache_dir / f"{cache_key}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("identity") != identity:
        return None
    if not isinstance(payload.get("observations"), dict):
        return None
    payload = deepcopy(payload)
    provenance = payload.setdefault("provenance", {})
    provenance["cached"] = True
    return payload


def _write_rights_cache(cache_dir: Path, cache_key: str, payload: dict[str, Any]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{cache_key}.json"
    stored = deepcopy(payload)
    provenance = stored.setdefault("provenance", {})
    provenance["cached"] = False
    path.write_text(json.dumps(stored, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _usage_totals_payload(totals: UsageTotals) -> dict[str, Any] | None:
    if totals.requests <= 0:
        return None
    return {
        "requests": totals.requests,
        "input_tokens": totals.input_tokens,
        "cached_input_tokens": totals.cached_input_tokens,
        "output_tokens": totals.output_tokens,
        "reasoning_tokens": totals.reasoning_tokens,
        "total_tokens": totals.total_tokens,
        "estimated_total_cost_usd": totals.estimated_cost_usd,
        "missing_usage_responses": totals.missing_usage_responses,
        "unpriced_requests": totals.unpriced_requests,
    }


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
    # Prominent identifiable property alone is REVIEW, not HIGH.

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
