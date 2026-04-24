"""File-based persistence for config and canvas layouts.

Config dir: ~/.supreme-claudemander/
Config file: ~/.supreme-claudemander/config.json
Canvas layouts: ~/.supreme-claudemander/canvases/{name}.json
"""

import copy
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
    "probe_profiles": [],
    "main_profile_name": "main",
    "util_container": {
        "name": "supreme-claudemander-util",
        "image": "supreme-claudemander-util:latest",
        "auto_start": True,
        "auto_stop": False,
        "mounts": {},
        "cpu_limit": 2.0,
        "cpu_shares": 64,
        "memory_limit": "8g",
        "pids_limit": 512,
    },
    "sessions": {
        "orphan_timeout": 300,
        "scrollback_size": 65536,
        "tmux_persistence": True,
    },
    "container_manager": {
        "favorites": [],
        "image_whitelist": [
            "ubuntu:24.04",
            "mcr.microsoft.com/devcontainers/base:ubuntu-24.04",
            "mcr.microsoft.com/devcontainers/python:3.12",
        ],
        # Global cap on canvas-claude-created containers (running + stopped).
        # See epic #199 intent §8 and child #205. Human-tunable.
        "max_containers": 4,
        # Resource caps applied at creation time (#204). Human-tunable.
        # ``disk_limit`` is advisory in v1 — Docker named volumes have no
        # native size cap on overlay2/ext4; observe via Child 7 stats widget.
        "defaults": {
            "cpu_limit": 2.0,
            "memory_limit": "8g",
            "disk_limit": "10g",
            "pids_limit": 1024,
        },
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

    # Merge defaults for any missing keys.
    # Deep-copy DEFAULT_CONFIG so callers cannot mutate the global default
    # (e.g. by appending to nested lists like container_manager.favorites).
    merged = {**copy.deepcopy(DEFAULT_CONFIG), **data}
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


def write_state_snapshot(
    app_config: AppConfig,
    name: str,
    cards: list[dict],
    canvas_size: tuple[int, int] | list[int] = (3840, 2160),
) -> bool:
    """Write a server-authored canvas snapshot.

    Epic #236 child 5 (#241): the on-disk canvas JSON is no longer authored by
    the browser via ``PUT /api/canvases/{name}`` — it is a server-written
    snapshot derived from ``CardRegistry`` state. ``write_canvas`` is retained
    for direct schema-aware writes (e.g. fixtures, migration tests); this
    helper composes the canonical snapshot envelope around a list of card
    descriptors and is the function called by the ``apply_state_patch``
    write-through hook.

    Each entry in ``cards`` should be the output of ``card.to_descriptor()``
    on a registered card — it carries ``card_id``, ``type``, all server-owned
    state fields, and the type-specific extras the frontend's
    ``spawnFromSerialized`` path expects.
    """
    snapshot = {
        "name": name,
        "canvas_size": list(canvas_size),
        "cards": cards,
    }
    return write_canvas(app_config, name, snapshot)


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
