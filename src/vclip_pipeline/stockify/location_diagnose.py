"""Read-only diagnostics for remaining Unknown Location sessions."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..db.repository import CatalogRepository
from ..errors import VClipError
from ..geo import LocationResolver
from .flight_location import (
    FlightIdentity,
    TrajectorySample,
    format_trajectory_diagnostics,
    resolve_flight_trajectory,
)
from .location_recovery import _has_gps, _is_unknown_session
from .metadata import extract_gps_summary, is_usable_gps
from .sidecars import normalized_stem, parse_srt_info
from .volume_index import (
    VolumeFileIndex,
    build_volume_file_index,
    first_existing_path,
)

EXAMPLE_FILENAME_LIMIT = 5
TRAJECTORY_SECTION_LIMIT = 12

# Internal reason codes → concise operator-facing labels.
REASON_LABELS: dict[str, str] = {
    "source_media_missing": "Missing original media / sidecar",
    "srt_missing": "Original media found, SRT missing",
    "srt_match_ambiguous": "Ambiguous SRT match",
    "srt_without_gps": "SRT found but no usable GPS",
    "conflicting_gps": "Conflicting GPS evidence",
    "multi_location_conflict": "Multiple distant location clusters",
    "place_resolution_failed": "Place lookup failed",
    "flight_gps_ready": "Flight GPS ready (run recover-locations)",
    "insufficient_evidence": "Other / insufficient evidence",
}

# Short labels for the biggest-groups table.
GROUP_REASON_LABELS: dict[str, str] = {
    "source_media_missing": "Original media not currently available",
    "srt_missing": "Original media found, SRT missing",
    "srt_match_ambiguous": "Ambiguous SRT match",
    "srt_without_gps": "SRT found but no usable GPS",
    "conflicting_gps": "Conflicting GPS evidence",
    "multi_location_conflict": "Multiple distant location clusters",
    "place_resolution_failed": "Place lookup failed",
    "flight_gps_ready": "Flight GPS ready (run recover-locations)",
    "insufficient_evidence": "Other / insufficient evidence",
}

# Sessions where finding another drive / SD / drone offload may help.
ACTIONABLE_REASONS = {
    "source_media_missing",
    "srt_missing",
    "srt_match_ambiguous",
}

REASON_ORDER = list(REASON_LABELS)


@dataclass
class LocationDiagnosticsReport:
    unknown_sessions: int = 0
    clips_affected: int = 0
    reason_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    groups: list[dict[str, Any]] = field(default_factory=list)
    actionable: list[dict[str, Any]] = field(default_factory=list)
    trajectory_notes: list[dict[str, Any]] = field(default_factory=list)
    sessions: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    verbose: bool = False


class LocationDiagnosticsService:
    """Explain remaining Unknown Location sessions without changing the catalog."""

    def __init__(
        self,
        repository: CatalogRepository,
        location_resolver: LocationResolver | None = None,
        *,
        scan_roots: Iterable[Path] | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.repository = repository
        self.location_resolver = location_resolver
        self.scan_roots = [Path(root) for root in (scan_roots or [Path("/Volumes")])]
        self.progress = progress
        self._srt_cache: dict[str, Any] = {}

    def run(self, *, verbose: bool = False) -> LocationDiagnosticsReport:
        runs = self.repository.list_stockify_runs(completed_only=True)
        if not runs:
            raise VClipError("The database does not contain a Stockify run.")

        library_by_run = self._library_names_by_run()
        unknown_rows: list[dict[str, Any]] = []
        needed_stems: set[str] = set()

        self._announce(f"Scanning {len(runs)} Stockify run(s) for unknown sessions.")
        for run in runs:
            run_id = str(run["id"])
            library_name = library_by_run.get(run_id) or _library_fallback(run)
            sessions = self.repository.sessions_for_run(run_id)
            candidates = self.repository.candidates_for_run(
                run_id,
                accepted_only=True,
            )
            by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for candidate in candidates:
                session_id = str(candidate.get("session_id") or "")
                if session_id:
                    by_session[session_id].append(candidate)

            for session in sessions:
                if not _is_unknown_session(session):
                    continue
                session_candidates = by_session.get(str(session["id"]), [])
                for candidate in session_candidates:
                    stem = _candidate_stem(candidate)
                    if stem:
                        needed_stems.add(stem)
                unknown_rows.append(
                    {
                        "run": run,
                        "library_name": library_name,
                        "session": session,
                        "candidates": session_candidates,
                    }
                )

        volume_index = self._build_volume_index(needed_stems)
        report = LocationDiagnosticsReport(
            unknown_sessions=len(unknown_rows),
            clips_affected=sum(len(row["candidates"]) for row in unknown_rows),
        )
        if volume_index.scan_errors:
            report.warnings.extend(volume_index.scan_errors)

        diagnosed: list[dict[str, Any]] = []
        for row in unknown_rows:
            diagnosed.append(
                self._diagnose_session(
                    library_name=str(row["library_name"]),
                    run=row["run"],
                    session=row["session"],
                    candidates=row["candidates"],
                    volume_index=volume_index,
                )
            )

        report.sessions = diagnosed
        report.reason_counts = _reason_totals(diagnosed)
        report.groups = _biggest_groups(diagnosed)
        report.actionable = [
            item
            for item in sorted(
                diagnosed,
                key=lambda item: (
                    -int(item["clip_count"]),
                    item["library_name"],
                    item["capture_date"] or "",
                ),
            )
            if item["reason_code"] in ACTIONABLE_REASONS
        ]
        report.trajectory_notes = _trajectory_notes(diagnosed)
        report.verbose = verbose
        return report

    def _diagnose_session(
        self,
        *,
        library_name: str,
        run: dict[str, Any],
        session: dict[str, Any],
        candidates: list[dict[str, Any]],
        volume_index: VolumeFileIndex,
    ) -> dict[str, Any]:
        sources = _unique_sources(candidates)
        media_found = 0
        srt_found = 0
        ambiguous = 0
        srt_without_gps = 0
        gps_points: list[tuple[float, float]] = []
        place_failed = 0
        filenames: list[str] = []
        trajectory_samples: list[TrajectorySample] = []

        for source in sources:
            stem = source["stem"]
            filename = source["filename"] or stem or "unknown"
            filenames.append(filename)

            media_path = source["media_path"]
            srt_path = source["srt_path"]
            media_on_disk = _path_exists(media_path) or bool(
                stem and volume_index.media_by_stem.get(stem)
            )
            srt_disk_path = first_existing_path(
                srt_path,
                *(volume_index.srt_by_stem.get(stem) or [] if stem else []),
            )
            srt_on_disk = srt_disk_path is not None
            if media_on_disk:
                media_found += 1
            if srt_on_disk:
                srt_found += 1

            if source["ambiguous"]:
                ambiguous += 1

            # Prefer live SRT re-parse so early (0,0) frames do not hide later GPS.
            live_gps = self._live_gps_from_srt(srt_disk_path)
            has_gps = bool(live_gps) or bool(source["has_gps"])
            center_lat = (
                live_gps["center_lat"]
                if live_gps
                else source["center_lat"]
            )
            center_lon = (
                live_gps["center_lon"]
                if live_gps
                else source["center_lon"]
            )
            if has_gps and is_usable_gps(center_lat, center_lon):
                gps_points.append((float(center_lat), float(center_lon)))
                trajectory_samples.append(
                    TrajectorySample(
                        latitude=float(center_lat),
                        longitude=float(center_lon),
                        source_key=str(source.get("key") or filename),
                        sample_count=int(
                            (live_gps or {}).get("valid_sample_count")
                            or (live_gps or {}).get("sample_count")
                            or 1
                        ),
                        source="srt_full" if live_gps else "catalog",
                        filename=str(filename),
                    )
                )
            if srt_on_disk and not has_gps:
                srt_without_gps += 1
            if has_gps and not source["has_place"]:
                place_failed += 1

        trajectory = None
        if self.location_resolver is not None and trajectory_samples:
            trajectory = resolve_flight_trajectory(
                trajectory_samples,
                self.location_resolver,
                identity=FlightIdentity(
                    run_id=str(run["id"]),
                    session_id=str(session["id"]),
                ),
            )

        reason_code = _classify_reason(
            source_count=len(sources),
            media_found=media_found,
            srt_found=srt_found,
            ambiguous=ambiguous,
            srt_without_gps=srt_without_gps,
            gps_points=gps_points,
            place_failed=place_failed,
            trajectory=trajectory,
        )
        capture_date = session.get("capture_date") or "Unknown Date"
        return {
            "stockify_run_id": str(run["id"]),
            "session_id": str(session["id"]),
            "library_name": _display_library_name(library_name),
            "capture_date": str(capture_date),
            "generated_event_name": session.get("generated_event_name"),
            "clip_count": len(candidates),
            "source_file_count": len(sources),
            "media_found": media_found,
            "srt_found": srt_found,
            "reason_code": reason_code,
            "reason_label": REASON_LABELS[reason_code],
            "group_reason_label": GROUP_REASON_LABELS[reason_code],
            "likely_action": _likely_action(
                reason_code,
                media_found=media_found,
                srt_found=srt_found,
                source_count=len(sources),
            ),
            "example_filenames": filenames[:EXAMPLE_FILENAME_LIMIT],
            "all_filenames": filenames,
            "filename_count": len(filenames),
            "trajectory": trajectory.diagnostics() if trajectory else None,
            "clip_details": [
                {
                    "stock_clip_id": candidate.get("stock_clip_id"),
                    "filename": candidate.get("source_filename")
                    or candidate.get("source_name"),
                    "media_path": candidate.get("source_media_path"),
                    "srt_path": candidate.get("sidecar_path")
                    or candidate.get("source_srt_path"),
                    "srt_match_method": candidate.get("source_srt_match_method"),
                }
                for candidate in candidates
            ],
        }

    def _library_names_by_run(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for row in self.repository.processed_libraries():
            name = str(row.get("library_name") or "")
            first = row.get("first_stockify_run_id")
            last = row.get("last_stockify_run_id")
            if first:
                mapping[str(first)] = name
            if last:
                mapping[str(last)] = name
        return mapping

    def _build_volume_index(self, needed_stems: set[str]) -> VolumeFileIndex:
        return build_volume_file_index(
            needed_stems,
            self.scan_roots,
            progress=self.progress,
        )

    def _live_gps_from_srt(self, srt_path: str | None) -> dict[str, object] | None:
        if not srt_path:
            return None
        try:
            srt_info = self._srt_cache.get(srt_path)
            if srt_info is None:
                srt_info = parse_srt_info(Path(srt_path))
                self._srt_cache[srt_path] = srt_info
        except OSError:
            return None
        return extract_gps_summary(srt_info, allow_full_sidecar_fallback=True)

    def _announce(self, message: str) -> None:
        if self.progress:
            self.progress(message)


def format_location_diagnostics_report(
    report: LocationDiagnosticsReport,
    *,
    verbose: bool = False,
) -> list[str]:
    """Render a concise, operator-facing diagnostics report."""
    lines: list[str] = [
        "LOCATION DIAGNOSTICS",
        "",
        f"Unknown sessions: {report.unknown_sessions}",
        f"Clips affected:   {report.clips_affected}",
        "",
        "WHY THEY ARE UNKNOWN",
        "",
    ]
    if report.unknown_sessions == 0:
        lines.append("No Unknown Location sessions remain in the catalog.")
        return lines

    width_sessions = max(
        (
            len(str(report.reason_counts.get(code, {}).get("sessions", 0)))
            for code in REASON_ORDER
        ),
        default=1,
    )
    width_clips = max(
        (
            len(str(report.reason_counts.get(code, {}).get("clips", 0)))
            for code in REASON_ORDER
        ),
        default=1,
    )
    label_width = max(len(label) for label in REASON_LABELS.values())
    for code in REASON_ORDER:
        counts = report.reason_counts.get(code) or {"sessions": 0, "clips": 0}
        if counts["sessions"] == 0 and counts["clips"] == 0:
            continue
        label = REASON_LABELS[code].ljust(label_width)
        lines.append(
            f"{label}  {counts['sessions']:>{width_sessions}} sessions   "
            f"{counts['clips']:>{width_clips}} clips"
        )

    lines.extend(["", "BIGGEST UNKNOWN GROUPS", ""])
    if not report.groups:
        lines.append("None.")
    else:
        current_library = None
        for group in report.groups:
            library = group["library_name"]
            if library != current_library:
                if current_library is not None:
                    lines.append("")
                lines.append(library)
                current_library = library
            date = str(group["capture_date"])
            clips = int(group["clip_count"])
            reason = group["group_reason_label"]
            lines.append(f"  {date:<18} {clips:>4} clips   {reason}")

    if report.trajectory_notes:
        lines.extend(["", "GPS TRAJECTORY ANALYSIS", ""])
        for index, item in enumerate(report.trajectory_notes):
            if index:
                lines.append("")
            lines.append(f"{item['library_name']} — {item['capture_date']}")
            lines.extend(format_trajectory_diagnostics(item.get("trajectory") or {}))

    lines.extend(["", "MAY REQUIRE ANOTHER DRIVE / SD CARD / DRONE OFFLOAD", ""])
    actionable = report.actionable
    if not actionable:
        lines.append(
            "None of the remaining unknowns look like simple missing media/SRT on an unmounted drive."
        )
    else:
        for index, item in enumerate(actionable):
            if index:
                lines.append("")
            lines.append(f"{item['library_name']} — {item['capture_date']}")
            lines.append(f"  {item['clip_count']} clips")
            lines.append(f"  Source files referenced: {item['source_file_count']}")
            lines.append(
                f"  Media currently found: {item['media_found']}/{item['source_file_count']}"
            )
            lines.append(
                f"  SRT currently found: {item['srt_found']}/{item['source_file_count']}"
            )
            lines.append(f"  Likely action: {item['likely_action']}")
            examples = item.get("example_filenames") or []
            total_names = int(item.get("filename_count") or len(examples))
            if examples:
                shown = ", ".join(examples)
                if total_names > len(examples):
                    lines.append(
                        f"  Example files: {shown} (+{total_names - len(examples)} more)"
                    )
                else:
                    lines.append(f"  Example files: {shown}")

    if verbose and report.sessions:
        lines.extend(["", "VERBOSE SESSION DETAIL", ""])
        for item in report.sessions:
            lines.append(
                f"{item['library_name']} — {item['capture_date']} "
                f"[{item['reason_code']}] {item['clip_count']} clips"
            )
            lines.append(f"  Run: {item['stockify_run_id']}")
            lines.append(f"  Session: {item['session_id']}")
            lines.append(f"  Reason: {item['reason_label']}")
            for filename in item.get("all_filenames") or item.get("example_filenames") or []:
                lines.append(f"  - {filename}")
            lines.append("")

    for warning in report.warnings:
        lines.append(f"warning: {warning}")
    return lines


def diagnostics_report_dict(report: LocationDiagnosticsReport) -> dict[str, Any]:
    return asdict(report)


def _classify_reason(
    *,
    source_count: int,
    media_found: int,
    srt_found: int,
    ambiguous: int,
    srt_without_gps: int,
    gps_points: list[tuple[float, float]],
    place_failed: int,
    trajectory: Any | None = None,
) -> str:
    # Only true city/region disagreement is a geographic conflict. Kilometer-scale
    # movement inside one city is normal drone flight behavior. Distant clusters
    # inside one session are a structural multi-location conflict.
    if trajectory is not None and trajectory.status == "multi_location":
        return "multi_location_conflict"
    if trajectory is not None and trajectory.status == "conflict":
        return "conflicting_gps"
    if trajectory is not None and trajectory.status == "resolved":
        # Coherent flight place exists on disk; catalog just hasn't been updated.
        return "flight_gps_ready"
    if gps_points and place_failed >= max(1, len(gps_points)):
        return "place_resolution_failed"
    if source_count == 0:
        return "insufficient_evidence"
    if ambiguous >= max(1, (source_count + 1) // 2) and srt_found == 0:
        return "srt_match_ambiguous"
    if srt_found > 0 and not gps_points and srt_without_gps >= srt_found:
        return "srt_without_gps"
    if media_found > 0 and srt_found == 0:
        if ambiguous > 0:
            return "srt_match_ambiguous"
        return "srt_missing"
    if media_found == 0 and srt_found == 0:
        return "source_media_missing"
    if media_found == 0 and srt_found > 0 and not gps_points:
        return "srt_without_gps"
    if gps_points and place_failed > 0:
        return "place_resolution_failed"
    return "insufficient_evidence"


def _trajectory_notes(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    notes = []
    for item in sessions:
        trajectory = item.get("trajectory") or {}
        support = trajectory.get("place_support") or []
        coherence = trajectory.get("coherence")
        if coherence == "conflict" or coherence in {"city", "neighborhood"} or support:
            notes.append(item)
    notes.sort(key=lambda item: (-int(item["clip_count"]), item["library_name"]))
    return notes[:TRAJECTORY_SECTION_LIMIT]


def _unique_sources(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        media_id = candidate.get("source_media_id")
        stem = _candidate_stem(candidate)
        key = str(media_id or stem or candidate.get("stock_clip_id"))
        location = candidate.get("location") or {}
        has_gps = _has_gps(location)
        has_place = bool(location.get("city") or location.get("public_label"))
        ambiguous = bool(candidate.get("source_srt_match_ambiguous")) or (
            candidate.get("source_srt_match_method") == "ambiguous"
        )
        srt_path = candidate.get("sidecar_path") or candidate.get("source_srt_path")
        entry = sources.get(key)
        if entry is None:
            sources[key] = {
                "key": key,
                "stem": stem,
                "filename": candidate.get("source_filename")
                or candidate.get("source_name"),
                "media_path": candidate.get("source_media_path"),
                "srt_path": srt_path,
                "ambiguous": ambiguous,
                "has_gps": has_gps,
                "has_place": has_place,
                "center_lat": location.get("center_lat"),
                "center_lon": location.get("center_lon"),
                "srt_has_position": candidate.get("source_srt_has_position"),
            }
            continue
        entry["ambiguous"] = entry["ambiguous"] or ambiguous
        entry["has_gps"] = entry["has_gps"] or has_gps
        entry["has_place"] = entry["has_place"] or has_place
        if entry["srt_path"] is None and srt_path:
            entry["srt_path"] = srt_path
        if entry["center_lat"] is None and has_gps:
            entry["center_lat"] = location.get("center_lat")
            entry["center_lon"] = location.get("center_lon")
    return list(sources.values())


def _candidate_stem(candidate: dict[str, Any]) -> str:
    stem = candidate.get("source_normalized_stem")
    if stem:
        return str(stem)
    for value in (
        candidate.get("source_filename"),
        candidate.get("source_media_path"),
        candidate.get("source_name"),
    ):
        stem = normalized_stem(str(value) if value else None)
        if stem:
            return stem
    return ""


def _path_exists(value: str | None) -> bool:
    if not value:
        return False
    try:
        return Path(value).expanduser().is_file()
    except OSError:
        return False


def _reason_totals(sessions: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    totals = {
        code: {"sessions": 0, "clips": 0}
        for code in REASON_ORDER
    }
    for item in sessions:
        code = item["reason_code"]
        totals[code]["sessions"] += 1
        totals[code]["clips"] += int(item["clip_count"])
    return totals


def _biggest_groups(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in sessions:
        key = (
            item["library_name"],
            str(item["capture_date"]),
            item["reason_code"],
        )
        bucket = grouped.get(key)
        if bucket is None:
            grouped[key] = {
                "library_name": item["library_name"],
                "capture_date": item["capture_date"],
                "reason_code": item["reason_code"],
                "group_reason_label": item["group_reason_label"],
                "clip_count": int(item["clip_count"]),
                "session_count": 1,
            }
        else:
            bucket["clip_count"] += int(item["clip_count"])
            bucket["session_count"] += 1

    ordered = sorted(
        grouped.values(),
        key=lambda item: (
            -int(item["clip_count"]),
            item["library_name"],
            str(item["capture_date"]),
        ),
    )
    # Keep library blocks contiguous while preserving global clip-desc priority:
    # sort libraries by their largest group, then dates inside each library.
    library_weight: Counter[str] = Counter()
    for item in ordered:
        library_weight[item["library_name"]] = max(
            library_weight[item["library_name"]],
            int(item["clip_count"]),
        )
    return sorted(
        ordered,
        key=lambda item: (
            -library_weight[item["library_name"]],
            item["library_name"],
            -int(item["clip_count"]),
            str(item["capture_date"]),
        ),
    )


def _likely_action(
    reason_code: str,
    *,
    media_found: int,
    srt_found: int,
    source_count: int,
) -> str:
    if reason_code == "srt_missing":
        return "locate corresponding SRT sidecars"
    if reason_code == "srt_match_ambiguous":
        return "resolve ambiguous SRT matches (find the correct sidecar set)"
    if reason_code == "flight_gps_ready":
        return "run vclip recover-locations to apply the coherent flight place"
    if reason_code == "multi_location_conflict":
        return (
            "review geographic clusters; split the project into per-location "
            "shoot sessions when ready (re-segmentation is opt-in)"
        )
    if reason_code == "conflicting_gps":
        return "review place votes / adjacent city labels in the trajectory report"
    if media_found == 0 and srt_found == 0:
        return "locate or offload original DJI files/SRTs"
    if media_found < source_count:
        return "locate or offload missing original DJI files/SRTs"
    return "locate missing archive media or SRT sidecars"


def _display_library_name(name: str) -> str:
    cleaned = name.strip()
    if cleaned.endswith(".fcpbundle"):
        return cleaned[: -len(".fcpbundle")]
    return cleaned or "Unknown library"


def _library_fallback(run: dict[str, Any]) -> str:
    source = Path(str(run.get("source_xml_path") or ""))
    for parent in (source, *source.parents):
        if parent.name.endswith(".fcpbundle"):
            return parent.name
    return source.stem or str(run.get("id") or "Unknown library")
