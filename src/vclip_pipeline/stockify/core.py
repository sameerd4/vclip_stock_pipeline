"""Small helpers for time values, identifiers, names, and normalized text."""

from __future__ import annotations

import hashlib
import re
import uuid
from fractions import Fraction

from .constants import FRACTION_RE


# Time and naming helpers

# Return an XML tag name without its namespace prefix.
def local_name(tag: str) -> str:
    """Return an XML element's local tag name, ignoring namespaces."""
    return tag.rsplit("}", 1)[-1]


# Read Final Cut time text into an exact fraction.
def parse_time(value: str | None) -> Fraction:
    if value is None:
        return Fraction(0)
    match = FRACTION_RE.match(value)
    if not match:
        raise ValueError(f"Unsupported FCPXML time value: {value!r}")
    numerator = int(match.group(1))
    denominator = int(match.group(2) or 1)
    if denominator == 0:
        raise ValueError(f"Invalid zero denominator in time value: {value!r}")
    return Fraction(numerator, denominator)


# Write an exact fraction in Final Cut time syntax.
def format_time(value: Fraction) -> str:
    value = value.limit_denominator(60000)
    if value.denominator == 1:
        return f"{value.numerator}s"
    return f"{value.numerator}/{value.denominator}s"


# Round a fraction for human-readable JSON output.
def format_seconds(value: Fraction) -> float:
    return round(float(value), 6)


# Build a repeatable UUID from stable input values.
def stable_uid(*parts: str) -> str:
    """
    Generate a deterministic UUID-shaped identifier for repeatable output.
    Final Cut accepts standard UUID strings for project/event UIDs.
    """
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).digest()
    return str(uuid.UUID(bytes=digest[:16])).upper()


# Clean a name without changing more than Final Cut requires.
def safe_name(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[\x00-\x1f/:]+", " - ", value or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or fallback


# Turn a project name into a safe export prefix.
def commandpost_filename_prefix(value: str, fallback: str = "VCLIP") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", value or "")
    cleaned = re.sub(r"_+", "_", cleaned).strip("_").upper()
    return cleaned or fallback


# Build a short, repeatable ID for one stock candidate.
def stock_clip_id(*parts: str) -> str:
    digest = hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"VCLIP_{digest.upper()}"


# Normalize free-form text for loose name matching.
def normalized_text(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


# Turn a title into a simple URL-style slug.
def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "vclip-package"
