"""Editorial-group geographic context forensics (read-only).

Source-level SRT / JPG evidence is preserved independently. Editorial FCP
events are treated as groupings that may span multiple neighborhoods within
one city/metro, without forcing event splits from spatial clusters.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from ..geo import LocationResolver, resolve_place
from ..stockify.jpg_exif_same_shoot import EVIDENCE_SOURCE as JPG_EXIF_EVIDENCE_SOURCE
from ..stockify.metadata import haversine_meters, is_usable_gps
from ..stockify.sidecars import normalized_stem
from .camera_scope import (
    SCOPE_OUT_OF_SCOPE_NON_DRONE,
    classify_appearance_camera_scope,
    is_out_of_scope_non_drone,
)

EDITORIAL_CONSENSUS_EVIDENCE = "editorial_group_consensus"

# Approximate Puget Sound / Seattle metro municipalities.
_SEATTLE_METRO_CITIES = frozenset(
    {
        "seattle",
        "bellevue",
        "redmond",
        "kirkland",
        "renton",
        "tacoma",
        "everett",
        "lynnwood",
        "bothell",
        "issaquah",
        "sammamish",
        "shoreline",
        "tukwila",
        "seatac",
        "burien",
        "edmonds",
        "lake forest park",
        "kenmore",
        "woodinville",
        "newcastle",
        "mercer island",
        "des moines",
        "federal way",
        "auburn",
        "kent",
        "puyallup",
        "olympia",
        "bainbridge island",
    }
)

METRO_RADIUS_M = 55_000.0
REGION_RADIUS_M = 200_000.0


@dataclass
class SourceGeoEvidence:
    source_basename: str
    stem: str
    evidence_kind: str  # srt_gps | jpg_exif_same_shoot | none
    confidence: str | None = None
    review_required: bool = False
    latitude: float | None = None
    longitude: float | None = None
    neighborhood: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    public_label: str | None = None
    evidence_files: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    stock_clip_ids: list[str] = field(default_factory=list)

    @property
    def has_coordinates(self) -> bool:
        return is_usable_gps(self.latitude, self.longitude)

    @property
    def is_located(self) -> bool:
        return bool(self.city or self.public_label) or self.has_coordinates

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_basename": self.source_basename,
            "stem": self.stem,
            "evidence_kind": self.evidence_kind,
            "confidence": self.confidence,
            "review_required": self.review_required,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "neighborhood": self.neighborhood,
            "city": self.city,
            "state": self.state,
            "country": self.country,
            "public_label": self.public_label,
            "evidence_files": list(self.evidence_files),
            "stock_clip_ids": list(self.stock_clip_ids),
            "provenance": dict(self.provenance),
            "direct_source_gps": self.evidence_kind == "srt_gps",
        }


@dataclass
class EditorialGroupForensic:
    original_event_name: str
    stockify_run_id: str
    shard_paths: list[str]
    total_candidates: int
    unique_sources: int
    sources_with_srt_gps: int
    sources_with_jpg_gps: int
    still_unlocated_sources: int
    cities_represented: list[str]
    neighborhoods_represented: list[str]
    geographic_extent_meters: float | None
    geographic_coherence: str
    recommended_group_label: str | None
    recommended_label_level: str | None
    unknown_clips_eligible_to_inherit: list[str]
    inherit_eligible_source_count: int
    confidence: str
    contradictory_evidence: list[str]
    review_required: bool
    source_evidence: list[dict[str, Any]] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_source_geo_evidence(
    *,
    unknown_appearances: list[dict[str, Any]],
    srt_observations: dict[str, dict[str, Any]],
    jpg_observations: dict[str, dict[str, Any]],
    location_resolver: LocationResolver,
) -> dict[str, SourceGeoEvidence]:
    """Best available source-level evidence; does not force event agreement."""
    by_stem: dict[str, SourceGeoEvidence] = {}
    clip_ids_by_stem: dict[str, set[str]] = defaultdict(set)

    for appearance in unknown_appearances:
        stem = normalized_stem(appearance.get("source_basename"))
        if not stem:
            continue
        clip_ids_by_stem[stem].add(str(appearance["stock_clip_id"]))
        if stem in by_stem:
            continue
        basename = str(appearance.get("source_basename") or stem)
        if stem in srt_observations:
            obs = srt_observations[stem]
            place = resolve_place(
                location_resolver,
                float(obs["lat"]),
                float(obs["lon"]),
            )
            by_stem[stem] = _evidence_from_coords(
                basename=basename,
                stem=stem,
                lat=float(obs["lat"]),
                lon=float(obs["lon"]),
                evidence_kind="srt_gps",
                confidence="high",
                review_required=False,
                evidence_files=list(obs.get("srt_paths") or []),
                place=place,
                provenance={
                    "evidence_sources": ["srt_gps"],
                    "sample_count": obs.get("sample_count"),
                },
            )
            continue
        if stem in jpg_observations:
            obs = jpg_observations[stem]
            jpg_payload = obs.get("jpg_exif_same_shoot") or {}
            place = resolve_place(
                location_resolver,
                float(obs["lat"]),
                float(obs["lon"]),
            )
            evidence_files = [
                str(item.get("path"))
                for item in (jpg_payload.get("evidence_photos") or [])
                if item.get("path")
            ]
            by_stem[stem] = _evidence_from_coords(
                basename=basename,
                stem=stem,
                lat=float(obs["lat"]),
                lon=float(obs["lon"]),
                evidence_kind=JPG_EXIF_EVIDENCE_SOURCE,
                confidence=str(obs.get("resolution_confidence") or "medium"),
                review_required=bool(obs.get("review_required")),
                evidence_files=evidence_files,
                place=place,
                provenance={
                    "evidence_sources": [JPG_EXIF_EVIDENCE_SOURCE],
                    "jpg_exif_same_shoot": jpg_payload,
                    "direct_source_gps": False,
                },
            )
            continue
        by_stem[stem] = SourceGeoEvidence(
            source_basename=basename,
            stem=stem,
            evidence_kind="none",
            provenance={"evidence_sources": []},
        )

    for stem, evidence in by_stem.items():
        evidence.stock_clip_ids = sorted(clip_ids_by_stem.get(stem) or [])
    return by_stem


def analyze_editorial_groups(
    *,
    unknown_appearances: list[dict[str, Any]],
    source_evidence: dict[str, SourceGeoEvidence],
) -> tuple[list[EditorialGroupForensic], dict[str, Any]]:
    """Classify each Unknown editorial event and inheritance eligibility."""
    by_event: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for appearance in unknown_appearances:
        event_name = appearance.get("event_name") or "Unknown Location"
        by_event[(appearance["stockify_run_id"], event_name)].append(appearance)

    groups: list[EditorialGroupForensic] = []
    for (run_id, event_name), members in sorted(by_event.items()):
        stems = sorted(
            {
                normalized_stem(item.get("source_basename"))
                for item in members
                if normalized_stem(item.get("source_basename"))
            }
        )
        evidence_rows = [source_evidence[stem] for stem in stems if stem in source_evidence]
        located = [
            item
            for item in evidence_rows
            if item.evidence_kind != "none" and (item.city or item.has_coordinates)
        ]
        unlocated = [item for item in evidence_rows if item not in located]

        cities = sorted(
            {
                _city_state(item.city, item.state)
                for item in located
                if item.city
            }
        )
        neighborhoods = sorted(
            {
                f"{item.neighborhood}, {item.city}" if item.city else str(item.neighborhood)
                for item in located
                if item.neighborhood
            }
        )
        extent = _geographic_extent_meters(located)
        coherence, label, label_level, contradictions = classify_group_coherence(
            located
        )
        contradictions = list(contradictions) + _stale_event_label_contradictions(
            event_name=event_name,
            cities_represented=cities,
            located=located,
        )
        if contradictions and coherence not in {"mixed", "unresolved"}:
            review_required_from_contradiction = True
        else:
            review_required_from_contradiction = False
        inherit_ids, inherit_sources, confidence, review_required = (
            _inheritance_decision(
                coherence=coherence,
                label=label,
                label_level=label_level,
                located=located,
                unlocated=unlocated,
                members=members,
            )
        )
        review_required = review_required or review_required_from_contradiction
        shard_paths = sorted(
            {
                str(item.get("relative_xml") or "")
                for item in members
                if item.get("relative_xml")
            }
        )
        groups.append(
            EditorialGroupForensic(
                original_event_name=event_name,
                stockify_run_id=run_id,
                shard_paths=shard_paths,
                total_candidates=len(
                    {(item["stockify_run_id"], item["stock_clip_id"]) for item in members}
                ),
                unique_sources=len(stems),
                sources_with_srt_gps=sum(
                    1 for item in evidence_rows if item.evidence_kind == "srt_gps"
                ),
                sources_with_jpg_gps=sum(
                    1
                    for item in evidence_rows
                    if item.evidence_kind == JPG_EXIF_EVIDENCE_SOURCE
                ),
                still_unlocated_sources=len(unlocated),
                cities_represented=cities,
                neighborhoods_represented=neighborhoods,
                geographic_extent_meters=extent,
                geographic_coherence=coherence,
                recommended_group_label=label,
                recommended_label_level=label_level,
                unknown_clips_eligible_to_inherit=inherit_ids,
                inherit_eligible_source_count=inherit_sources,
                confidence=confidence,
                contradictory_evidence=contradictions,
                review_required=review_required,
                source_evidence=[item.as_dict() for item in evidence_rows],
                provenance={
                    "evidence_source": EDITORIAL_CONSENSUS_EVIDENCE,
                    "editorial_group": event_name,
                    "mutates_corpus": False,
                    "coordinates_inherited": False,
                    "note": (
                        "Group-level labels are editorial context only. "
                        "Inherited clips receive no fabricated precise GPS."
                    ),
                },
            )
        )

    summary = summarize_editorial_forensics(
        unknown_appearances=unknown_appearances,
        source_evidence=source_evidence,
        groups=groups,
    )
    return groups, summary


def classify_group_coherence(
    located: list[SourceGeoEvidence],
) -> tuple[str, str | None, str | None, list[str]]:
    """Return (coherence, recommended_label, label_level, contradictions)."""
    if not located:
        return "unresolved", None, None, []

    city_keys = {
        (_norm(item.city), _norm(item.state))
        for item in located
        if item.city
    }
    neighborhood_keys = {
        (_norm(item.neighborhood), _norm(item.city), _norm(item.state))
        for item in located
        if item.neighborhood and item.city
    }
    contradictions: list[str] = []
    extent = _geographic_extent_meters(located)

    # Coordinates without reverse-geocoded municipalities are not "mixed".
    # Keep them unresolved for labeling unless extent is clearly discontinuous.
    if not city_keys:
        notes = ["coords_without_place_labels"]
        if extent is not None:
            notes.append(f"extent_m={extent:.0f}")
        if extent is not None and extent > REGION_RADIUS_M:
            return "mixed", None, None, notes
        return "unresolved", None, None, notes

    # Single city.
    if len(city_keys) == 1:
        city, state = next(iter(city_keys))
        city_label = _display_city_state(city, state)
        if len(neighborhood_keys) == 1:
            neighborhood = next(iter(neighborhood_keys))[0]
            return (
                "neighborhood",
                f"{_title(neighborhood)}, {_title(city)}",
                "neighborhood",
                [],
            )
        if len(neighborhood_keys) > 1:
            # Multiple neighborhoods in one city remain one editorial city group.
            return "city", city_label, "city", []
        return "city", city_label, "city", []

    # Multiple cities — metro / region / mixed.
    if _same_metro(city_keys) and (extent is None or extent <= METRO_RADIUS_M):
        metro_label = _metro_label(city_keys)
        return "metro", metro_label, "metro", []

    states = {state for _, state in city_keys if state}
    countries = {_norm(item.country) for item in located if item.country}
    if len(countries) > 1:
        contradictions.append(
            "multiple_countries:" + ",".join(sorted(countries))
        )
        return "mixed", None, None, contradictions

    if len(states) == 1 and (extent is None or extent <= REGION_RADIUS_M):
        state = next(iter(states))
        contradictions.append(
            "multiple_cities_same_region:"
            + ",".join(sorted(_display_city_state(c, s) for c, s in city_keys))
        )
        # Broad region label only — not a city claim.
        return (
            "region",
            f"{_title(state)}, United States" if state else "United States",
            "region",
            contradictions,
        )

    contradictions.append(
        "discontinuous_cities:"
        + ",".join(sorted(_display_city_state(c, s) for c, s in city_keys))
    )
    if extent is not None:
        contradictions.append(f"extent_m={extent:.0f}")
    return "mixed", None, None, contradictions


def summarize_editorial_forensics(
    *,
    unknown_appearances: list[dict[str, Any]],
    source_evidence: dict[str, SourceGeoEvidence],
    groups: list[EditorialGroupForensic],
) -> dict[str, Any]:
    unknown_clip_keys = {
        (item["stockify_run_id"], item["stock_clip_id"]) for item in unknown_appearances
    }
    scope_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    out_of_scope_keys: set[tuple[str, str]] = set()
    family_counts: Counter[str] = Counter()
    for appearance in unknown_appearances:
        key = (appearance["stockify_run_id"], appearance["stock_clip_id"])
        scope = classify_appearance_camera_scope(appearance)
        scope_by_key[key] = scope
        family_counts[str(scope.get("camera_family") or "unknown")] += 1
        if is_out_of_scope_non_drone(scope):
            out_of_scope_keys.add(key)

    in_scope_unknown_keys = unknown_clip_keys - out_of_scope_keys

    clips_with_srt = set()
    clips_with_jpg = set()
    for appearance in unknown_appearances:
        key = (appearance["stockify_run_id"], appearance["stock_clip_id"])
        if key in out_of_scope_keys:
            continue
        stem = normalized_stem(appearance.get("source_basename"))
        evidence = source_evidence.get(stem)
        if evidence is None:
            continue
        if evidence.evidence_kind == "srt_gps":
            clips_with_srt.add(key)
        elif evidence.evidence_kind == JPG_EXIF_EVIDENCE_SOURCE:
            clips_with_jpg.add(key)

    inherit_clips = {
        clip_id
        for group in groups
        for clip_id in group.unknown_clips_eligible_to_inherit
    }
    # Map clip ids back to keys for counting (clip ids unique within forensic corpus).
    inherit_keys_all = {
        (item["stockify_run_id"], item["stock_clip_id"])
        for item in unknown_appearances
        if item["stock_clip_id"] in inherit_clips
    }
    inherit_keys = inherit_keys_all - out_of_scope_keys

    mixed_group_clips = set()
    only_city_metro_region = set()
    has_source_context = clips_with_srt | clips_with_jpg
    still_fully_unresolved_all = set()
    for appearance in unknown_appearances:
        key = (appearance["stockify_run_id"], appearance["stock_clip_id"])
        stem = normalized_stem(appearance.get("source_basename"))
        evidence = source_evidence.get(stem)
        in_inherit = key in inherit_keys_all
        located = evidence is not None and evidence.evidence_kind != "none"
        group = next(
            (
                item
                for item in groups
                if item.original_event_name == appearance.get("event_name")
                and item.stockify_run_id == appearance["stockify_run_id"]
            ),
            None,
        )
        if key not in out_of_scope_keys and group is not None:
            if group.geographic_coherence == "mixed":
                mixed_group_clips.add(key)
            if (
                group.recommended_label_level in {"city", "metro", "region"}
                and (located or in_inherit)
            ):
                only_city_metro_region.add(key)

        group_helps = (
            group is not None
            and group.geographic_coherence
            in {"neighborhood", "city", "metro", "region"}
            and (located or in_inherit)
        )
        if not located and not in_inherit and not group_helps:
            still_fully_unresolved_all.add(key)
        elif (
            not located
            and not in_inherit
            and group is not None
            and group.geographic_coherence in {"mixed", "unresolved"}
        ):
            still_fully_unresolved_all.add(key)

    still_fully_unresolved = still_fully_unresolved_all - out_of_scope_keys
    out_of_scope_unresolved = still_fully_unresolved_all & out_of_scope_keys

    unlabeled_gps = set()
    for item in unknown_appearances:
        key = (item["stockify_run_id"], item["stock_clip_id"])
        if key in out_of_scope_keys:
            continue
        evidence = source_evidence.get(normalized_stem(item.get("source_basename")))
        if (
            evidence is not None
            and evidence.has_coordinates
            and not evidence.city
            and not evidence.public_label
        ):
            unlabeled_gps.add(key)

    return {
        # Backlog metrics exclude out_of_scope_non_drone (Pocket / iPhone / etc.).
        "current_unknown_clips": len(in_scope_unknown_keys),
        "current_unknown_clips_including_out_of_scope": len(unknown_clip_keys),
        "clips_out_of_scope_non_drone": len(out_of_scope_keys),
        "out_of_scope_non_drone_clip_ids": sorted(
            clip_id for _run_id, clip_id in out_of_scope_keys
        ),
        "out_of_scope_non_drone_keys": [
            {
                "stockify_run_id": run_id,
                "stock_clip_id": clip_id,
                **scope_by_key[(run_id, clip_id)],
            }
            for run_id, clip_id in sorted(out_of_scope_keys)
        ],
        "camera_family_counts": dict(family_counts),
        "clips_with_direct_srt_gps_context": len(clips_with_srt),
        "clips_gaining_source_level_jpg_context": len(clips_with_jpg),
        "additional_clips_eligible_for_group_consensus": len(
            inherit_keys - has_source_context
        ),
        "clips_only_resolvable_to_city_metro_region": len(only_city_metro_region),
        "clips_in_genuinely_mixed_groups": len(mixed_group_clips),
        "clips_still_fully_unresolved": len(still_fully_unresolved),
        "clips_still_fully_unresolved_including_out_of_scope": len(
            still_fully_unresolved_all
        ),
        "clips_out_of_scope_non_drone_unresolved": len(out_of_scope_unresolved),
        "clips_with_gps_missing_place_labels": len(unlabeled_gps),
        "still_fully_unresolved_clip_ids": sorted(
            clip_id for _run_id, clip_id in still_fully_unresolved
        ),
        "still_fully_unresolved_keys": [
            {"stockify_run_id": run_id, "stock_clip_id": clip_id}
            for run_id, clip_id in sorted(still_fully_unresolved)
        ],
        "out_of_scope_non_drone_unresolved_keys": [
            {
                "stockify_run_id": run_id,
                "stock_clip_id": clip_id,
                **scope_by_key[(run_id, clip_id)],
            }
            for run_id, clip_id in sorted(out_of_scope_unresolved)
        ],
        "editorial_groups": len(groups),
        "coherence_counts": dict(
            Counter(item.geographic_coherence for item in groups)
        ),
        "evidence_source": EDITORIAL_CONSENSUS_EVIDENCE,
        "note": (
            "Source-level SRT/JPG evidence is independent of editorial grouping. "
            "Group consensus never fabricates precise GPS for inheriting clips. "
            f"Non-drone families are classified as {SCOPE_OUT_OF_SCOPE_NON_DRONE} "
            "and excluded from drone unknown-location backlog metrics without "
            "mutating catalog/XML provenance."
        ),
    }


def fill_missing_place_labels(
    source_evidence: dict[str, SourceGeoEvidence],
    location_resolver: LocationResolver,
) -> dict[str, Any]:
    """Batch reverse-geocode GPS points that still lack city/neighborhood labels.

    Coordinates and evidence_kind are preserved; only place labels / provenance
    annotations are filled when resolution succeeds.
    """
    attempted = 0
    labeled = 0
    failed = 0
    details: list[dict[str, Any]] = []
    for stem, evidence in sorted(source_evidence.items()):
        if not evidence.has_coordinates:
            continue
        if evidence.city or evidence.public_label:
            continue
        attempted += 1
        place = resolve_place(
            location_resolver,
            float(evidence.latitude),
            float(evidence.longitude),
        )
        if not place or not (place.get("city") or place.get("neighborhood")):
            failed += 1
            details.append(
                {
                    "stem": stem,
                    "source_basename": evidence.source_basename,
                    "latitude": evidence.latitude,
                    "longitude": evidence.longitude,
                    "status": "unresolved",
                    "evidence_kind": evidence.evidence_kind,
                }
            )
            continue
        evidence.neighborhood = (
            str(place.get("neighborhood") or place.get("locality") or "") or None
        )
        evidence.city = str(place.get("city") or "") or None
        evidence.state = str(place.get("state") or place.get("region") or "") or None
        evidence.country = str(place.get("country") or "") or None
        if place.get("public_label"):
            evidence.public_label = str(place["public_label"])
        elif evidence.neighborhood and evidence.city:
            evidence.public_label = f"{evidence.neighborhood}, {evidence.city}"
        elif evidence.city and evidence.state:
            evidence.public_label = f"{evidence.city}, {evidence.state}"
        else:
            evidence.public_label = evidence.city
        evidence.provenance = {
            **dict(evidence.provenance),
            "place_label_retry": {
                "provider": place.get("provider"),
                "match_confidence": place.get("match_confidence"),
                "gps_provenance_unchanged": True,
                "evidence_kind_unchanged": evidence.evidence_kind,
                "coordinates_unchanged": True,
            },
        }
        labeled += 1
        details.append(
            {
                "stem": stem,
                "source_basename": evidence.source_basename,
                "latitude": evidence.latitude,
                "longitude": evidence.longitude,
                "status": "labeled",
                "city": evidence.city,
                "neighborhood": evidence.neighborhood,
                "public_label": evidence.public_label,
                "evidence_kind": evidence.evidence_kind,
                "provider": place.get("provider"),
            }
        )
    return {
        "mode": "batch_reverse_geocode_retry",
        "attempted_sources": attempted,
        "labeled_sources": labeled,
        "failed_sources": failed,
        "details": details,
        "note": (
            "Place labels filled for GPS-bearing evidence that previously lacked "
            "city/neighborhood. Source GPS provenance and evidence_kind are unchanged."
        ),
    }


def _inheritance_decision(
    *,
    coherence: str,
    label: str | None,
    label_level: str | None,
    located: list[SourceGeoEvidence],
    unlocated: list[SourceGeoEvidence],
    members: list[dict[str, Any]],
) -> tuple[list[str], int, str, bool]:
    if coherence in {"mixed", "unresolved"} or not label or not label_level:
        return [], 0, "none", True
    if not located or not unlocated:
        # No inheritors; confidence reflects group itself.
        confidence = _group_confidence(coherence, located)
        return [], 0, confidence, confidence != "high"

    # Strong consensus: every located source supports the group label level,
    # with no city-level contradictions already filtered by coherence.
    if coherence not in {"neighborhood", "city", "metro", "region"}:
        return [], 0, "none", True

    # Require at least one located source; prefer >=2 for high inheritance confidence.
    if len(located) < 1:
        return [], 0, "none", True

    # Do not push neighborhood labels onto inheritors unless coherence is neighborhood.
    inherit_level = label_level
    if inherit_level == "neighborhood" and coherence != "neighborhood":
        inherit_level = "city"

    eligible_stems = {item.stem for item in unlocated}
    inherit_ids = sorted(
        {
            str(item["stock_clip_id"])
            for item in members
            if normalized_stem(item.get("source_basename")) in eligible_stems
        }
    )
    confidence = _group_confidence(coherence, located)
    # Region-level inheritance always review-required; metro with <3 sources too.
    review_required = (
        confidence != "high"
        or coherence in {"region", "metro"}
        or len(located) < 2
    )
    if coherence == "metro" and len(located) >= 3 and confidence == "high":
        review_required = True  # metro inheritance stays cautious
    return inherit_ids, len(eligible_stems), confidence, review_required


def _group_confidence(coherence: str, located: list[SourceGeoEvidence]) -> str:
    if not located:
        return "none"
    high_ratio = sum(1 for item in located if item.confidence == "high") / len(located)
    if coherence in {"neighborhood", "city"} and len(located) >= 2 and high_ratio >= 0.5:
        return "high"
    if coherence in {"neighborhood", "city"} and len(located) >= 1:
        return "medium"
    if coherence == "metro" and len(located) >= 2:
        return "medium"
    if coherence == "region":
        return "low"
    return "low"


def _evidence_from_coords(
    *,
    basename: str,
    stem: str,
    lat: float,
    lon: float,
    evidence_kind: str,
    confidence: str,
    review_required: bool,
    evidence_files: list[str],
    place: dict[str, Any] | None,
    provenance: dict[str, Any],
) -> SourceGeoEvidence:
    return SourceGeoEvidence(
        source_basename=basename,
        stem=stem,
        evidence_kind=evidence_kind,
        confidence=confidence,
        review_required=review_required,
        latitude=round(lat, 6),
        longitude=round(lon, 6),
        neighborhood=(
            str(place.get("neighborhood") or place.get("locality") or "") or None
            if place
            else None
        ),
        city=str(place.get("city") or "") or None if place else None,
        state=str(place.get("state") or place.get("region") or "") or None
        if place
        else None,
        country=str(place.get("country") or "") or None if place else None,
        public_label=(
            str(place.get("public_label") or "") or None
            if place and place.get("public_label")
            else (
                f"{place.get('neighborhood')}, {place.get('city')}"
                if place and place.get("neighborhood") and place.get("city")
                else (
                    f"{place.get('city')}, {place.get('state')}"
                    if place and place.get("city") and place.get("state")
                    else None
                )
            )
        ),
        evidence_files=list(evidence_files),
        provenance=dict(provenance),
    )


def _geographic_extent_meters(located: Iterable[SourceGeoEvidence]) -> float | None:
    points = [
        (float(item.latitude), float(item.longitude))
        for item in located
        if item.has_coordinates
    ]
    if len(points) < 2:
        return 0.0 if points else None
    farthest = 0.0
    for index, (lat1, lon1) in enumerate(points):
        for lat2, lon2 in points[index + 1 :]:
            farthest = max(farthest, haversine_meters(lat1, lon1, lat2, lon2))
    return farthest


def _same_metro(city_keys: set[tuple[str | None, str | None]]) -> bool:
    cities = {city for city, _state in city_keys if city}
    if not cities:
        return False
    if cities <= _SEATTLE_METRO_CITIES:
        return True
    return False


def _metro_label(city_keys: set[tuple[str | None, str | None]]) -> str:
    states = {state for _city, state in city_keys if state}
    state = next(iter(states)) if len(states) == 1 else None
    if cities := {city for city, _ in city_keys if city}:
        if cities <= _SEATTLE_METRO_CITIES:
            return (
                f"Seattle Metro, {_title(state)}"
                if state
                else "Seattle Metro"
            )
    return "Metro Area"


def _city_state(city: str | None, state: str | None) -> str:
    return _display_city_state(_norm(city), _norm(state))


def _display_city_state(city: str | None, state: str | None) -> str:
    if city and state:
        return f"{_title(city)}, {_title(state)}"
    return _title(city or state or "Unknown")


def _stale_event_label_contradictions(
    *,
    event_name: str,
    cities_represented: list[str],
    located: list[SourceGeoEvidence],
) -> list[str]:
    """Flag when an existing event place label conflicts with source GPS evidence."""
    if not located or not cities_represented:
        return []
    if "unknown location" in (event_name or "").casefold():
        return []
    implied = _implied_place_from_event_name(event_name)
    if not implied:
        return []
    implied_city, implied_state = implied
    source_cities = {
        (_norm(item.city), _norm(item.state))
        for item in located
        if item.city
    }
    if not source_cities:
        return []
    if any(
        city == implied_city and (not implied_state or state == implied_state)
        for city, state in source_cities
    ):
        return []
    # Same-metro soft matches still contradict a distant named city (e.g. Troutville
    # vs Fremont/Seattle), which is the stale-association case we need to surface.
    return [
        (
            "stale_event_label_contradicted_by_source_gps:"
            f"event={event_name}|sources="
            + ",".join(cities_represented)
        )
    ]


def _implied_place_from_event_name(
    event_name: str,
) -> tuple[str | None, str | None] | None:
    """Parse leading 'City, State — …' / 'City — …' event labels."""
    text = str(event_name or "").strip()
    if not text:
        return None
    head = text
    for sep in (" — ", " – ", "—", "–"):
        if sep in head:
            head = head.split(sep, 1)[0].strip()
            break
    if not head or "unknown" in head.casefold():
        return None
    if "," in head:
        city_part, state_part = head.split(",", 1)
        city = _norm(city_part)
        state = _norm(state_part)
        if city:
            return city, state
        return None
    city = _norm(head)
    return (city, None) if city else None


def _norm(value: str | None) -> str | None:
    if not value:
        return None
    return " ".join(str(value).casefold().split())


def _title(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(part.capitalize() for part in value.split())
