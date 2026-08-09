"""Persistent application configuration loaded from the environment and config file."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__


ENV_NOMINATIM_USER_AGENT = "VCLIP_NOMINATIM_USER_AGENT"
ENV_CONFIG_DIR = "VCLIP_CONFIG_DIR"
CONFIG_FILENAME = "config.json"
DEFAULT_NOMINATIM_USER_AGENT = (
    f"vclip-stock-pipeline/{__version__} "
    "(local reverse-geocoder; set VCLIP_NOMINATIM_USER_AGENT to identify your install)"
)


def app_config_dir() -> Path:
    """Return the persistent config directory for this install."""
    override = os.environ.get(ENV_CONFIG_DIR)
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "VClip"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "vclip"
    return Path.home() / ".config" / "vclip"


def app_config_path() -> Path:
    return app_config_dir() / CONFIG_FILENAME


def load_app_config() -> dict[str, Any]:
    """Load optional JSON config; missing or invalid files yield an empty mapping."""
    path = app_config_path()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def nominatim_user_agent(cli_override: str | None = None) -> str:
    """Resolve Nominatim identity: CLI → env → config file → built-in default."""
    if cli_override and cli_override.strip():
        return cli_override.strip()
    env_value = os.environ.get(ENV_NOMINATIM_USER_AGENT)
    if env_value and env_value.strip():
        return env_value.strip()
    configured = load_app_config().get("nominatim_user_agent")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return DEFAULT_NOMINATIM_USER_AGENT
