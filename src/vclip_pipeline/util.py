"""Small general-purpose helpers used across pipeline stages."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def utc_now() -> str:
    """Return a stable ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash a file without loading it into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(prefix: str, *parts: object, length: int = 20) -> str:
    """Build a deterministic readable identifier from stable inputs."""
    payload = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length].upper()
    return f"{prefix}_{digest}"


def safe_filename(value: str, fallback: str = "untitled") -> str:
    """Turn a display label into a macOS-friendly filename."""
    cleaned = re.sub(r"[\x00-\x1f/:]+", " - ", value or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or fallback


def json_dumps(value: Any) -> str:
    """Serialize compact JSON for SQLite columns."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_loads(value: str | None, fallback: Any) -> Any:
    """Read optional JSON from SQLite without spreading null checks."""
    if not value:
        return fallback
    return json.loads(value)


def ensure_empty_directory(path: Path, *, overwrite: bool) -> None:
    """Create an output directory, optionally replacing an existing one."""
    if path.exists():
        if not overwrite:
            raise FileExistsError(f"Output already exists: {path}")
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    path.mkdir(parents=True, exist_ok=True)


def unique_preserving_order(values: Iterable[str]) -> list[str]:
    """Remove duplicates while retaining the first occurrence."""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
