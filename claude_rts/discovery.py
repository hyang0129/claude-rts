"""Discover running devcontainer hubs via docker ps."""

import asyncio
import re
import sys

_DOCKER = "docker.exe" if sys.platform == "win32" else "docker"


async def discover_hubs() -> list[dict]:
    """Return a list of running devcontainer hubs.

    Each entry: {"hub": "hub_1", "container": "container_name"}
    Sorted by hub name.
    """
    proc = await asyncio.create_subprocess_exec(
        _DOCKER,
        "ps",
        "--filter",
        "label=devcontainer.local_folder",
        "--format",
        '{{.Names}}|{{.Label "devcontainer.local_folder"}}',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        return []

    hubs = []
    for line in stdout.decode().strip().splitlines():
        parts = line.split("|", 1)
        if len(parts) != 2:
            continue
        container, folder = parts
        # Extract hub name from path like "d:\containers\hub_1"
        match = re.search(r"[/\\]([^/\\]+)$", folder)
        if match:
            hubs.append({"hub": match.group(1), "container": container.strip()})

    hubs.sort(key=lambda h: h["hub"])
    return hubs
