"""Design for geography-aware shoot-session re-segmentation.

Status: design only. Do not auto-split sessions from recover-locations yet.
Callers may use :func:`propose_session_splits` to inspect a dry proposal after a
``multi_location_conflict`` diagnosis.

Problem
-------
Stockify currently binds one shoot session per unknown project
(``unknown:{project_id}``). A single Final Cut project can contain source media
from multiple real shoots (e.g. UVA 1 with Charlottesville *and* Blacksburg
clips). Place consensus correctly refuses to invent one city, but operators still
need multiple shoot sessions for packaging, weather, and naming.

Goals
-----
1. Detect distant GPS clusters inside an existing session (already done via
   :func:`vclip_pipeline.stockify.flight_location.cluster_source_points`).
2. Propose one child shoot session per geographic cluster.
3. Preserve the original Final Cut / Stockify project relationship so exports and
   audits can still answer "which project did this come from?".
4. Keep re-segmentation opt-in and reviewable — never silently rewrite history.

Proposed data model
-------------------
Keep the existing ``shoot_sessions`` row as a *project session* (or mark it
``segmentation_status=multi_location_parent``). Add child sessions:

* ``session_key``: ``geo:{parent_session_id}:cluster:{n}``
* ``parent_session_id`` / ``source_project_id``: unchanged project link
* ``location_*``: resolved from that cluster's trajectory only
* ``segmentation`` JSON on parent and children::

    {
      "status": "proposed" | "applied" | "rejected",
      "method": "source_media_geography",
      "parent_session_id": "...",
      "cluster_id": 1,
      "source_keys": ["MEDIA_A", ...],
      "sibling_session_ids": ["...", "..."],
      "original_project_id": "PROJ_...",
      "original_session_id": "SESS_..."
    }

Clip / candidate reassignment
-----------------------------
* Stock candidates whose source media falls in cluster N move to child session N.
* Candidates without GPS stay on the parent (or follow majority cluster only when
  an operator confirms inheritance — default: leave on parent for review).
* Generated event/project labels regenerate from each child location; the
  original project ``source_name`` remains on ``source_projects``.

CLI / workflow (future)
-----------------------
1. ``diagnose-locations`` / ``recover-locations`` report ``multi_location_conflict``
   with cluster centers and source keys (implemented).
2. ``propose-session-splits --session SESS`` writes a proposal JSON (this module).
3. ``apply-session-splits --proposal path`` creates child sessions and rewires
   candidates inside a transaction (not implemented).
4. Package / weather continue to key off shoot sessions; parent multi-location
   rows stay non-packaged until split or manually located.

Invariants
----------
* Never delete the original project row.
* Never change ``source_projects.id`` / Final Cut UIDs.
* Splits are based on source-media GPS medians, not stock-clip trim windows.
* Clusters use the same separation threshold as trajectory conflict detection.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .flight_location import (
    GEO_CLUSTER_SEPARATION_METERS,
    GeoCluster,
    TrajectorySample,
    cluster_source_points,
    resolve_flight_trajectory,
)


@dataclass(frozen=True)
class SessionSplitProposal:
    """Dry-run proposal to turn one mixed session into geography-based children."""

    parent_session_id: str
    run_id: str
    original_project_id: str | None
    status: str  # proposed
    method: str = "source_media_geography"
    separation_meters: float = GEO_CLUSTER_SEPARATION_METERS
    clusters: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def propose_session_splits(
    *,
    run_id: str,
    parent_session_id: str,
    source_points: list[tuple[str, float, float]],
    original_project_id: str | None = None,
    place_labels_by_source: dict[str, str] | None = None,
    separation_meters: float = GEO_CLUSTER_SEPARATION_METERS,
    location_resolver: Any | None = None,
) -> SessionSplitProposal:
    """Build a reviewable split proposal from source-media GPS medians.

    This does not mutate the catalog. When ``location_resolver`` is provided,
    each cluster also gets a suggested public location from the flight
    trajectory consensus path.
    """
    clusters = cluster_source_points(
        source_points,
        separation_meters=separation_meters,
        place_labels_by_source=place_labels_by_source,
    )
    notes: list[str] = []
    if len(clusters) <= 1:
        notes.append(
            "Fewer than two distant clusters; re-segmentation is not recommended."
        )

    cluster_payloads: list[dict[str, Any]] = []
    for cluster in clusters:
        payload = _cluster_proposal_dict(cluster, parent_session_id)
        if location_resolver is not None:
            samples = [
                TrajectorySample(
                    latitude=lat,
                    longitude=lon,
                    source_key=key,
                    sample_count=1,
                    source="proposal",
                )
                for key, lat, lon in source_points
                if key in set(cluster.source_keys)
            ]
            trajectory = resolve_flight_trajectory(
                samples,
                location_resolver,
                cluster_separation_meters=separation_meters,
            )
            payload["suggested_location"] = (trajectory.location or {}).get(
                "public_label"
            )
            payload["trajectory_status"] = trajectory.status
            payload["trajectory_coherence"] = trajectory.coherence
        cluster_payloads.append(payload)

    return SessionSplitProposal(
        parent_session_id=parent_session_id,
        run_id=run_id,
        original_project_id=original_project_id,
        status="proposed",
        separation_meters=separation_meters,
        clusters=cluster_payloads,
        notes=notes,
    )


def _cluster_proposal_dict(
    cluster: GeoCluster,
    parent_session_id: str,
) -> dict[str, Any]:
    return {
        "cluster_id": cluster.cluster_id,
        "proposed_session_key": (
            f"geo:{parent_session_id}:cluster:{cluster.cluster_id}"
        ),
        "source_count": cluster.source_count,
        "center_lat": cluster.center_lat,
        "center_lon": cluster.center_lon,
        "source_keys": list(cluster.source_keys),
        "place_labels": list(cluster.place_labels),
        "preserves_project_relationship": True,
    }
