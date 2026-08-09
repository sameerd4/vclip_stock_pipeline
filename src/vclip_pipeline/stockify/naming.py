"""Generate concise Final Cut and package-facing names from factual metadata."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from ..util import safe_filename
from .domain import ProjectBuild


TIME_LABELS = {
    "morning_golden_hour": "Morning",
    "morning": "Morning",
    "midday": "Midday",
    "afternoon": "Afternoon",
    "evening_golden_hour": "Evening",
    "blue_hour": "Blue Hour",
    "night": "Night",
    "unknown": "Footage",
    None: "Footage",
}


def location_area(location: dict[str, object]) -> str:
    """Choose the shortest useful area name for a project label."""
    poi = location.get("poi")
    neighborhood = location.get("neighborhood")
    city = location.get("city")
    if poi:
        return str(poi)
    if neighborhood:
        if str(neighborhood).lower() == "downtown" and city:
            return f"Downtown {city}"
        return str(neighborhood)
    return str(city or "Unknown Location")


def public_location(location: dict[str, object]) -> str:
    """Choose a safe public location for an event name."""
    if location.get("public_label"):
        return str(location["public_label"])
    neighborhood = location.get("neighborhood")
    city = location.get("city")
    state = location.get("state")
    if neighborhood and city:
        if str(neighborhood).lower() == "downtown":
            return f"Downtown {city}"
        return f"{neighborhood}, {city}"
    if city and state:
        return f"{city}, {state}"
    return str(city or state or "Unknown Location")


def project_base_label(location: dict[str, object], time_of_day: dict[str, object]) -> str:
    """Build a short review/package label without using old creative names."""
    area = location_area(location)
    time_label = TIME_LABELS.get(time_of_day.get("label"), "Footage")
    return safe_filename(f"{area} {time_label}")


def event_base_name(location: dict[str, object], capture_time: dict[str, object]) -> str:
    """Build the generated event's location-and-date identity."""
    date = capture_time.get("date") or "Unknown Date"
    return safe_filename(f"{public_location(location)} — {date}")


def disambiguate_event_names(
    sessions: list[dict[str, object]],
) -> None:
    """Add a time label only when multiple sessions share location and date."""
    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for session in sessions:
        groups[str(session["event_base_name"])].append(session)
    for base, group in groups.items():
        if len(group) == 1:
            group[0]["generated_event_name"] = base
            continue
        ordered = sorted(
            group,
            key=lambda item: str(item.get("captured_at_local") or ""),
        )
        used: set[str] = set()
        for index, session in enumerate(ordered, start=1):
            time_label = TIME_LABELS.get(session.get("time_of_day"), "Session")
            candidate = safe_filename(f"{base} — {time_label}")
            if candidate in used:
                candidate = safe_filename(f"{base} — Session {index}")
            used.add(candidate)
            session["generated_event_name"] = candidate


def assign_project_labels(projects: Iterable[ProjectBuild]) -> None:
    """Resolve project-label collisions using meaningful treatment suffixes."""
    groups: dict[tuple[str, str], list[ProjectBuild]] = defaultdict(list)
    for project in projects:
        if (
            not project.session_id
            or not project.generated_project_label
            or not project.accepted
            or project.family_role in {"superseded", "withheld"}
        ):
            continue
        groups[(project.session_id, project.generated_project_label)].append(project)

    for (_session_id, base), group in groups.items():
        if len(group) == 1:
            group[0].generated_project_label = base
            continue

        suffix_counts: dict[str, int] = defaultdict(int)
        for project in group:
            suffix_counts[project.project_treatment] += 1

        seen: dict[str, int] = defaultdict(int)
        for project in group:
            suffix = project.project_treatment
            seen[suffix] += 1
            if suffix_counts[suffix] == 1:
                label = f"{base} — {suffix}"
            else:
                label = f"{base} — {suffix} {seen[suffix]}"
            project.generated_project_label = safe_filename(label)
