"""File-based persistence for config and canvas layouts.

Config dir: ~/.supreme-claudemander/
Config file: ~/.supreme-claudemander/config.json
Canvas layouts: ~/.supreme-claudemander/canvases/{name}.json
"""

import json
import os
import pathlib
import re
import shutil
from dataclasses import dataclass

from loguru import logger


@dataclass
class AppConfig:
    """Explicit config object — resolved once at startup, threaded everywhere."""

    config_dir: pathlib.Path

    @property
    def config_file(self) -> pathlib.Path:
        return self.config_dir / "config.json"

    @property
    def canvases_dir(self) -> pathlib.Path:
        return self.config_dir / "canvases"


def load(config_dir: pathlib.Path | None = None) -> AppConfig:
    """Factory: build an AppConfig from an explicit path or the environment."""
    if config_dir is None:
        override = os.environ.get("SUPREME_CLAUDEMANDER_CONFIG_DIR")
        config_dir = pathlib.Path(override) if override else pathlib.Path.home() / ".supreme-claudemander"
    return AppConfig(config_dir=config_dir)


# Legacy config dir — migrated automatically on first run
_LEGACY_CONFIG_DIR = pathlib.Path.home() / ".claude-rts"

DEFAULT_CONFIG = {
    "copy": "ctrl-shift-c",
    "paste": "ctrl-shift-v",
    "rightclick": "paste",
    "idle_threshold": 5,
    "default_canvas": "probe-qa",
    "theme": "catppuccin-mocha",
    "startup_script": "util-terminal",
    "util_container": {
        "name": "supreme-claudemander-util",
        "image": "supreme-claudemander-util:latest",
        "auto_start": True,
        "auto_stop": False,
        "mounts": {},
    },
    "sessions": {
        "orphan_timeout": 300,
        "scrollback_size": 65536,
        "tmux_persistence": True,
    },
}

# Allowed canvas name pattern: alphanumeric, hyphens, underscores
_CANVAS_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _migrate_legacy_config(app_config: AppConfig) -> None:
    """Migrate ~/.claude-rts/ to the active config dir if it exists."""
    if not _LEGACY_CONFIG_DIR.exists() or app_config.config_dir.exists():
        return
    try:
        shutil.copytree(_LEGACY_CONFIG_DIR, app_config.config_dir)
        logger.info("Migrated config from {} to {}", _LEGACY_CONFIG_DIR, app_config.config_dir)
    except Exception as exc:
        logger.warning("Failed to migrate legacy config: {}", exc)


def ensure_dirs(app_config: AppConfig) -> None:
    """Create config and canvases directories if they don't exist."""
    _migrate_legacy_config(app_config)
    app_config.config_dir.mkdir(parents=True, exist_ok=True)
    app_config.canvases_dir.mkdir(parents=True, exist_ok=True)
    logger.debug("Ensured config dirs: {}, {}", app_config.config_dir, app_config.canvases_dir)


def _valid_canvas_name(name: str) -> bool:
    """Return True if name is a safe canvas filename."""
    return bool(name) and bool(_CANVAS_NAME_RE.match(name))


# ── Config ──────────────────────────────────────────────


def read_config(app_config: AppConfig) -> dict:
    """Read config from disk, returning defaults for missing keys."""
    ensure_dirs(app_config)
    if app_config.config_file.exists():
        try:
            data = json.loads(app_config.config_file.read_text(encoding="utf-8"))
            logger.debug("Loaded config from {}", app_config.config_file)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read config, using defaults: {}", exc)
            data = {}
    else:
        data = {}

    # Merge defaults for any missing keys
    merged = {**DEFAULT_CONFIG, **data}
    return merged


def write_config(app_config: AppConfig, data: dict) -> dict:
    """Write config to disk. Merges with defaults for missing keys."""
    ensure_dirs(app_config)
    merged = {**DEFAULT_CONFIG, **data}
    app_config.config_file.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    logger.info("Wrote config to {}", app_config.config_file)
    return merged


# ── Canvases ────────────────────────────────────────────


def list_canvases(app_config: AppConfig) -> list[str]:
    """Return sorted list of saved canvas names (without .json extension)."""
    ensure_dirs(app_config)
    names = sorted(p.stem for p in app_config.canvases_dir.glob("*.json") if p.is_file())
    logger.debug("Listed {} canvas(es)", len(names))
    return names


def read_canvas(app_config: AppConfig, name: str) -> dict | None:
    """Read a canvas layout by name. Returns None if not found."""
    if not _valid_canvas_name(name):
        logger.warning("Invalid canvas name: {!r}", name)
        return None
    path = app_config.canvases_dir / f"{name}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        logger.debug("Loaded canvas '{}' from {}", name, path)
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read canvas '{}': {}", name, exc)
        return None


def write_canvas(app_config: AppConfig, name: str, data: dict) -> bool:
    """Write a canvas layout to disk. Returns True on success."""
    if not _valid_canvas_name(name):
        logger.warning("Invalid canvas name: {!r}", name)
        return False
    ensure_dirs(app_config)
    path = app_config.canvases_dir / f"{name}.json"
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info("Wrote canvas '{}' to {}", name, path)
        return True
    except OSError as exc:
        logger.error("Failed to write canvas '{}': {}", name, exc)
        return False


def delete_canvas(app_config: AppConfig, name: str) -> bool:
    """Delete a canvas layout. Returns True if it existed and was deleted."""
    if not _valid_canvas_name(name):
        return False
    path = app_config.canvases_dir / f"{name}.json"
    if path.exists():
        path.unlink()
        logger.info("Deleted canvas '{}'", name)
        return True
    return False
