"""Read-only geographic inventory over canonical catalog export rows."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..db.connection import Database

UNKNOWN_GROUP = "(unknown)"
GROUP_BY_CHOICES = ("city", "neighborhood", "market")
ORIENTATION_CHOICES = ("vertical", "horizontal")

_UNKNOWN_LABELS = frozenset(
    {"", "unknown", "unknown location", "unknown place", "none"}
)
_HEADER_BY_GROUP = {
    "city": "Location",
    "neighborhood": "Neighborhood",
    "market": "Market",
}


def load_clip_markets(
    database: Database,
) -> dict[tuple[str, str], list[dict[str, str]]]:
    """Load market assignments keyed by (stockify_run_id, stock_clip_id).

    ``clip_markets`` is many-to-many. Duplicate (clip, market_id) rows from
    different sources are collapsed so a clip is counted once per market.
    """
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT stockify_run_id, stock_clip_id, market_id, market_label
            FROM clip_markets
            ORDER BY market_id, source
            """
        ).fetchall()
    result: dict[tuple[str, str], list[dict[str, str]]] = {}
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        key = (str(row["stockify_run_id"]), str(row["stock_clip_id"]))
        market_id = str(row["market_id"])
        identity = (*key, market_id)
        if identity in seen:
            continue
        seen.add(identity)
        result.setdefault(key, []).append(
            {
                "market_id": market_id,
                "market_label": str(row["market_label"] or market_id),
            }
        )
    return result


def aggregate_location_inventory(
    rows: Iterable[dict[str, Any]],
    *,
    markets_by_clip: Mapping[tuple[str, str], list[dict[str, str]]] | None = None,
    group_by: str = "city",
    city: str | None = None,
    neighborhood: str | None = None,
    market: str | None = None,
    orientation: str | None = None,
) -> dict[str, Any]:
    """Aggregate one exported stock clip once per city/neighborhood group.

    Identity is ``(stockify_run_id, stock_clip_id)``. Tags, named subjects,
    collections, and package copies are not part of the source rows and
    cannot multiply counts.

    Market grouping is many-to-many: a clip assigned to multiple markets
    appears in each matching market group, but ``totals`` stay unique.
    """
    if group_by not in GROUP_BY_CHOICES:
        raise ValueError(f"Unsupported group_by: {group_by}")
    requested_orientation = orientation
    filter_orientation = _catalog_orientation(orientation)
    market_map = markets_by_clip or {}

    filtered: list[dict[str, Any]] = []
    for row in rows:
        key = (str(row["stockify_run_id"]), str(row["stock_clip_id"]))
        clip_markets = list(market_map.get(key, []))
        resolved_city = _resolved_city(row)
        resolved_neighborhood = _resolved_neighborhood(row)
        if city and not _label_matches(resolved_city, city):
            continue
        if neighborhood and not _label_matches(resolved_neighborhood, neighborhood):
            continue
        if market and not _clip_in_market(clip_markets, market):
            continue
        if filter_orientation and row.get("orientation") != filter_orientation:
            continue
        enriched = dict(row)
        enriched["_clip_key"] = key
        enriched["_city"] = resolved_city
        enriched["_neighborhood"] = resolved_neighborhood
        enriched["_markets"] = clip_markets
        filtered.append(enriched)

    totals = _empty_totals()
    located = 0
    unlocated = 0
    groups: dict[str, dict[str, Any]] = {}
    display_keys: dict[str, str] = {}

    for row in filtered:
        duration = _clip_duration_seconds(row)
        size = _clip_size_bytes(row)
        orientation_value = row.get("orientation")
        totals["clip_count"] += 1
        totals["total_duration_seconds"] += duration
        if size is not None:
            totals["total_size_bytes"] += size
        if row["_city"]:
            located += 1
        else:
            unlocated += 1

        for group_key in _group_keys(row, group_by=group_by, market=market):
            folded = group_key.casefold()
            display = display_keys.setdefault(folded, group_key)
            bucket = groups.setdefault(display, _empty_group(display))
            bucket["clip_count"] += 1
            bucket["total_duration_seconds"] += duration
            if size is not None:
                bucket["total_size_bytes"] += size
            if orientation_value == "vertical":
                bucket["vertical_clip_count"] += 1
            elif orientation_value == "landscape":
                bucket["horizontal_clip_count"] += 1

    totals["located_clip_count"] = located
    totals["unlocated_clip_count"] = unlocated
    ordered = sorted(
        groups.values(),
        key=lambda item: (
            -item["total_duration_seconds"],
            item["key"] == UNKNOWN_GROUP,
            item["key"].casefold(),
        ),
    )
    return {
        "group_by": group_by,
        "filters": {
            "city": city,
            "neighborhood": neighborhood,
            "market": market,
            "orientation": requested_orientation,
        },
        "totals": totals,
        "groups": ordered,
    }


def format_location_inventory_text(report: Mapping[str, Any]) -> str:
    header = _HEADER_BY_GROUP.get(str(report.get("group_by") or "city"), "Location")
    groups = list(report.get("groups") or [])
    totals = report.get("totals") or _empty_totals()
    name_width = max(len(header), len("TOTAL"), 32)
    if groups:
        name_width = max(name_width, *(len(str(item["key"])) for item in groups))
    rule = "-" * (name_width + 8 + 12 + 12 + 3)
    lines = [
        f"{header:<{name_width}} {'Clips':>8} {'Duration':>12} {'Size':>12}",
        rule,
    ]
    for item in groups:
        lines.append(_format_row(item["key"], item, name_width))
    lines.append(rule)
    lines.append(_format_row("TOTAL", totals, name_width))
    lines.append(
        "Exported clips: "
        f"{int(totals.get('clip_count') or 0)}  "
        f"Located: {int(totals.get('located_clip_count') or 0)}  "
        f"Unlocated: {int(totals.get('unlocated_clip_count') or 0)}"
    )
    return "\n".join(lines)


def _format_row(name: str, metrics: Mapping[str, Any], name_width: int) -> str:
    return (
        f"{name:<{name_width}} "
        f"{int(metrics.get('clip_count') or 0):>8} "
        f"{format_duration_seconds(float(metrics.get('total_duration_seconds') or 0)):>12} "
        f"{format_size_bytes(int(metrics.get('total_size_bytes') or 0)):>12}"
    )


def format_duration_seconds(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s"


def format_size_bytes(size_bytes: int) -> str:
    value = float(max(0, int(size_bytes)))
    units = ("B", "KB", "MB", "GB", "TB")
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    if index == 0:
        return f"{int(value)} B"
    return f"{value:.1f} {units[index]}"


def _empty_totals() -> dict[str, Any]:
    return {
        "clip_count": 0,
        "total_duration_seconds": 0.0,
        "total_size_bytes": 0,
        "located_clip_count": 0,
        "unlocated_clip_count": 0,
    }


def _empty_group(key: str) -> dict[str, Any]:
    return {
        "key": key,
        "clip_count": 0,
        "total_duration_seconds": 0.0,
        "total_size_bytes": 0,
        "vertical_clip_count": 0,
        "horizontal_clip_count": 0,
    }


def _catalog_orientation(value: str | None) -> str | None:
    if not value:
        return None
    if value == "horizontal":
        return "landscape"
    if value == "vertical":
        return "vertical"
    raise ValueError(f"Unsupported orientation: {value}")


def _resolved_city(row: Mapping[str, Any]) -> str | None:
    location = row.get("location") if isinstance(row.get("location"), dict) else {}
    return _clean_label(location.get("city") or row.get("city"))


def _resolved_neighborhood(row: Mapping[str, Any]) -> str | None:
    location = row.get("location") if isinstance(row.get("location"), dict) else {}
    return _clean_label(location.get("neighborhood") or row.get("neighborhood"))


def _clean_label(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or text.casefold() in _UNKNOWN_LABELS:
        return None
    return text


def _label_matches(actual: str | None, expected: str) -> bool:
    return bool(actual) and actual.casefold() == expected.strip().casefold()


def _clip_in_market(markets: Iterable[Mapping[str, str]], expected: str) -> bool:
    needle = expected.strip().casefold()
    if not needle:
        return False
    for item in markets:
        if str(item.get("market_id") or "").casefold() == needle:
            return True
        if str(item.get("market_label") or "").casefold() == needle:
            return True
    return False


def _group_keys(
    row: Mapping[str, Any],
    *,
    group_by: str,
    market: str | None,
) -> list[str]:
    if group_by == "city":
        return [row["_city"] or UNKNOWN_GROUP]
    if group_by == "neighborhood":
        return [row["_neighborhood"] or UNKNOWN_GROUP]
    labels: list[str] = []
    seen: set[str] = set()
    for item in row.get("_markets") or []:
        if market and not _clip_in_market((item,), market):
            continue
        label = str(item.get("market_label") or item.get("market_id") or "").strip()
        folded = label.casefold()
        if not label or folded in seen:
            continue
        seen.add(folded)
        labels.append(label)
    if labels:
        return labels
    if market:
        return []
    return [UNKNOWN_GROUP]


def _clip_duration_seconds(row: Mapping[str, Any]) -> float:
    duration = (
        row.get("export_duration_seconds")
        if row.get("export_duration_seconds") not in (None, "")
        else None
    )
    if duration is None:
        duration = row.get("final_duration_seconds") or row.get("proposed_duration_seconds") or 0
    try:
        return float(duration)
    except (TypeError, ValueError):
        return 0.0


def _clip_size_bytes(row: Mapping[str, Any]) -> int | None:
    value = row.get("file_size_bytes")
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
