"""Flight/trajectory location model for moving drone shoots.

A flight is a moving geographic trajectory. Source recordings and stock clips are
fragments of that flight and may inherit its resolved location.

Today flights are keyed by existing Stockify shoot sessions. The FlightIdentity
shape reserves an optional authoritative flight_id so DJI flight-log ingestion
can be added later without changing downstream consumers.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..geo import LocationResolver, haversine_meters, resolve_place
from .metadata import is_usable_gps, public_location_label

# Complete-linkage distance beyond which source recordings form separate
# geographic clusters inside one session (e.g. Charlottesville vs Blacksburg).
GEO_CLUSTER_SEPARATION_METERS = 50_000.0

# Clear municipality majority among place votes (Redmond 19 vs Bellevue 3).
MUNICIPALITY_MAJORITY_FRACTION = 2.0 / 3.0
MUNICIPALITY_MAJORITY_MIN_VOTES = 3


@dataclass(frozen=True)
class FlightIdentity:
    """Stable identity for one flight/session trajectory.

    Prefer flight_id when an authoritative flight log exists; otherwise use the
    Stockify session_id as the flight boundary.
    """

    run_id: str
    session_id: str
    flight_id: str | None = None

    @property
    def key(self) -> str:
        return self.flight_id or self.session_id


@dataclass
class TrajectorySample:
    """One GPS observation contributing to a flight trajectory."""

    latitude: float
    longitude: float
    source_key: str
    stock_clip_id: str | None = None
    source_media_id: str | None = None
    sample_count: int = 1
    source: str = "srt"
    filename: str | None = None


@dataclass
class PlaceSupport:
    label: str
    city: str | None
    neighborhood: str | None
    state: str | None
    country: str | None
    place: dict[str, object]
    source_count: int = 0
    source_keys: list[str] = field(default_factory=list)


@dataclass
class GeoCluster:
    """One geographically tight group of source recordings inside a session."""

    cluster_id: int
    source_keys: list[str]
    center_lat: float
    center_lon: float
    source_count: int
    place_labels: list[str] = field(default_factory=list)


@dataclass
class TrajectoryLocationResult:
    """Result of resolving a flight trajectory to a public location."""

    status: str  # resolved | conflict | multi_location | unresolved
    coherence: str  # neighborhood | city | county | conflict | multi_location | none
    location: dict[str, Any] | None = None
    place_support: list[PlaceSupport] = field(default_factory=list)
    contributing_clip_ids: list[str] = field(default_factory=list)
    contributing_source_keys: list[str] = field(default_factory=list)
    center_lat: float | None = None
    center_lon: float | None = None
    sample_count: int = 0
    flight_id: str | None = None
    session_id: str | None = None
    geo_clusters: list[GeoCluster] = field(default_factory=list)
    consensus_method: str | None = None

    def diagnostics(self) -> dict[str, Any]:
        municipality = _common_municipality(self.place_support, self.location)
        return {
            "status": self.status,
            "coherence": self.coherence,
            "consensus_method": self.consensus_method,
            "flight_id": self.flight_id,
            "session_id": self.session_id,
            "gps_source_files": len(self.contributing_source_keys),
            "gps_sample_count": self.sample_count,
            "center_lat": self.center_lat,
            "center_lon": self.center_lon,
            "resolved_neighborhoods": sorted(
                {
                    item.neighborhood
                    for item in self.place_support
                    if item.neighborhood
                }
            ),
            "resolved_cities": sorted(
                {
                    _municipality_label(item.city, item.state)
                    for item in self.place_support
                    if item.city
                }
            ),
            "common_municipality": municipality,
            "assigned": (self.location or {}).get("public_label"),
            "place_support": [
                {
                    "label": item.label,
                    "city": item.city,
                    "neighborhood": item.neighborhood,
                    "state": item.state,
                    "source_count": item.source_count,
                }
                for item in self.place_support
            ],
            "geo_clusters": [
                {
                    "cluster_id": cluster.cluster_id,
                    "source_count": cluster.source_count,
                    "center_lat": cluster.center_lat,
                    "center_lon": cluster.center_lon,
                    "source_keys": list(cluster.source_keys),
                    "place_labels": list(cluster.place_labels),
                }
                for cluster in self.geo_clusters
            ],
            "sample_count": self.sample_count,
        }


def resolve_flight_trajectory(
    samples: list[TrajectorySample],
    resolver: LocationResolver,
    *,
    identity: FlightIdentity | None = None,
    cluster_separation_meters: float = GEO_CLUSTER_SEPARATION_METERS,
) -> TrajectoryLocationResult:
    """Resolve a flight's GPS trajectory to the most specific coherent place."""
    usable = [
        sample
        for sample in samples
        if is_usable_gps(sample.latitude, sample.longitude)
    ]
    if not usable:
        return TrajectoryLocationResult(
            status="unresolved",
            coherence="none",
            flight_id=identity.flight_id if identity else None,
            session_id=identity.session_id if identity else None,
        )

    # One place vote per source recording/file, not per stock clip fragment.
    by_source: dict[str, list[TrajectorySample]] = defaultdict(list)
    for sample in usable:
        by_source[sample.source_key].append(sample)

    support_map: dict[str, PlaceSupport] = {}
    clip_ids: set[str] = set()
    all_lats: list[float] = []
    all_lons: list[float] = []
    total_samples = 0
    source_points: list[tuple[str, float, float]] = []
    source_place_label: dict[str, str] = {}

    for source_key, source_samples in by_source.items():
        lats = [float(item.latitude) for item in source_samples]
        lons = [float(item.longitude) for item in source_samples]
        lat = _median(lats)
        lon = _median(lons)
        source_points.append((source_key, lat, lon))
        all_lats.extend(lats)
        all_lons.extend(lons)
        total_samples += max(
            (max(1, int(item.sample_count)) for item in source_samples),
            default=1,
        )
        for item in source_samples:
            if item.stock_clip_id:
                clip_ids.add(str(item.stock_clip_id))

        place = resolve_place(resolver, lat, lon)
        place = _coerce_place_for_consensus(place)
        if place is None:
            continue
        label = public_location_label(place) or str(
            place.get("city") or place.get("county") or "Unknown"
        )
        source_place_label[source_key] = label
        support = support_map.get(label)
        if support is None:
            support_map[label] = PlaceSupport(
                label=label,
                city=_optional_str(place.get("city")),
                neighborhood=_optional_str(place.get("neighborhood")),
                state=_optional_str(place.get("state")),
                country=_optional_str(place.get("country")),
                place=dict(place),
                source_count=1,
                source_keys=[source_key],
            )
        else:
            support.source_count += 1
            support.source_keys.append(source_key)

    place_support = sorted(
        support_map.values(),
        key=lambda item: (-item.source_count, item.label),
    )
    center_lat = _median(all_lats) if all_lats else None
    center_lon = _median(all_lons) if all_lons else None
    geo_clusters = cluster_source_points(
        source_points,
        separation_meters=cluster_separation_meters,
        place_labels_by_source=source_place_label,
    )
    base = TrajectoryLocationResult(
        status="unresolved",
        coherence="none",
        place_support=place_support,
        contributing_clip_ids=sorted(clip_ids),
        contributing_source_keys=sorted(by_source),
        center_lat=center_lat,
        center_lon=center_lon,
        sample_count=total_samples,
        flight_id=identity.flight_id if identity else None,
        session_id=identity.session_id if identity else None,
        geo_clusters=geo_clusters,
    )

    # Distant GPS clusters inside one session are a structural conflict even
    # when place labels happen to agree or majority-vote toward one city.
    if len(geo_clusters) > 1:
        base.status = "multi_location"
        base.coherence = "multi_location"
        return base

    if not place_support:
        return base

    municipality_key, consensus_method = _select_municipality(place_support)
    if municipality_key is None:
        base.status = "conflict"
        base.coherence = "conflict"
        base.consensus_method = consensus_method
        return base

    members = [
        item
        for item in place_support
        if _canonical_city_key(item.city, item.state) == municipality_key
    ]
    # Prefer a row that already uses the parent city spelling (Charlottesville
    # over University of Virginia) when nested labels were collapsed.
    parent_members = [
        item
        for item in members
        if _city_key(item.city, item.state) == municipality_key
    ]
    primary = max(
        parent_members or members,
        key=lambda item: item.source_count,
    )
    display_city = primary.city
    display_state = primary.state
    if _city_key(primary.city, primary.state) != municipality_key:
        from ..geo import nested_municipality_parent

        parent = nested_municipality_parent(primary.city, primary.state)
        if parent is not None:
            display_city, display_state = parent
    neighborhoods = {
        item.neighborhood.casefold()
        for item in members
        if item.neighborhood
    }
    place_type = str(primary.place.get("place_type") or "city")
    if consensus_method == "compatible_nested":
        place_type = "city"

    if len(neighborhoods) == 1 and primary.neighborhood and place_type != "county":
        assigned_place = dict(primary.place)
        assigned_place["city"] = display_city
        assigned_place["state"] = display_state
        assigned = _location_from_place(
            assigned_place,
            center_lat=center_lat,
            center_lon=center_lon,
            sample_count=total_samples,
            coherence="neighborhood",
            identity=identity,
            place_support=place_support,
        )
        base.status = "resolved"
        base.coherence = "neighborhood"
        base.consensus_method = consensus_method
        base.location = assigned
        return base

    city_place = {
        "city": display_city,
        "state": display_state,
        "country": primary.country,
        "neighborhood": None,
        "poi": None,
        "timezone": primary.place.get("timezone"),
        "provider": primary.place.get("provider"),
        "aliases": primary.place.get("aliases") or [],
        "place_type": place_type,
        "county": primary.place.get("county"),
    }
    coherence = "county" if place_type == "county" else "city"
    assigned = _location_from_place(
        city_place,
        center_lat=center_lat,
        center_lon=center_lon,
        sample_count=total_samples,
        coherence=coherence,
        identity=identity,
        place_support=place_support,
    )
    base.status = "resolved"
    base.coherence = coherence
    base.consensus_method = consensus_method
    base.location = assigned
    return base


def cluster_source_points(
    source_points: list[tuple[str, float, float]],
    *,
    separation_meters: float = GEO_CLUSTER_SEPARATION_METERS,
    place_labels_by_source: dict[str, str] | None = None,
) -> list[GeoCluster]:
    """Complete-linkage clusters of source GPS medians."""
    if not source_points:
        return []
    labels = place_labels_by_source or {}
    clusters: list[list[tuple[str, float, float]]] = [
        [point] for point in source_points
    ]
    merged = True
    while merged and len(clusters) > 1:
        merged = False
        best: tuple[int, int] | None = None
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                if _complete_linkage_distance(clusters[i], clusters[j]) <= separation_meters:
                    best = (i, j)
                    break
            if best is not None:
                break
        if best is not None:
            i, j = best
            clusters[i] = clusters[i] + clusters[j]
            del clusters[j]
            merged = True

    results: list[GeoCluster] = []
    for index, members in enumerate(
        sorted(clusters, key=lambda items: (-len(items), items[0][0])),
        start=1,
    ):
        lats = [item[1] for item in members]
        lons = [item[2] for item in members]
        keys = sorted(item[0] for item in members)
        results.append(
            GeoCluster(
                cluster_id=index,
                source_keys=keys,
                center_lat=_median(lats),
                center_lon=_median(lons),
                source_count=len(members),
                place_labels=sorted(
                    {labels[key] for key in keys if key in labels}
                ),
            )
        )
    return results


def format_trajectory_diagnostics(
    result: TrajectoryLocationResult | dict[str, Any],
) -> list[str]:
    """Human-readable place-support block for diagnostics/recovery reports."""
    if isinstance(result, TrajectoryLocationResult):
        payload = result.diagnostics()
        support = [
            (item.label, item.source_count) for item in result.place_support
        ]
        coherence = result.coherence
        assigned = (result.location or {}).get("public_label")
    else:
        payload = result
        support = [
            (str(item.get("label") or "Unknown"), int(item.get("source_count") or 0))
            for item in (payload.get("place_support") or [])
        ]
        coherence = str(payload.get("coherence") or "none")
        assigned = payload.get("assigned")

    source_files = int(payload.get("gps_source_files") or 0)
    sample_count = int(payload.get("gps_sample_count") or payload.get("sample_count") or 0)
    center_lat = payload.get("center_lat")
    center_lon = payload.get("center_lon")
    neighborhoods = list(payload.get("resolved_neighborhoods") or [])
    cities = list(payload.get("resolved_cities") or [])
    municipality = payload.get("common_municipality")
    clusters = list(payload.get("geo_clusters") or [])
    consensus_method = payload.get("consensus_method")

    lines = [f"  GPS source files: {source_files}"]
    if sample_count:
        lines.append(f"  GPS samples: {sample_count}")
    if center_lat is not None and center_lon is not None:
        lines.append(f"  GPS center: {float(center_lat):.6f}, {float(center_lon):.6f}")
    if support:
        lines.append("  Resolved places:")
        width = max(len(label) for label, _count in support)
        for label, count in support:
            lines.append(f"    {label.ljust(width)}  {count}")
    if neighborhoods:
        lines.append(f"  Resolved neighborhoods: {', '.join(neighborhoods)}")
    if cities:
        lines.append(f"  Resolved cities: {', '.join(cities)}")
    if municipality:
        lines.append(f"  Common municipality: {municipality}")
    elif coherence in {"conflict", "multi_location"}:
        lines.append("  Common municipality: none (conflicting cities/regions)")
    if consensus_method:
        lines.append(f"  Consensus: {consensus_method}")
    if clusters:
        lines.append(f"  Geographic clusters: {len(clusters)}")
        for cluster in clusters:
            lines.append(
                "    "
                f"cluster {cluster.get('cluster_id')}: "
                f"{cluster.get('source_count')} sources @ "
                f"{float(cluster.get('center_lat')):.5f}, "
                f"{float(cluster.get('center_lon')):.5f}"
                + (
                    f" ({', '.join(cluster.get('place_labels') or [])})"
                    if cluster.get("place_labels")
                    else ""
                )
            )
    if coherence == "neighborhood":
        lines.append("  Result: coherent at neighborhood level")
    elif coherence == "city":
        lines.append("  Result: coherent at city level")
    elif coherence == "county":
        lines.append("  Result: coherent at county / administrative level")
    elif coherence == "multi_location":
        lines.append("  Result: multiple distant geographic clusters")
    elif coherence == "conflict":
        lines.append("  Result: conflicting cities/regions")
    else:
        lines.append("  Result: no resolvable place")
    if assigned:
        lines.append(f"  Final location: {assigned}")
    return lines


def _coerce_place_for_consensus(
    place: dict[str, object] | None,
) -> dict[str, object] | None:
    """Accept city or fallback administrative labels for trajectory voting."""
    if not place:
        return None
    city = _optional_str(place.get("city"))
    county = _optional_str(place.get("county"))
    if city:
        coerced = dict(place)
        coerced.setdefault("place_type", place.get("place_type") or "city")
        return coerced
    if county:
        coerced = dict(place)
        coerced["city"] = county
        coerced["county"] = county
        coerced["place_type"] = "county"
        return coerced
    return None


def _select_municipality(
    place_support: list[PlaceSupport],
) -> tuple[tuple[str, str] | None, str]:
    # Collapse nested campus labels into their parent city before voting so
    # University of Virginia + Charlottesville count as one municipality.
    votes: Counter[tuple[str, str]] = Counter()
    for item in place_support:
        if not item.city:
            continue
        votes[_canonical_city_key(item.city, item.state)] += item.source_count
    if not votes:
        return None, "none"
    best_key, best_count = votes.most_common(1)[0]
    total = sum(votes.values())
    if len(votes) == 1:
        method = "unanimous"
        # Distinguish pure nested collapse from a single raw label.
        raw_keys = {
            _city_key(item.city, item.state)
            for item in place_support
            if item.city
        }
        if len(raw_keys) > 1:
            method = "compatible_nested"
        return best_key, method
    if (
        best_count >= MUNICIPALITY_MAJORITY_MIN_VOTES
        and best_count / total >= MUNICIPALITY_MAJORITY_FRACTION
    ):
        return best_key, "majority"
    return None, "conflict"


def _canonical_city_key(city: str | None, state: str | None) -> tuple[str, str]:
    from ..geo import nested_municipality_parent

    key = _city_key(city, state)
    parent = nested_municipality_parent(city, state)
    if parent is None:
        return key
    return (parent[0].casefold(), parent[1].casefold())


def _complete_linkage_distance(
    left: list[tuple[str, float, float]],
    right: list[tuple[str, float, float]],
) -> float:
    return max(
        haversine_meters(a[1], a[2], b[1], b[2])
        for a in left
        for b in right
    )


def _location_from_place(
    place: dict[str, object],
    *,
    center_lat: float | None,
    center_lon: float | None,
    sample_count: int,
    coherence: str,
    identity: FlightIdentity | None,
    place_support: list[PlaceSupport],
) -> dict[str, Any]:
    label = public_location_label(place)
    if coherence in {"city", "county"} and place.get("city") and place.get("state"):
        label = f"{place['city']}, {place['state']}"
    elif coherence in {"city", "county"} and place.get("city"):
        label = str(place["city"])
    return {
        "status": "resolved",
        "confidence": "high" if coherence == "neighborhood" else "medium",
        "evidence_sources": ["flight_trajectory", "srt_gps"],
        "center_lat": center_lat,
        "center_lon": center_lon,
        "sample_count": sample_count,
        "valid_sample_count": sample_count,
        "radius_meters": None,
        "country": place.get("country"),
        "state": place.get("state"),
        "city": place.get("city"),
        "neighborhood": place.get("neighborhood"),
        "poi": place.get("poi"),
        "county": place.get("county"),
        "public_label": label,
        "timezone": place.get("timezone"),
        "place_provider": place.get("provider"),
        "private_precision": "flight_trajectory_internal_only",
        "review_required": False,
        "flight": {
            "flight_id": identity.flight_id if identity else None,
            "session_id": identity.session_id if identity else None,
            "coherence": coherence,
            "source_file_count": len(
                {key for item in place_support for key in item.source_keys}
            ),
            "place_support": [
                {"label": item.label, "source_count": item.source_count}
                for item in place_support
            ],
        },
    }


def _city_key(city: str | None, state: str | None) -> tuple[str, str]:
    return ((city or "").casefold(), (state or "").casefold())


def _municipality_label(city: str | None, state: str | None) -> str:
    if city and state:
        return f"{city}, {state}"
    return str(city or state or "Unknown")


def _common_municipality(
    place_support: list[PlaceSupport],
    location: dict[str, Any] | None = None,
) -> str | None:
    if location and location.get("city"):
        return _municipality_label(
            _optional_str(location.get("city")),
            _optional_str(location.get("state")),
        )
    key, _method = _select_municipality(place_support)
    if key is None:
        return None
    city, state = key
    # Recover original casing from support rows.
    for item in place_support:
        if _city_key(item.city, item.state) == key:
            return _municipality_label(item.city, item.state)
    return _municipality_label(city, state)


def _optional_str(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def majority_place_label(place_support: list[PlaceSupport]) -> str | None:
    if not place_support:
        return None
    counts = Counter({item.label: item.source_count for item in place_support})
    return counts.most_common(1)[0][0]


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
