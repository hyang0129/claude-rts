"""Startup scripts: pluggable card source discovery.

Built-in scripts:
  - discover-devcontainers: runs docker discovery, returns terminal entries
  - from-layout: returns empty list (frontend loads from saved canvas)

Custom scripts: executable files in ~/.supreme-claudemander/startup/ that output JSON arrays.
"""

import asyncio
import json
import sys

from loguru import logger

from .config import AppConfig, read_config
from .discovery import discover_hubs

_DOCKER = "docker.exe" if sys.platform == "win32" else "docker"

# Built-in script names
BUILTIN_SCRIPTS = {"discover-devcontainers", "from-layout", "util-terminal"}


def ensure_startup_dir(app_config: AppConfig) -> None:
    """Create startup scripts directory if it doesn't exist."""
    startup_dir = app_config.config_dir / "startup"
    startup_dir.mkdir(parents=True, exist_ok=True)


async def run_startup(script_name: str, app_config: AppConfig) -> list[dict]:
    """Run a startup script and return its JSON output.

    For built-in scripts, handles them directly.
    For custom scripts in ~/.supreme-claudemander/startup/, executes them and parses JSON.

    Returns a list of card descriptors, e.g.:
      [{"type": "terminal", "name": "hub_1", "exec": "docker.exe exec -it ..."}]
    """
    if script_name == "discover-devcontainers":
        return await _builtin_discover_devcontainers()
    elif script_name == "from-layout":
        return await _builtin_from_layout()
    elif script_name == "util-terminal":
        return await _builtin_util_terminal(app_config)
    else:
        return await _run_custom_script(script_name, app_config)


async def _builtin_discover_devcontainers() -> list[dict]:
    """Discover devcontainers and return terminal card descriptors with exec commands."""
    hubs = await discover_hubs()
    result = []
    for h in hubs:
        result.append(
            {
                "type": "terminal",
                "name": h["hub"],
                "container": h["container"],
                "exec": f"{_DOCKER} exec -it -u vscode -w /workspaces/{h['hub']} {h['container']} bash -l",
            }
        )
    logger.info("discover-devcontainers: found {} hub(s)", len(result))
    return result


async def _builtin_util_terminal(app_config: AppConfig) -> list[dict]:
    """Return a single terminal card for the util container."""
    config = read_config(app_config)
    util_name = config.get("util_container", {}).get("name", "supreme-claudemander-util")
    logger.info("util-terminal: using util container '{}'", util_name)
    return [
        {
            "type": "terminal",
            "name": util_name,
            "container": util_name,
            "exec": f"{_DOCKER} exec -it {util_name} bash -l",
        }
    ]


async def _builtin_from_layout() -> list[dict]:
    """Return empty list — frontend will load from saved canvas."""
    logger.info("from-layout: returning empty list (frontend loads saved canvas)")
    return []


async def _run_custom_script(script_name: str, app_config: AppConfig) -> list[dict]:
    """Execute a custom startup script and parse its JSON output."""
    ensure_startup_dir(app_config)
    startup_dir = app_config.config_dir / "startup"

    # Security: only allow alphanumeric, hyphens, underscores in script names
    import re

    if not re.match(r"^[a-zA-Z0-9_-]+$", script_name):
        logger.error("Invalid startup script name: {!r}", script_name)
        raise ValueError(f"Invalid startup script name: {script_name!r}")

    # Look for the script in the startup directory
    script_path = None
    for candidate in startup_dir.iterdir():
        if candidate.stem == script_name and candidate.is_file():
            script_path = candidate
            break

    if script_path is None:
        logger.error("Startup script '{}' not found in {}", script_name, startup_dir)
        raise FileNotFoundError(f"Startup script '{script_name}' not found in {startup_dir}")

    logger.info("Running custom startup script: {}", script_path)

    proc = await asyncio.create_subprocess_exec(
        str(script_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err_msg = stderr.decode(errors="replace").strip()
        logger.error("Startup script '{}' failed (rc={}): {}", script_name, proc.returncode, err_msg)
        raise RuntimeError(f"Startup script '{script_name}' failed: {err_msg}")

    try:
        data = json.loads(stdout.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.error("Startup script '{}' returned invalid JSON: {}", script_name, exc)
        raise ValueError(f"Startup script '{script_name}' returned invalid JSON: {exc}")

    if not isinstance(data, list):
        raise ValueError(f"Startup script '{script_name}' must return a JSON array")

    logger.info("Custom startup script '{}' returned {} card(s)", script_name, len(data))
    return data
