"""File-based persistence for config and canvas layouts.

Config dir: ~/.claude-rts/
Config file: ~/.claude-rts/config.json
Canvas layouts: ~/.claude-rts/canvases/{name}.json
"""

import json
import pathlib
import re

from loguru import logger

CONFIG_DIR = pathlib.Path.home() / ".claude-rts"
CONFIG_FILE = CONFIG_DIR / "config.json"
CANVASES_DIR = CONFIG_DIR / "canvases"

DEFAULT_CONFIG = {
    "copy": "ctrl-shift-c",
    "paste": "ctrl-shift-v",
    "rightclick": "paste",
    "idle_threshold": 5,
    "default_canvas": "main",
    "theme": "catppuccin-mocha",
    "startup_script": "discover-devcontainers",
}

# Allowed canvas name pattern: alphanumeric, hyphens, underscores
_CANVAS_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def ensure_dirs() -> None:
    """Create config and canvases directories if they don't exist."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CANVASES_DIR.mkdir(parents=True, exist_ok=True)
    logger.debug("Ensured config dirs: {}, {}", CONFIG_DIR, CANVASES_DIR)


def _valid_canvas_name(name: str) -> bool:
    """Return True if name is a safe canvas filename."""
    return bool(name) and bool(_CANVAS_NAME_RE.match(name))


# ── Config ──────────────────────────────────────────────


def read_config() -> dict:
    """Read config from disk, returning defaults for missing keys."""
    ensure_dirs()
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            logger.debug("Loaded config from {}", CONFIG_FILE)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read config, using defaults: {}", exc)
            data = {}
    else:
        data = {}

    # Merge defaults for any missing keys
    merged = {**DEFAULT_CONFIG, **data}
    return merged


def write_config(data: dict) -> dict:
    """Write config to disk. Merges with defaults for missing keys."""
    ensure_dirs()
    merged = {**DEFAULT_CONFIG, **data}
    CONFIG_FILE.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    logger.info("Wrote config to {}", CONFIG_FILE)
    return merged


# ── Canvases ────────────────────────────────────────────


def list_canvases() -> list[str]:
    """Return sorted list of saved canvas names (without .json extension)."""
    ensure_dirs()
    names = sorted(
        p.stem for p in CANVASES_DIR.glob("*.json") if p.is_file()
    )
    logger.debug("Listed {} canvas(es)", len(names))
    return names


def read_canvas(name: str) -> dict | None:
    """Read a canvas layout by name. Returns None if not found."""
    if not _valid_canvas_name(name):
        logger.warning("Invalid canvas name: {!r}", name)
        return None
    path = CANVASES_DIR / f"{name}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        logger.debug("Loaded canvas '{}' from {}", name, path)
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read canvas '{}': {}", name, exc)
        return None


def write_canvas(name: str, data: dict) -> bool:
    """Write a canvas layout to disk. Returns True on success."""
    if not _valid_canvas_name(name):
        logger.warning("Invalid canvas name: {!r}", name)
        return False
    ensure_dirs()
    path = CANVASES_DIR / f"{name}.json"
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info("Wrote canvas '{}' to {}", name, path)
        return True
    except OSError as exc:
        logger.error("Failed to write canvas '{}': {}", name, exc)
        return False


def delete_canvas(name: str) -> bool:
    """Delete a canvas layout. Returns True if it existed and was deleted."""
    if not _valid_canvas_name(name):
        return False
    path = CANVASES_DIR / f"{name}.json"
    if path.exists():
        path.unlink()
        logger.info("Deleted canvas '{}'", name)
        return True
    return False
