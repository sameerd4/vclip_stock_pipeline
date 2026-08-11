"""Classify whether unknown-location media is in-scope for VClip drone location work.

Does not mutate catalog/XML provenance. Classification is report-only.
"""

from __future__ import annotations

import re
from typing import Any

SCOPE_DRONE = "drone"
SCOPE_OUT_OF_SCOPE_NON_DRONE = "out_of_scope_non_drone"
SCOPE_UNKNOWN = "unknown_camera_family"

# Positive drone evidence (LUT / path / folder).
_DRONE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"dji\s*mini\s*[345]\s*pro", re.I),
    re.compile(r"dji\s*air\s*[23]\s*s?\b", re.I),
    re.compile(r"dji\s*mavic", re.I),
    re.compile(r"dji\s*avata", re.I),
    re.compile(r"/\s*drone\s*/", re.I),
    re.compile(r"mini\s*5\s*pro", re.I),
    re.compile(r"\bair\s*3\b", re.I),
)

# Clear non-drone handheld / phone / still-camera families.
_NON_DRONE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"osmo\s*pocket", re.I),
    re.compile(r"pocket\s*3", re.I),
    re.compile(r"osmo\s*action", re.I),
    re.compile(r"dji\s*action\s*[345]", re.I),
    re.compile(r"\biphone\b", re.I),
    re.compile(r"pocket\s+night", re.I),
    re.compile(r"pocket\s+reloaded", re.I),
)

_UUID_MOV_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.mov$",
    re.I,
)
_STILL_CAMERA_PREFIX_RE = re.compile(r"^(DSC_|IMG_|IMG-|DCIM)", re.I)


def classify_vclip_camera_scope(
    *,
    source_basename: str | None = None,
    media_path: str | None = None,
    camera_lut: str | None = None,
    source_event_name: str | None = None,
    source_project_name: str | None = None,
    extra_texts: list[str | None] | None = None,
) -> dict[str, Any]:
    """Return scope classification for a source appearance.

    ``out_of_scope_non_drone`` means the clip is not part of the VClip drone
    location backlog (Pocket / Action / iPhone / still-camera families).
    """
    filename = _basename(source_basename) or _basename(media_path) or ""
    texts = [
        camera_lut,
        media_path,
        source_basename,
        source_event_name,
        source_project_name,
        *list(extra_texts or []),
    ]
    blob = "\n".join(str(item) for item in texts if item)

    drone_hit = _first_match(blob, _DRONE_PATTERNS)
    non_drone_hit = _first_match(blob, _NON_DRONE_PATTERNS)

    if _UUID_MOV_RE.match(filename) or _STILL_CAMERA_PREFIX_RE.match(filename):
        return {
            "camera_scope": SCOPE_OUT_OF_SCOPE_NON_DRONE,
            "camera_family": (
                "iphone_export" if _UUID_MOV_RE.match(filename) else "still_camera"
            ),
            "reason": "filename_non_drone_family",
            "evidence": filename,
        }

    # LUT/path drone identity wins over weak naming hints.
    if drone_hit and not _is_non_drone_lut(camera_lut):
        return {
            "camera_scope": SCOPE_DRONE,
            "camera_family": "dji_drone",
            "reason": "drone_model_evidence",
            "evidence": drone_hit,
        }

    if non_drone_hit or _is_non_drone_lut(camera_lut):
        family = _non_drone_family(blob, camera_lut)
        return {
            "camera_scope": SCOPE_OUT_OF_SCOPE_NON_DRONE,
            "camera_family": family,
            "reason": "non_drone_camera_family",
            "evidence": non_drone_hit or str(camera_lut or "")[:120],
        }

    if filename.upper().startswith("DJI"):
        return {
            "camera_scope": SCOPE_UNKNOWN,
            "camera_family": "dji_unspecified",
            "reason": "dji_filename_without_model_evidence",
            "evidence": filename,
        }

    return {
        "camera_scope": SCOPE_UNKNOWN,
        "camera_family": "unknown",
        "reason": "insufficient_camera_evidence",
        "evidence": filename or None,
    }


def classify_appearance_camera_scope(appearance: dict[str, Any]) -> dict[str, Any]:
    """Classify from a review-location-recover appearance dict."""
    row = appearance.get("row") if isinstance(appearance.get("row"), dict) else {}
    return classify_vclip_camera_scope(
        source_basename=appearance.get("source_basename") or row.get("source_filename"),
        media_path=row.get("source_media_path"),
        camera_lut=row.get("camera_lut"),
        source_event_name=row.get("source_event_name"),
        source_project_name=row.get("source_project_name"),
        extra_texts=[
            appearance.get("event_name"),
            appearance.get("project_name"),
            row.get("session_event_name"),
        ],
    )


def is_out_of_scope_non_drone(scope: dict[str, Any] | str | None) -> bool:
    if isinstance(scope, dict):
        return scope.get("camera_scope") == SCOPE_OUT_OF_SCOPE_NON_DRONE
    return scope == SCOPE_OUT_OF_SCOPE_NON_DRONE


def _is_non_drone_lut(camera_lut: str | None) -> bool:
    if not camera_lut:
        return False
    return _first_match(str(camera_lut), _NON_DRONE_PATTERNS) is not None


def _non_drone_family(blob: str, camera_lut: str | None) -> str:
    text = f"{camera_lut or ''}\n{blob}".casefold()
    if "pocket" in text:
        return "osmo_pocket"
    if "action" in text:
        return "osmo_action"
    if "iphone" in text:
        return "iphone"
    if "dsc_" in text or "img_" in text:
        return "still_camera"
    return "non_drone"


def _first_match(text: str, patterns: tuple[re.Pattern[str], ...]) -> str | None:
    if not text:
        return None
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def _basename(value: str | None) -> str | None:
    if not value:
        return None
    name = str(value).rstrip("/").split("/")[-1]
    return name or None
