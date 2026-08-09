"""Shared FCPXML tags, parsing patterns, and stock-metadata policy data."""

from __future__ import annotations

import re


VIDEO_CLIP_TAGS = {
    "asset-clip",
    "clip",
    "sync-clip",
    "mc-clip",
    "ref-clip",
    "video",
}

STRIPPED_CHILD_TAGS = {
    "audio",
    "audio-channel-source",
    "caption",
    "gap",
    "generator",
    "mc-source",
    "ref-clip",
    "sync-clip",
    "text",
    "title",
    "transition",
}

PRESERVED_VIDEO_CHILD_TAGS = {
    "adjust-blend",
    "adjust-crop",
    "adjust-corners",
    "adjust-stabilization",
    "adjust-transform",
    "adjust-rollingShutter",
    "adjust-360-transform",
    "adjust-reorient",
    "adjust-conform",
    "conform-rate",
    "filter-video",
    "metadata",
    "video-animation",
}

FRACTION_RE = re.compile(r"^\s*(-?\d+)(?:/(\d+))?s\s*$")
SRT_TIME_RE = re.compile(
    r"(\d\d):(\d\d):(\d\d),(\d\d\d)\s+-->\s+"
    r"(\d\d):(\d\d):(\d\d),(\d\d\d)"
)
SRT_DATETIME_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2})\s+"
    r"(\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)\b"
)
SRT_NUMERIC_FIELD_RE = re.compile(r"\b([A-Za-z_]+):\s*(-?\d+(?:\.\d+)?)")
DJI_FILENAME_TIMESTAMP_RE = re.compile(r"\bDJI_(\d{8})(\d{6})_", re.I)

KNOWN_PLACES = [
    {
        "poi": "University of Washington",
        "neighborhood": "University District",
        "city": "Seattle",
        "state": "Washington",
        "country": "United States",
        "lat": 47.6553,
        "lon": -122.3035,
        "radius_m": 1800.0,
        "timezone": "America/Los_Angeles",
        "aliases": [
            "uw",
            "u district",
            "university district",
            "university of washington",
            "cherry blossoms",
            "cherry blossoms at uw",
        ],
        "tags": [
            "Seattle",
            "University District",
            "University of Washington",
            "UW",
            "campus",
        ],
        "use_cases": [
            "education marketing",
            "university social media",
            "campus promotional video",
            "Seattle b-roll",
        ],
    },
    {
        "poi": None,
        "neighborhood": None,
        "city": "Seattle",
        "state": "Washington",
        "country": "United States",
        "lat": 47.6062,
        "lon": -122.3321,
        "radius_m": 26000.0,
        "timezone": "America/Los_Angeles",
        "aliases": ["seattle"],
        "tags": ["Seattle", "Washington", "Pacific Northwest"],
        "use_cases": ["Seattle b-roll", "city social media"],
    },
    {
        "poi": None,
        "neighborhood": None,
        "city": "Port Angeles",
        "state": "Washington",
        "country": "United States",
        "lat": 48.1181,
        "lon": -123.4307,
        "radius_m": 18000.0,
        "timezone": "America/Los_Angeles",
        "aliases": ["port angeles", "olympic", "olympic peninsula"],
        "tags": ["Port Angeles", "Olympic Peninsula", "Washington", "coastal"],
        "use_cases": ["travel marketing", "Pacific Northwest b-roll"],
    },
]

CREATIVE_NAME_TOKENS = [
    "rip fredo",
    "time flies",
    "chill",
    "island in the sun",
]

SUPPORTED_STOCK_SEGMENT_TAGS = {
    "asset-clip",
    "video",
}

RESOURCE_REF_TAGS = {
    "asset-clip",
    "audio",
    "clip",
    "filter-audio",
    "filter-video",
    "generator",
    "title",
    "transition",
    "video",
}

STILL_IMAGE_EXTENSIONS = {
    ".arw",
    ".bmp",
    ".cr2",
    ".dng",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".nef",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}

SIDECAR_EXTENSIONS = {
    ".srt",
}

SKIPPED_SCAN_DIR_NAMES = {
    ".DocumentRevisions-V100",
    ".DocumentRevisions-V100-bad-1",
    ".Spotlight-V100",
    ".TemporaryItems",
    ".Trashes",
    ".fseventsd",
    "__Trash",
}

DTD_FORBIDDEN_ATTRS = {
    ("library", "name"),
    ("asset-clip", "audioDuration"),
    ("asset-clip", "audioEnable"),
    ("asset-clip", "audioStart"),
}
