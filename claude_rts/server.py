"""aiohttp server: static files, hub discovery API, WebSocket-to-docker bridge via ConPTY."""

import asyncio
import json
import pathlib
import platform
import re
import sys
import time

from aiohttp import web
from loguru import logger
from .pty_compat import PtyProcess

_start_time = time.monotonic()

from .config import (  # noqa: E402
    AppConfig,
    read_config,
    write_config,
    list_canvases,
    read_canvas,
    write_state_snapshot,
    delete_canvas,
)
from .migrations import canvas_236  # noqa: E402
from .discovery import discover_hubs  # noqa: E402
from .startup import run_startup  # noqa: E402
from .util_container import ensure_util_container, discover_profiles, exec_in_util  # noqa: E402
from .sessions import SessionManager  # noqa: E402
from .cards import ServiceCardRegistry, ClaudeUsageCard, TerminalCard, CardRegistry, CanvasClaudeCard, BlueprintCard  # noqa: E402
from .cards.base import BaseCard  # noqa: E402
from .event_bus import EventBus  # noqa: E402
from .ansi_strip import strip_ansi  # noqa: E402
from . import blueprint as blueprint_mod  # noqa: E402
from . import container_spec as container_spec_mod  # noqa: E402

STATIC_DIR = pathlib.Path(__file__).parent / "static"

# Profile / slot names are interpolated into shell commands inside the util
# container (``sh -c '... /profiles/<name> ...'``). Every caller that accepts
# such a name must validate against this regex before substitution. Mirrors
# ``claude_rts/cards/claude_usage_card.py::_SAFE_IDENTIFIER`` and the frontend
# check in ``static/index.html``.
_SAFE_PROFILE_NAME = re.compile(r"^[a-zA-Z0-9._-]+$")


async def index_handler(request: web.Request) -> web.FileResponse:
    logger.debug("Serving index.html to {}", request.remote)
    return web.FileResponse(STATIC_DIR / "index.html")


async def hubs_handler(request: web.Request) -> web.Response:
    logger.info("Hub discovery requested by {}", request.remote)
    hubs = await discover_hubs()
    logger.info("Discovered {} hub(s): {}", len(hubs), [h["hub"] for h in hubs])
    return web.json_response(hubs)


async def startup_handler(request: web.Request) -> web.Response:
    logger.info("Startup requested by {}", request.remote)
    app_config: AppConfig = request.app["app_config"]
    config = read_config(app_config)
    script_name = config.get("startup_script", "util-terminal")
    try:
        result = await run_startup(script_name, app_config)
        logger.info("Startup script '{}' returned {} card(s)", script_name, len(result))
        return web.json_response({"status": "ok", "script": script_name, "cards": result})
    except Exception as exc:
        logger.exception("Startup script '{}' failed", script_name)
        return web.json_response(
            {"status": "error", "script": script_name, "error": str(exc), "cards": []},
            status=500,
        )


async def config_get_handler(request: web.Request) -> web.Response:
    logger.debug("Config read requested by {}", request.remote)
    app_config: AppConfig = request.app["app_config"]
    data = read_config(app_config)
    return web.json_response(data)


async def config_put_handler(request: web.Request) -> web.Response:
    logger.info("Config update requested by {}", request.remote)
    app_config: AppConfig = request.app["app_config"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="Invalid JSON")
    saved = write_config(app_config, body)
    return web.json_response(saved)


async def canvases_list_handler(request: web.Request) -> web.Response:
    logger.debug("Canvas list requested by {}", request.remote)
    app_config: AppConfig = request.app["app_config"]
    names = list_canvases(app_config)
    return web.json_response(names)


async def canvas_get_handler(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    logger.debug("Canvas '{}' read requested by {}", name, request.remote)
    app_config: AppConfig = request.app["app_config"]
    data = read_canvas(app_config, name)
    if data is None:
        raise web.HTTPNotFound(text=f"Canvas '{name}' not found")
    return web.json_response(data)


# Epic #236 child 5 (#241): the client-driven ``PUT /api/canvases/{name}`` was
# the last surviving client-authored mutation path for canvas state. It is
# retired here. Canvas JSON files are now server-authored — see
# ``write_state_snapshot`` and the ``CardRegistry`` write-through hook in
# ``on_startup`` below. ``GET`` and ``DELETE`` on canvases remain because they
# are canvas lifecycle operations, not card-field mutations.


async def cards_list_handler(request: web.Request) -> web.Response:
    """GET /api/cards?canvas=X — list live registry cards for a canvas.

    Epic #254 child 2 (#257): exposes server-owned card state to observers
    (MCP tools, monitoring, curl) before any browser attaches. Returns the
    output of ``CardRegistry.cards_on_canvas(canvas)`` filtered through
    ``to_descriptor()`` so the response carries the full server-owned state
    (card_id, starred, geometry, display_name, recovery_script, error_state).

    404 if the canvas name is invalid or the canvas JSON file does not exist;
    200 with ``[]`` if the canvas is empty.
    """
    canvas_name = request.query.get("canvas", "").strip()
    if not canvas_name:
        raise web.HTTPBadRequest(text="Missing 'canvas' query parameter")
    app_config: AppConfig = request.app["app_config"]
    # Validate canvas existence through the same read path as the rest of the
    # API — ``read_canvas`` already handles invalid names and missing files.
    data = read_canvas(app_config, canvas_name)
    if data is None:
        raise web.HTTPNotFound(text=f"Canvas '{canvas_name}' not found")

    card_registry: CardRegistry = request.app["card_registry"]
    descriptors: list[dict] = []
    for card in card_registry.cards_on_canvas(canvas_name):
        if getattr(card, "hidden", False):
            continue
        if not hasattr(card, "to_descriptor"):
            continue
        try:
            descriptors.append(card.to_descriptor())
        except Exception:
            logger.exception("cards_list_handler: card '{}' to_descriptor failed", card.id)
    return web.json_response(descriptors)


async def canvas_delete_handler(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    logger.info("Canvas '{}' delete requested by {}", name, request.remote)
    if name == "probe-qa":
        raise web.HTTPBadRequest(text="Cannot delete the 'probe-qa' canvas")
    app_config: AppConfig = request.app["app_config"]
    ok = delete_canvas(app_config, name)
    if not ok:
        raise web.HTTPNotFound(text=f"Canvas '{name}' not found")
    return web.json_response({"status": "ok", "name": name})


async def widget_system_info_handler(request: web.Request) -> web.Response:
    """Return system information for the system-info widget."""
    uptime_seconds = int(time.monotonic() - _start_time)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s"

    data = {
        "hostname": platform.node(),
        "platform": platform.platform(),
        "python_version": sys.version.split()[0],
        "uptime": uptime_str,
        "uptime_seconds": uptime_seconds,
    }
    return web.json_response(data)


_DOCKER_CMD = "docker"


async def widget_container_stats_handler(request: web.Request) -> web.Response:
    """Return live CPU/MEM stats for every Docker container (running + stopped).

    Runs ``docker stats --no-stream --format '{{json .}}'`` for running containers
    and ``docker ps -a`` to include stopped containers (zeroed stats). Each row
    is augmented with the ``created_by`` label so the UI can flag canvas-claude
    ownership.
    """
    # Test-mode injection: return mocked payload verbatim
    test_stats = request.app.get("_test_container_stats")
    if test_stats is not None:
        return web.json_response({"containers": list(test_stats)})

    # 1) docker ps -a to enumerate all containers + their created_by label
    try:
        proc = await asyncio.create_subprocess_exec(
            _DOCKER_CMD,
            "ps",
            "-a",
            "--format",
            '{{.Names}}|{{.State}}|{{.Label "created_by"}}',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except FileNotFoundError:
        return web.json_response({"error": "docker_unavailable"}, status=500)

    if proc.returncode != 0:
        err = stderr.decode().strip() if stderr else "docker ps failed"
        logger.warning("widget_container_stats: docker ps failed: {}", err)
        return web.json_response({"error": err}, status=500)

    containers: list[dict] = []
    for line in stdout.decode().strip().splitlines():
        parts = line.split("|", 2)
        if len(parts) < 2:
            continue
        name = parts[0].strip()
        state = parts[1].strip().lower()
        created_by = parts[2].strip() if len(parts) > 2 else ""
        containers.append(
            {
                "name": name,
                "status": "running" if state == "running" else "stopped",
                "cpu_percent": "0.00%",
                "mem_usage": "0B",
                "mem_limit": "0B",
                "mem_percent": "0.00%",
                "net_io": "--",
                "block_io": "--",
                "pids": 0,
                "created_by": created_by,
            }
        )

    if not containers:
        return web.json_response({"containers": []})

    # 2) docker stats --no-stream on running containers
    try:
        stats_proc = await asyncio.create_subprocess_exec(
            _DOCKER_CMD,
            "stats",
            "--no-stream",
            "--format",
            "{{json .}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stats_out, stats_err = await stats_proc.communicate()
    except FileNotFoundError:
        return web.json_response({"containers": containers})

    if stats_proc.returncode == 0:
        by_name = {c["name"]: c for c in containers}
        for line in stats_out.decode().strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                s = json.loads(line)
            except json.JSONDecodeError:
                continue
            n = s.get("Name") or s.get("Container") or ""
            if n not in by_name:
                continue
            c = by_name[n]
            c["cpu_percent"] = s.get("CPUPerc", "0.00%")
            mem_usage_raw = s.get("MemUsage", "0B / 0B")
            # MemUsage format: "123MiB / 1.5GiB"
            if " / " in mem_usage_raw:
                used, limit = mem_usage_raw.split(" / ", 1)
                c["mem_usage"] = used.strip()
                c["mem_limit"] = limit.strip()
            else:
                c["mem_usage"] = mem_usage_raw
            c["mem_percent"] = s.get("MemPerc", "0.00%")
            c["net_io"] = s.get("NetIO", "--")
            c["block_io"] = s.get("BlockIO", "--")
            try:
                c["pids"] = int(s.get("PIDs", 0))
            except (TypeError, ValueError):
                c["pids"] = 0

    return web.json_response({"containers": containers})


async def container_single_stats_handler(request: web.Request) -> web.Response:
    """Return live stats for a single container via ``docker stats --no-stream``."""
    name = request.match_info["name"]

    test_stats = request.app.get("_test_container_stats")
    if test_stats is not None:
        for c in test_stats:
            if c.get("name") == name:
                return web.json_response(c)
        return web.json_response({"error": f"No such container: {name}"}, status=404)

    try:
        proc = await asyncio.create_subprocess_exec(
            _DOCKER_CMD,
            "stats",
            "--no-stream",
            "--format",
            "{{json .}}",
            "--",
            name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except FileNotFoundError:
        return web.json_response({"error": "docker_unavailable"}, status=500)

    if proc.returncode != 0:
        err = stderr.decode().strip() if stderr else "docker stats failed"
        return web.json_response({"error": err}, status=500)

    line = stdout.decode().strip().splitlines()
    if not line:
        return web.json_response({"error": f"No stats for: {name}"}, status=404)

    try:
        s = json.loads(line[0])
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid stats output"}, status=500)

    mem_usage_raw = s.get("MemUsage", "0B / 0B")
    used, limit = (mem_usage_raw.split(" / ", 1) + [""])[:2] if " / " in mem_usage_raw else (mem_usage_raw, "")
    return web.json_response(
        {
            "name": s.get("Name") or name,
            "cpu_percent": s.get("CPUPerc", "0.00%"),
            "mem_usage": used.strip(),
            "mem_limit": limit.strip(),
            "mem_percent": s.get("MemPerc", "0.00%"),
            "net_io": s.get("NetIO", "--"),
            "block_io": s.get("BlockIO", "--"),
        }
    )


# ── Container Manager API ────────────────────────────────────────────────────


async def container_discover_handler(request: web.Request) -> web.Response:
    """Discover all Docker containers (running + stopped) with status."""
    # In test mode, return injected mock data if available
    test_containers = request.app.get("_test_containers")
    if test_containers is not None:
        return web.json_response(sorted(test_containers, key=lambda c: c["name"]))

    proc = await asyncio.create_subprocess_exec(
        _DOCKER_CMD,
        "ps",
        "-a",
        "--format",
        "{{.Names}}|{{.State}}|{{.Image}}|{{.Status}}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err = stderr.decode().strip() if stderr else "docker ps failed"
        logger.warning("container_discover: docker ps failed: {}", err)
        return web.json_response({"error": err}, status=500)

    containers = []
    for line in stdout.decode().strip().splitlines():
        parts = line.split("|", 3)
        if len(parts) < 2:
            continue
        name = parts[0].strip()
        state = parts[1].strip().lower()
        image = parts[2].strip() if len(parts) > 2 else ""
        status_text = parts[3].strip() if len(parts) > 3 else ""
        # Normalize state to online/offline/starting
        if state == "running":
            normalized = "online"
        elif state in ("created", "restarting"):
            normalized = "starting"
        else:
            normalized = "offline"
        containers.append(
            {
                "name": name,
                "state": normalized,
                "image": image,
                "status": status_text,
            }
        )

    containers.sort(key=lambda c: c["name"])
    return web.json_response(containers)


async def container_favorites_get_handler(request: web.Request) -> web.Response:
    """Read the Container Manager favorites list from config."""
    app_config: AppConfig = request.app["app_config"]
    config = read_config(app_config)
    cm_config = config.get("container_manager", {})
    favorites = cm_config.get("favorites", [])
    return web.json_response(favorites)


async def container_favorites_put_handler(request: web.Request) -> web.Response:
    """Write the Container Manager favorites list to config."""
    app_config: AppConfig = request.app["app_config"]
    body = await request.json()
    favorites = body if isinstance(body, list) else body.get("favorites", [])
    config = read_config(app_config)
    if "container_manager" not in config:
        config["container_manager"] = {}
    config["container_manager"]["favorites"] = favorites
    write_config(app_config, config)
    return web.json_response(favorites)


async def container_start_handler(request: web.Request) -> web.Response:
    """Start a stopped Docker container by name."""
    name = request.match_info["name"]

    # In test mode, flip mock container state instead of calling Docker
    test_containers = request.app.get("_test_containers")
    if test_containers is not None:
        for c in test_containers:
            if c["name"] == name:
                c["state"] = "online"
                logger.info("container_start (test): flipped '{}' to online", name)
                return web.json_response({"name": name, "state": "online"})
        return web.json_response({"error": f"No such container: {name}"}, status=500)

    proc = await asyncio.create_subprocess_exec(
        _DOCKER_CMD,
        "start",
        "--",
        name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err = stderr.decode().strip() if stderr else "docker start failed"
        logger.warning("container_start: failed to start container '{}': {}", name, err)
        return web.json_response({"error": err}, status=500)

    logger.info("container_start: started container '{}'", name)
    return web.json_response({"name": name, "state": "online"})


async def _require_canvas_claude_owned(request: web.Request, name: str) -> web.Response | None:
    """Guard: if the request originates from Canvas Claude (MCP), verify the container
    carries the Docker label ``created_by=canvas-claude``. Returns a 403 JSON response
    if the guard rejects, 500 if ``docker inspect`` fails, or None if the request is
    allowed (either not Canvas-Claude-originated, or label matches).

    Origin signal: query param ``via=canvas-claude`` OR header ``X-Canvas-Claude-Spawner``.
    Human UI requests do NOT set these and bypass the guard entirely.
    """
    via = request.query.get("via", "").strip().lower()
    spawner_hdr = request.headers.get("X-Canvas-Claude-Spawner", "").strip()
    is_canvas_claude = via == "canvas-claude" or bool(spawner_hdr)
    if not is_canvas_claude:
        return None

    # Test-mode hook: label lookup table keyed by container name
    test_labels = request.app.get("_test_container_labels")
    if test_labels is not None:
        label = test_labels.get(name, {}).get("created_by")
        if label != "canvas-claude":
            return web.json_response(
                {"error": "not_canvas_claude_owned", "container": name},
                status=403,
            )
        return None

    # Real Docker path: inspect the container's created_by label
    try:
        proc = await asyncio.create_subprocess_exec(
            _DOCKER_CMD,
            "inspect",
            "--format",
            '{{index .Config.Labels "created_by"}}',
            "--",
            name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except Exception as exc:
        logger.warning("created_by guard: docker inspect failed for '{}': {}", name, exc)
        return web.json_response(
            {"error": "docker_inspect_failed", "container": name, "detail": str(exc)},
            status=500,
        )
    if proc.returncode != 0:
        err = stderr.decode().strip() if stderr else "docker inspect failed"
        logger.warning("created_by guard: docker inspect failed for '{}': {}", name, err)
        return web.json_response(
            {"error": "docker_inspect_failed", "container": name, "detail": err},
            status=500,
        )
    label = stdout.decode().strip()
    # `docker inspect --format '{{index .Config.Labels "created_by"}}'` returns
    # the literal string "<no value>" when the label is missing.
    if label != "canvas-claude":
        logger.info(
            "created_by guard: rejected Canvas-Claude stop of '{}' (label={!r})",
            name,
            label,
        )
        return web.json_response(
            {"error": "not_canvas_claude_owned", "container": name},
            status=403,
        )
    return None


async def container_stop_handler(request: web.Request) -> web.Response:
    """Stop a running Docker container by name."""
    name = request.match_info["name"]

    # Authorization guard: when the request originates from Canvas Claude (MCP),
    # enforce that the container was created by Canvas Claude. Human UI calls do
    # NOT set the origin signal and are unaffected.
    guard_resp = await _require_canvas_claude_owned(request, name)
    if guard_resp is not None:
        return guard_resp

    # In test mode, flip mock container state instead of calling Docker
    test_containers = request.app.get("_test_containers")
    if test_containers is not None:
        for c in test_containers:
            if c["name"] == name:
                c["state"] = "offline"
                logger.info("container_stop (test): flipped '{}' to offline", name)
                return web.json_response({"name": name, "state": "offline"})
        return web.json_response({"error": f"No such container: {name}"}, status=500)

    # Optional timeout query param (default: Docker's built-in 10s)
    timeout_str = request.query.get("timeout")
    cmd_args = [_DOCKER_CMD, "stop"]
    if timeout_str is not None:
        try:
            timeout_val = max(0, int(timeout_str))
            cmd_args.extend(["-t", str(timeout_val)])
        except ValueError:
            return web.json_response({"error": "timeout must be a non-negative integer"}, status=400)
    cmd_args.extend(["--", name])

    proc = await asyncio.create_subprocess_exec(
        *cmd_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err = stderr.decode().strip() if stderr else "docker stop failed"
        logger.warning("container_stop: failed to stop container '{}': {}", name, err)
        return web.json_response({"error": err}, status=500)

    logger.info("container_stop: stopped container '{}'", name)
    return web.json_response({"name": name, "state": "offline"})


async def container_favorites_actions_put_handler(request: web.Request) -> web.Response:
    """Update actions for a specific favorite container by name."""
    name = request.match_info["name"]
    app_config: AppConfig = request.app["app_config"]
    cfg = read_config(app_config)
    cm_config = cfg.get("container_manager", {})
    favorites = cm_config.get("favorites", [])

    # Find the target favorite
    target = None
    for fav in favorites:
        if fav.get("name") == name:
            target = fav
            break

    if target is None:
        return web.json_response({"error": f"Favorite not found: {name}"}, status=404)

    try:
        actions = await request.json()
    except Exception:
        return web.json_response({"error": "Request body must be valid JSON"}, status=400)
    if not isinstance(actions, list):
        return web.json_response({"error": "Request body must be a JSON array of action objects"}, status=400)
    target["actions"] = actions

    # Persist
    if "container_manager" not in cfg:
        cfg["container_manager"] = {}
    cfg["container_manager"]["favorites"] = favorites
    write_config(app_config, cfg)

    return web.json_response(actions)


async def container_create_handler(request: web.Request) -> web.Response:
    """Create a new container via the devcontainer CLI.

    Body (JSON): ``{"image": str, "name"?: str, "preset"?: str}``.
    - Validates ``image`` against ``container_manager.image_whitelist`` config.
    - Generates a temp devcontainer.json, invokes ``devcontainer up`` async,
      stamps ``created_by=canvas-claude`` via runArgs.
    - On success, auto-registers the container as a favorite and returns
      ``{"container_id": name, "name": name, "status": "created"}``.
    """
    app_config: AppConfig = request.app["app_config"]
    cfg = read_config(app_config)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Request body must be valid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "Request body must be a JSON object"}, status=400)

    image = (body.get("image") or "").strip()
    if not image:
        return web.json_response({"error": "image is required"}, status=400)

    whitelist = cfg.get("container_manager", {}).get(
        "image_whitelist",
        ["ubuntu:24.04"],
    )
    if image not in whitelist:
        return web.json_response(
            {"error": "image_not_whitelisted", "allowed": whitelist},
            status=400,
        )

    name = (body.get("name") or "").strip() or None
    preset = body.get("preset") or "devcontainer"

    # Resource caps (#204): merge config-level defaults onto the spec so a
    # human can tune without code changes. Missing keys fall back to the
    # ContainerSpec class defaults (v1 targets from epic #199 intent §8).
    cap_defaults = cfg.get("container_manager", {}).get("defaults", {}) or {}
    cap_kwargs: dict = {}
    if "cpu_limit" in cap_defaults:
        cap_kwargs["cpu_limit"] = float(cap_defaults["cpu_limit"])
    if "memory_limit" in cap_defaults:
        cap_kwargs["memory_limit"] = str(cap_defaults["memory_limit"])
    if "disk_limit" in cap_defaults:
        cap_kwargs["disk_limit"] = str(cap_defaults["disk_limit"])
    if "pids_limit" in cap_defaults:
        cap_kwargs["pids_limit"] = int(cap_defaults["pids_limit"])

    # Profiles volume mount (#207). Volume name is configurable via the same
    # ``util_container.mounts.profiles`` config key used by the util container
    # so both point at the same named volume by default (``claude-profiles``).
    profiles_volume = (
        cfg.get("util_container", {}).get("mounts", {}).get("profiles") or container_spec_mod.DEFAULT_PROFILES_VOLUME
    )
    cap_kwargs["profiles_volume"] = profiles_volume

    spec = container_spec_mod.ContainerSpec(
        image=image,
        name=name,
        preset=preset,
        **cap_kwargs,
    )

    # ── 4-container global cap (#205) ─────────────────────────────────────
    # Mirrors the 10-terminal cap pattern at ``claude_terminal_create``: the
    # lock is held across the count-check + creation so concurrent requests
    # cannot race past the cap. The cap counts only containers stamped with
    # ``created_by=canvas-claude``; human-created containers are excluded.
    max_containers = int(cfg.get("container_manager", {}).get("max_containers", 4))
    create_lock: asyncio.Lock = request.app["container_create_lock"]
    async with create_lock:
        existing_names, count_err = await _count_canvas_claude_containers(request.app)
        if count_err is not None:
            return count_err
        if len(existing_names) >= max_containers:
            return web.json_response(
                {
                    "error": "container_cap_reached",
                    "message": (
                        f"Canvas Claude has reached the {max_containers}-container cap. "
                        "Rebuild or remove one before creating another."
                    ),
                    "existing_container_ids": sorted(existing_names),
                    "live_container_names": sorted(existing_names),
                },
                status=429,
            )

        # Test-mode hook: bypass the real subprocess call.
        test_create = request.app.get("_test_container_create")
        if test_create is not None:
            # Record the spec for assertions.
            test_create.setdefault("calls", []).append(
                {
                    "image": spec.image,
                    "name": spec.name,
                    "preset": spec.preset,
                    "labels": dict(spec.labels),
                    "devcontainer_json": spec.devcontainer_preset(),
                }
            )
            if test_create.get("should_fail"):
                return web.json_response(
                    {"error": "creation_failed", "detail": test_create.get("error", "mock failure")},
                    status=500,
                )
            # Register the new container in the test-labels map so subsequent
            # count-checks (inside the same test) see it as canvas-claude-owned.
            test_labels = request.app.get("_test_container_labels")
            if test_labels is not None:
                test_labels.setdefault(spec.name, {})["created_by"] = "canvas-claude"
        else:
            try:
                await container_spec_mod.create(spec)
            except RuntimeError as exc:
                return web.json_response(
                    {"error": "creation_failed", "detail": str(exc)},
                    status=500,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("container_create: unexpected failure")
                return web.json_response(
                    {"error": "creation_failed", "detail": str(exc)},
                    status=500,
                )

        # Auto-register as favorite (idempotent — skip if already present).
        if "container_manager" not in cfg:
            cfg["container_manager"] = {}
        favorites = cfg["container_manager"].get("favorites", [])
        if not any(f.get("name") == spec.name for f in favorites):
            favorites.append({"name": spec.name, "type": "docker", "actions": []})
            cfg["container_manager"]["favorites"] = favorites
            write_config(app_config, cfg)

        return web.json_response(
            {
                "container_id": spec.name,
                "name": spec.name,
                "image": spec.image,
                "status": "created",
            }
        )


async def _count_canvas_claude_containers(
    app: web.Application,
) -> tuple[list[str], web.Response | None]:
    """Return (names, error_response). Names are containers (running + stopped)
    with the ``created_by=canvas-claude`` label. On docker failure, returns
    ([], 500 response) — callers fail closed.

    Test-mode path: when ``_test_container_labels`` is populated, derive the
    count from that map so tests don't need to mock docker.
    """
    test_labels = app.get("_test_container_labels")
    if test_labels is not None:
        names = [name for name, entry in test_labels.items() if (entry or {}).get("created_by") == "canvas-claude"]
        return names, None

    try:
        proc = await asyncio.create_subprocess_exec(
            _DOCKER_CMD,
            "ps",
            "-a",
            "--filter",
            "label=created_by=canvas-claude",
            "--format",
            "{{.Names}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except Exception as exc:  # noqa: BLE001
        logger.warning("container_create: docker ps (cap count) failed: {}", exc)
        return [], web.json_response(
            {"error": "container_count_failed", "detail": str(exc)},
            status=500,
        )
    if proc.returncode != 0:
        err = stderr.decode().strip() if stderr else "docker ps failed"
        logger.warning("container_create: docker ps (cap count) failed: {}", err)
        return [], web.json_response(
            {"error": "container_count_failed", "detail": err},
            status=500,
        )
    names = [line.strip() for line in stdout.decode().splitlines() if line.strip()]
    return names, None


async def _inspect_container_for_rebuild(request: web.Request, name: str) -> tuple[dict | None, web.Response | None]:
    """Return (inspect_info, error_response). ``inspect_info`` is a dict with
    ``image``, ``labels``, ``mounts`` (list of mount-strings suitable for
    ``ContainerSpec.mounts``). If this is a test (``_test_container_labels``
    set), fall back to label data + image recorded in ``_test_container_create``
    (where available). On docker failure, returns (None, error_response).
    """
    # Test-mode hook: reuse the labels lookup table populated by tests.
    test_labels = request.app.get("_test_container_labels")
    if test_labels is not None:
        entry = test_labels.get(name) or {}
        image = entry.get("image", "ubuntu:24.04")
        labels = {k: v for k, v in entry.items() if k != "image"}
        # workspace volume name follows the create-time default
        mounts = entry.get("mounts") or [
            f"source={name}-workspace,target=/workspace,type=volume",
        ]
        return (
            {"image": image, "labels": labels, "mounts": mounts},
            None,
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            _DOCKER_CMD,
            "inspect",
            "--format",
            "{{json .}}",
            "--",
            name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except Exception as exc:  # noqa: BLE001
        logger.warning("container_rebuild: docker inspect failed for '{}': {}", name, exc)
        return None, web.json_response(
            {"error": "docker_inspect_failed", "container": name, "detail": str(exc)},
            status=500,
        )
    if proc.returncode != 0:
        err = stderr.decode().strip() if stderr else "docker inspect failed"
        return None, web.json_response(
            {"error": "docker_inspect_failed", "container": name, "detail": err},
            status=500,
        )
    try:
        raw = json.loads(stdout.decode())
    except Exception as exc:  # noqa: BLE001
        return None, web.json_response(
            {"error": "docker_inspect_parse_failed", "container": name, "detail": str(exc)},
            status=500,
        )
    cfg = raw.get("Config", {}) or {}
    image = cfg.get("Image", "")
    labels = cfg.get("Labels", {}) or {}
    mounts: list[str] = []
    for m in raw.get("Mounts", []) or []:
        mtype = m.get("Type", "")
        if mtype == "volume":
            vol = m.get("Name", "")
            target = m.get("Destination", "")
            if vol and target:
                mounts.append(f"source={vol},target={target},type=volume")
        elif mtype == "bind":
            src = m.get("Source", "")
            target = m.get("Destination", "")
            if src and target:
                mounts.append(f"source={src},target={target},type=bind")
    return {"image": image, "labels": labels, "mounts": mounts}, None


async def container_rebuild_handler(request: web.Request) -> web.Response:
    """Rebuild a canvas-claude-owned container: stop + docker rm + recreate.

    ABSOLUTE INVARIANT: only containers with ``created_by=canvas-claude`` may be
    rebuilt through this endpoint. The guard is enforced unconditionally (not
    gated on the Canvas-Claude origin signal) because ``docker rm`` is a
    destructive operation that must never touch human-owned containers.

    The workspace volume is preserved — ``docker rm`` is called WITHOUT the
    ``-v`` flag, so named volumes survive and are re-attached to the new
    container via the reconstructed ``ContainerSpec``.
    """
    name = request.match_info["name"]

    # Hard guard: rebuild is unconditionally gated on created_by=canvas-claude.
    # We force the origin signal on so `_require_canvas_claude_owned` runs
    # regardless of how the request was made.
    test_labels = request.app.get("_test_container_labels")
    if test_labels is not None:
        label = test_labels.get(name, {}).get("created_by")
        if label != "canvas-claude":
            return web.json_response(
                {"error": "not_canvas_claude_owned", "container": name},
                status=403,
            )
    else:
        # Real-Docker path: run the same inspect used by the stop guard.
        try:
            proc = await asyncio.create_subprocess_exec(
                _DOCKER_CMD,
                "inspect",
                "--format",
                '{{index .Config.Labels "created_by"}}',
                "--",
                name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"error": "docker_inspect_failed", "container": name, "detail": str(exc)},
                status=500,
            )
        if proc.returncode != 0:
            err = stderr.decode().strip() if stderr else "docker inspect failed"
            return web.json_response(
                {"error": "docker_inspect_failed", "container": name, "detail": err},
                status=500,
            )
        label = stdout.decode().strip()
        if label != "canvas-claude":
            return web.json_response(
                {"error": "not_canvas_claude_owned", "container": name},
                status=403,
            )

    # Read the container's spec (image, labels, mounts) to reconstruct on recreate.
    info, err_resp = await _inspect_container_for_rebuild(request, name)
    if err_resp is not None:
        return err_resp
    assert info is not None

    # Test-mode hook: flip mock state + record the rebuild call instead of
    # calling Docker. Mirrors the _test_container_create convention so tests
    # can assert on the reconstructed ContainerSpec.
    test_create = request.app.get("_test_container_create")
    test_containers = request.app.get("_test_containers")
    if test_labels is not None or test_create is not None or test_containers is not None:
        # Record the docker rm/stop invocations for assertions.
        rebuild_log = request.app.setdefault("_test_rebuild_calls", [])
        rebuild_log.append({"op": "stop", "name": name})
        rebuild_log.append({"op": "rm", "name": name, "with_volumes": False})
        # Rebuild the spec and hand to the create test-hook for recording.
        spec = container_spec_mod.ContainerSpec(
            image=info["image"] or "ubuntu:24.04",
            name=name,
            preset="devcontainer",
            labels=dict(info["labels"]),
            mounts=list(info["mounts"]),
        )
        if test_create is None:
            test_create = {}
            request.app["_test_container_create"] = test_create
        test_create.setdefault("calls", []).append(
            {
                "image": spec.image,
                "name": spec.name,
                "preset": spec.preset,
                "labels": dict(spec.labels),
                "mounts": list(spec.mounts),
                "devcontainer_json": spec.devcontainer_preset(),
            }
        )
        if test_containers is not None:
            for c in test_containers:
                if c["name"] == name:
                    c["state"] = "online"
                    break
        return web.json_response({"container_id": name, "name": name, "status": "rebuilt"})

    # Real-Docker path: stop → rm → recreate.
    stop_proc = await asyncio.create_subprocess_exec(
        _DOCKER_CMD,
        "stop",
        "-t",
        "10",
        "--",
        name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await stop_proc.communicate()
    # Note: stop failure is non-fatal — the container may already be stopped.

    # `docker rm` WITHOUT -v so the named workspace volume is preserved.
    rm_proc = await asyncio.create_subprocess_exec(
        _DOCKER_CMD,
        "rm",
        "--",
        name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    rm_stdout, rm_stderr = await rm_proc.communicate()
    if rm_proc.returncode != 0:
        err = rm_stderr.decode().strip() if rm_stderr else "docker rm failed"
        logger.warning("container_rebuild: docker rm failed for '{}': {}", name, err)
        return web.json_response(
            {"error": "rebuild_failed", "container": name, "detail": err},
            status=500,
        )

    spec = container_spec_mod.ContainerSpec(
        image=info["image"] or "ubuntu:24.04",
        name=name,
        preset="devcontainer",
        labels=dict(info["labels"]),
        mounts=list(info["mounts"]),
    )
    try:
        await container_spec_mod.create(spec)
    except RuntimeError as exc:
        return web.json_response(
            {
                "error": "rebuild_failed",
                "container": name,
                "detail": str(exc),
                "note": "container removed but recreation failed — volume preserved",
            },
            status=500,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("container_rebuild: unexpected failure during recreate")
        return web.json_response(
            {
                "error": "rebuild_failed",
                "container": name,
                "detail": str(exc),
                "note": "container removed but recreation failed — volume preserved",
            },
            status=500,
        )

    logger.info("container_rebuild: rebuilt '{}'", name)
    return web.json_response({"container_id": name, "name": name, "status": "rebuilt"})


# Remote-access note (issue #224, epic #119):
# aiohttp 3.13.x's web.WebSocketResponse does NOT validate the WebSocket Origin
# header — the class exposes no `check_origin` / `allowed_origins` parameter and
# its source contains no reference to the Origin header. Browsers running on a
# remote Tailscale peer (e.g. http://100.x.x.x:3000) can upgrade WebSocket
# connections against `--host 0.0.0.0` without a 403.
#
# This is intentional for supreme-claudemander: the auth boundary is Tailscale
# network enrollment, not an application-level Origin allowlist. All four
# `web.WebSocketResponse()` call sites in this file (exec / session_new /
# session_attach / ws_control) therefore remain bare `WebSocketResponse()`
# constructions with no origin guard. See tests/test_server.py for the
# regression test that pins this behaviour.
async def exec_websocket_handler(request: web.Request) -> web.WebSocketResponse:
    """WebSocket handler that spawns a PTY for an arbitrary command."""
    cmd = request.query.get("cmd", "").strip()
    if not cmd:
        logger.warning("exec WebSocket: missing 'cmd' query parameter")
        raise web.HTTPBadRequest(text="Missing 'cmd' query parameter")

    logger.info("exec WebSocket request: cmd={!r}", cmd)

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    logger.info("exec WebSocket established for cmd={!r}", cmd)

    logger.info("Spawning PTY process: {}", cmd)

    try:
        pty = PtyProcess.spawn(cmd, dimensions=(24, 80))
    except Exception:
        logger.exception("Failed to spawn PTY for cmd={!r}", cmd)
        await ws.close(code=1011, message=b"Failed to spawn terminal")
        return ws

    logger.info("PTY spawned successfully for cmd={!r}", cmd)

    async def pty_read_loop():
        """Read from PTY and forward to WebSocket."""
        loop = asyncio.get_event_loop()
        try:
            while pty.isalive():
                try:
                    data = await loop.run_in_executor(None, pty.read)
                    if data:
                        await ws.send_bytes(data)
                except EOFError:
                    logger.info("PTY EOF for cmd={!r}", cmd)
                    break
                except Exception:
                    logger.exception("PTY read error for cmd={!r}", cmd)
                    break
        finally:
            if not ws.closed:
                await ws.close()

    read_task = asyncio.create_task(pty_read_loop())

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.BINARY:
                text = msg.data.decode("utf-8", errors="replace")
                pty.write(text)
            elif msg.type == web.WSMsgType.TEXT:
                try:
                    control = json.loads(msg.data)
                    if control.get("type") == "resize":
                        cols = control.get("cols", 80)
                        rows = control.get("rows", 24)
                        logger.info("Resize exec cmd={!r}: {}x{}", cmd, cols, rows)
                        pty.setwinsize(rows, cols)
                except json.JSONDecodeError:
                    logger.warning("Invalid JSON control message for exec cmd={!r}", cmd)
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    except Exception:
        logger.exception("exec WebSocket handler error for cmd={!r}", cmd)
    finally:
        logger.info("Cleaning up exec session cmd={!r}", cmd)
        read_task.cancel()
        try:
            pty.terminate(force=True)
        except Exception:
            pass

    return ws


# ── Session-based WebSocket handlers ─────────────────────────────────────────


async def _session_ws_input_loop(ws: web.WebSocketResponse, session, mgr: SessionManager):
    """Shared input loop for session-based WebSocket handlers."""
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.BINARY:
                text = msg.data.decode("utf-8", errors="replace")
                session.pty.write(text)
            elif msg.type == web.WSMsgType.TEXT:
                try:
                    control = json.loads(msg.data)
                    if control.get("type") == "resize":
                        cols = control.get("cols", 80)
                        rows = control.get("rows", 24)
                        session.pty.setwinsize(rows, cols)
                except json.JSONDecodeError:
                    pass
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    except Exception:
        logger.exception("Session {} WS input error", session.session_id)
    finally:
        mgr.detach(session.session_id, ws)


async def session_new_handler(request: web.Request) -> web.WebSocketResponse:
    """Create a new persistent session and attach via WebSocket.

    Creates a TerminalCard, registers it in the CardRegistry, starts
    the PTY, then bridges the WebSocket to the session.
    """
    cmd = request.query.get("cmd", "").strip()
    hub = request.query.get("hub", "")
    container = request.query.get("container", "").strip()
    # Epic #236 child 5 (#241): canvas_name records canvas membership for the
    # write-through persistence hook; absent → no write-through.
    canvas_name = request.query.get("canvas_name", "").strip() or None
    # Epic #236 follow-up (#254): the client forwards the snapshot's
    # ``starred`` field so the server's initial registry state matches the
    # canvas the card is spawning from. The client is not authoring the
    # value — the snapshot / preset fixture is. Absent or "false" → unstarred
    # (server default). Only the exact string "true" flips to starred.
    starred = request.query.get("starred", "").strip().lower() == "true"
    # Stable identity UUID across reconnects. Client generates via
    # ``crypto.randomUUID`` on first spawn and ships it here; the client is a
    # courier, not the author. Server stores it and emits via ``to_descriptor``.
    card_uid = request.query.get("card_uid", "").strip()
    # Rehydrate display_name and recovery_script from the snapshot when the
    # client is re-spawning a starred card on reload. Same courier pattern as
    # ``starred`` and ``card_uid`` — the client carries the value from the
    # snapshot into the new PTY session so the server registry (authoritative)
    # is re-seeded from disk. On first spawn both are empty strings.
    spawn_display_name = request.query.get("display_name", "")
    spawn_recovery_script = request.query.get("recovery_script", "")
    # Epic #254 child 2 (#257): attach-vs-create deduplication. Hydrated cards
    # already exist in ``CardRegistry`` at startup; a browser calling
    # ``/ws/session/new?card_id=<id>`` (or, pending Child #3 client
    # inversion, ``card_uid=<uid>``) must attach to the existing session
    # instead of spawning a duplicate TerminalCard. This is the coexistence
    # seam that keeps the hydration path (Child #2) working against the
    # pre-inversion client (before Child #3 ships).
    spawn_card_id = request.query.get("card_id", "").strip()
    if not cmd:
        raise web.HTTPBadRequest(text="Missing 'cmd' query parameter")

    mgr: SessionManager = request.app["session_manager"]
    card_registry: CardRegistry = request.app["card_registry"]

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # Dedup: if a hydrated card already exists for this spawn, attach to it
    # rather than creating a new TerminalCard.
    existing: TerminalCard | None = None
    if spawn_card_id:
        existing = card_registry.get_terminal(spawn_card_id)
    if existing is None and card_uid:
        for term in card_registry.list_terminals():
            if getattr(term, "card_uid", "") == card_uid:
                existing = term
                break
    if existing is not None and existing.alive:
        session = existing.session
        await ws.send_str(json.dumps({"session_id": session.session_id, "tmux": session.tmux_backed}))
        await mgr.attach(session.session_id, ws)
        await _session_ws_input_loop(ws, session, mgr)
        return ws

    try:
        card = TerminalCard(
            session_manager=mgr,
            cmd=cmd,
            hub=hub or None,
            container=container or None,
            starred=starred,
            card_uid=card_uid or None,
            display_name=spawn_display_name or None,
            recovery_script=spawn_recovery_script or None,
        )
        await card.start()
    except Exception:
        logger.exception("Failed to create session for cmd={!r}", cmd)
        await ws.send_str(json.dumps({"error": "Failed to spawn terminal"}))
        await ws.close()
        return ws

    session = card.session
    # Send session_id BEFORE registering the card.  card_registry.register()
    # schedules a card_created broadcast via the EventBus async task.  If the
    # broadcast fires before the browser's terminal WS receives session_id, the
    # client-side duplicate guard (cards.some(c => c.sessionId === ...)) sees
    # sessionId as null and spawns a ghost card.  Sending session_id first
    # closes that race window.
    await ws.send_str(json.dumps({"session_id": session.session_id, "tmux": session.tmux_backed}))
    card_registry.register(card, canvas_name=canvas_name)
    # Write-through on registration: epic #236 identity fields carried in via
    # the WS spawn query (card_uid, display_name, recovery_script, starred)
    # must land in the snapshot without waiting for a user-driven mutation.
    # Funnel through ``apply_state_patch`` so the write-through hook runs
    # exactly as it would for a PUT /api/cards/{id}/state (a no-op re-commit
    # of ``starred`` suffices; the hook rewrites the full descriptor).
    if canvas_name:
        try:
            card_registry.apply_state_patch(card.id, {"starred": bool(card.starred)})
        except (LookupError, ValueError):
            logger.exception("session_new_handler: post-register persist failed")
    await mgr.attach(session.session_id, ws)

    # Send resize if client sends it as first message
    await _session_ws_input_loop(ws, session, mgr)
    return ws


async def session_attach_handler(request: web.Request) -> web.WebSocketResponse:
    """Attach to an existing persistent session via WebSocket.

    Looks up the TerminalCard in the CardRegistry first, falling back
    to a plain SessionManager lookup for legacy sessions.
    """
    session_id = request.match_info["session_id"]
    mgr: SessionManager = request.app["session_manager"]
    card_registry: CardRegistry = request.app["card_registry"]

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # Try CardRegistry first, then fall back to SessionManager
    card = card_registry.get_terminal(session_id)
    if card and not card.alive:
        # Card exists but PTY died — unregister stale card
        card_registry.unregister(session_id)
        card = None

    scrollback = await mgr.attach(session_id, ws)
    if scrollback is None:
        await ws.send_str(json.dumps({"error": "session_not_found"}))
        await ws.close()
        return ws

    # Replay scrollback, then signal ready
    if scrollback:
        await ws.send_bytes(scrollback)
    await ws.send_str(json.dumps({"type": "session_attached", "session_id": session_id}))

    session = mgr.get_session(session_id)
    if session:
        await _session_ws_input_loop(ws, session, mgr)
    return ws


async def sessions_list_handler(request: web.Request) -> web.Response:
    """List all active sessions."""
    mgr: SessionManager = request.app["session_manager"]
    return web.json_response(mgr.list_sessions())


# ── Test puppeting API (test_mode only) ──────────────────────────────────────


async def test_session_create(request: web.Request) -> web.Response:
    cmd = request.query.get("cmd", "").strip()
    hub = request.query.get("hub", "")
    container = request.query.get("container", "").strip()
    if not cmd:
        raise web.HTTPBadRequest(text="Missing 'cmd' query parameter")
    mgr: SessionManager = request.app["session_manager"]
    try:
        session = mgr.create_session(cmd, hub=hub or None, container=container or None)
        return web.json_response({"session_id": session.session_id})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def test_session_send(request: web.Request) -> web.Response:
    sid = request.match_info["id"]
    mgr: SessionManager = request.app["session_manager"]
    session = mgr.get_session(sid)
    if not session:
        raise web.HTTPNotFound(text="Session not found")
    text = await request.text()
    session.pty.write(text)
    return web.json_response({"status": "ok", "sent": len(text)})


async def test_session_read(request: web.Request) -> web.Response:
    sid = request.match_info["id"]
    mgr: SessionManager = request.app["session_manager"]
    session = mgr.get_session(sid)
    if not session:
        raise web.HTTPNotFound(text="Session not found")
    data = session.scrollback.get_all()
    return web.json_response(
        {
            "output": data.decode("utf-8", errors="replace"),
            "size": len(data),
            "total_written": session.scrollback.total_written,
        }
    )


async def test_session_status(request: web.Request) -> web.Response:
    sid = request.match_info["id"]
    mgr: SessionManager = request.app["session_manager"]
    session = mgr.get_session(sid)
    if not session:
        raise web.HTTPNotFound(text="Session not found")
    now = time.monotonic()
    return web.json_response(
        {
            "session_id": session.session_id,
            "alive": session.alive,
            "client_count": len(session.clients),
            "scrollback_size": session.scrollback.size,
            "age_seconds": int(now - session.created_at),
            "idle_seconds": int(now - session.last_client_time),
        }
    )


async def test_session_delete(request: web.Request) -> web.Response:
    sid = request.match_info["id"]
    mgr: SessionManager = request.app["session_manager"]
    if not mgr.get_session(sid):
        raise web.HTTPNotFound(text="Session not found")
    mgr.destroy_session(sid, kill_tmux=True)
    return web.json_response({"status": "ok"})


async def test_sessions_list(request: web.Request) -> web.Response:
    mgr: SessionManager = request.app["session_manager"]
    return web.json_response(mgr.list_sessions())


async def test_canvas_seed(request: web.Request) -> web.Response:
    """POST /api/test/canvases/{name} — seed a canvas JSON fixture on disk.

    Test-mode only. Replaces the pre-epic ``PUT /api/canvases/{name}`` that
    E2E tests used to write fixtures before a reload (#247 removed the
    public endpoint because canvas JSON is now server-authored via the
    write-through hook). Accepts an arbitrary JSON body and writes it
    directly to ``{canvases_dir}/{name}.json``. Validates ``name`` through
    the same regex as the production canvas paths to keep this off the
    filesystem-write attack surface even though test mode is opt-in.
    """
    from . import config as _cfg

    name = request.match_info["name"]
    if not _cfg._valid_canvas_name(name):
        raise web.HTTPBadRequest(text="invalid canvas name")
    try:
        data = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest(text=f"invalid JSON: {exc}")

    app_config: AppConfig = request.app["app_config"]
    _cfg.ensure_dirs(app_config)
    path = app_config.canvases_dir / f"{name}.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return web.json_response({"status": "ok", "path": str(path)})


async def test_containers_put(request: web.Request) -> web.Response:
    """PUT /api/test/containers — inject fake container list for E2E tests."""
    data = await request.json()
    containers = data if isinstance(data, list) else data.get("containers", [])
    request.app["_test_containers"] = containers
    return web.json_response(containers)


async def test_containers_get(request: web.Request) -> web.Response:
    """GET /api/test/containers — read back fake container list."""
    containers = request.app.get("_test_containers", [])
    return web.json_response(containers)


async def profiles_discover_handler(request: web.Request) -> web.Response:
    """GET /api/profiles/discover — re-scan /profiles in the util container."""
    app_config: AppConfig = request.app["app_config"]
    discovered = await discover_profiles(app_config)
    request.app["discovered_profiles"] = discovered
    return web.json_response({"profiles": discovered})


async def profiles_list_handler(request: web.Request) -> web.Response:
    """GET /api/profiles — list all probe profiles with latest usage data."""
    app_config: AppConfig = request.app["app_config"]
    registry: ServiceCardRegistry = request.app["service_card_registry"]
    config = read_config(app_config)
    main_profile_name = config.get("main_profile_name") or "main"
    active_main_source = config.get("active_main_source")

    # Merge discovered profiles with any manually configured ones
    discovered = request.app.get("discovered_profiles", [])
    config_profiles = config.get("probe_profiles", [])
    probe_profiles = sorted(set(discovered + config_profiles))

    profiles = []
    for profile in probe_profiles:
        card = registry.get("claude-usage", profile)
        entry = {"profile": profile, "main_profile_name": main_profile_name, "is_main": profile == active_main_source}
        if card and card.last_result:
            r = card.last_result
            entry.update(
                {
                    "five_hour_pct": r.get("five_hour_pct"),
                    "five_hour_resets": r.get("five_hour_resets"),
                    "seven_day_pct": r.get("seven_day_pct"),
                    "seven_day_resets": r.get("seven_day_resets"),
                    "burn_rate": r.get("burn_rate"),
                    "probe_available": True,
                }
            )
        else:
            entry.update(
                {
                    "five_hour_pct": None,
                    "five_hour_resets": None,
                    "seven_day_pct": None,
                    "seven_day_resets": None,
                    "burn_rate": None,
                    "probe_available": False,
                }
            )
        profiles.append(entry)

    # Sort by burn_rate ascending, nulls last
    profiles.sort(key=lambda p: (p["burn_rate"] is None, p["burn_rate"] or 0))
    return web.json_response(profiles)


async def main_profile_get_handler(request: web.Request) -> web.Response:
    """GET /api/profiles/main — return the configured main profile slot name.

    Returns {"main_profile_name": "<name>", "exists": <bool>} where exists
    reports whether the credential file for the main slot is present in the
    util container. A missing credential file means no profile has been
    promoted yet; callers should show an error + retry UI.
    """
    app_config: AppConfig = request.app["app_config"]
    config = read_config(app_config)
    name = config.get("main_profile_name") or "main"

    # Defence-in-depth: the name is interpolated into a shell command below,
    # so reject anything that is not a plain identifier. If a user hand-edits
    # config.json with a malicious value we refuse rather than inject.
    if not _SAFE_PROFILE_NAME.match(name):
        logger.error("main_profile_get: invalid main_profile_name in config: {!r}", name)
        raise web.HTTPInternalServerError(text=f"Invalid main_profile_name in config: {name!r}")

    # Best-effort: check if credentials file exists in the util container.
    exists = False
    try:
        rc, _ = await exec_in_util(
            app_config,
            f"test -f /profiles/{name}/.credentials.json",
            timeout=5,
        )
        exists = rc == 0
    except Exception as exc:
        # Container not running or transient — report exists=False but log
        # so operators tailing logs can diagnose unexpected errors.
        logger.debug("main_profile_get: exists-check failed ({}); reporting exists=False", exc)
        exists = False

    active_source = config.get("active_main_source")
    return web.json_response({"main_profile_name": name, "exists": exists, "active_main_source": active_source})


async def main_profile_set_handler(request: web.Request) -> web.Response:
    """PUT /api/profiles/main — promote a tracked profile into the main slot.

    Body: {"source_profile": "<tracked-name>"}

    Copies .credentials.json and .claude.json from /profiles/<source> into
    /profiles/<main_profile_name>/ so the main slot inherits both auth and
    the source's onboarding/identity state (userID, oauthAccount, theme,
    hasCompletedOnboarding). Without .claude.json, Claude shows its first-
    run theme picker because the main slot has credentials but no identity.
    Running PTY sessions are not restarted — they will pick up the new
    credential on their next Claude API call.
    """
    app_config: AppConfig = request.app["app_config"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="Invalid JSON")

    source = body.get("source_profile")
    if not source or not isinstance(source, str):
        raise web.HTTPBadRequest(text="'source_profile' field required in body")

    # Validate shape before any further work — names are interpolated into a
    # shell command in the util container.
    if not _SAFE_PROFILE_NAME.match(source):
        raise web.HTTPBadRequest(text=f"Invalid source_profile name: {source!r}")

    # Validate against known profiles to avoid copying from arbitrary paths.
    config = read_config(app_config)
    discovered = request.app.get("discovered_profiles", [])
    config_profiles = config.get("probe_profiles", [])
    all_profiles = set(discovered + config_profiles)
    if source not in all_profiles:
        raise web.HTTPBadRequest(text=f"Profile '{source}' not found in discovered or configured profiles")

    main_name = config.get("main_profile_name") or "main"
    if not _SAFE_PROFILE_NAME.match(main_name):
        logger.error("main_profile_set: invalid main_profile_name in config: {!r}", main_name)
        raise web.HTTPInternalServerError(text=f"Invalid main_profile_name in config: {main_name!r}")
    if source == main_name:
        raise web.HTTPBadRequest(text=f"Cannot promote the main slot '{main_name}' into itself")

    # Copy the credential + identity files inside the util container. `cp -f`
    # overwrites atomically from the perspective of each read call. Directory
    # is created first so a fresh main slot works on first promotion.
    # .claude.json is best-effort — a freshly-authed profile may not have one
    # yet, in which case the main slot keeps whatever was there before.
    copy_cmd = (
        f"sh -c 'mkdir -p /profiles/{main_name} && "
        f"cp -f /profiles/{source}/.credentials.json /profiles/{main_name}/.credentials.json && "
        f"(cp -f /profiles/{source}/.claude.json /profiles/{main_name}/.claude.json || true)'"
    )
    try:
        rc, stdout = await exec_in_util(app_config, copy_cmd, timeout=10)
    except RuntimeError as exc:
        raise web.HTTPServiceUnavailable(text=f"Utility container unavailable: {exc}")
    if rc != 0:
        logger.warning("main_profile_set: copy failed (rc={}): {}", rc, stdout)
        raise web.HTTPInternalServerError(text=f"Failed to copy credentials from '{source}' into main slot")

    config["active_main_source"] = source
    write_config(app_config, config)
    logger.info("main_profile_set: promoted '{}' into main slot '{}'", source, main_name)
    return web.json_response({"main_profile_name": main_name, "source_profile": source, "status": "ok"})


async def claude_usage_handler(request: web.Request) -> web.Response:
    """POST /api/claude-usage

    Accepts {"profile": "name"} in the request body.

    First call for a profile:
      - Creates a ClaudeUsageCard via the service card registry
      - Runs an initial probe (blocking until complete or timeout)
      - Stores the card reference server-side keyed by profile

    Subsequent calls for the same profile:
      - Reuses the existing service card
      - Returns its most recent probe result

    Returns the probe result dict as JSON, or 503 if the probe failed.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="Invalid JSON")

    profile = body.get("profile", "").strip()
    if not profile:
        raise web.HTTPBadRequest(text="'profile' field required in request body")

    app_config: AppConfig = request.app["app_config"]
    registry: ServiceCardRegistry = request.app["service_card_registry"]
    config = read_config(app_config)
    util_cfg = config.get("util_container", {})
    util_name = util_cfg.get("name", "supreme-claudemander-util")
    probe_interval = config.get("probe_interval", 1800)

    card = registry.get("claude-usage", profile)
    if card is None:
        # First call: create card, run initial probe (subscribe calls start() which runs probe)
        def _noop(result):
            pass

        try:
            card = await registry.subscribe(
                "claude-usage",
                profile,
                _noop,
                interval_seconds=probe_interval,
                container=util_name,
            )
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        except Exception:
            logger.exception("claude_usage_handler: failed to create card for '{}'", profile)
            raise web.HTTPInternalServerError(text="Failed to start probe")

    result = card.last_result
    if result is None:
        return web.json_response({"error": "probe failed or timed out"}, status=503)

    return web.json_response(result)


async def probe_claude_usage_handler(request: web.Request) -> web.Response:
    """POST /api/probe/claude-usage?profile=<name>

    Starts a visible ClaudeUsageCard puppet probe. Returns the session_id so the
    frontend can attach a terminal card to the live PTY session.
    """
    profile = request.query.get("profile", "").strip()
    if not profile:
        raise web.HTTPBadRequest(text="profile query parameter required")

    app_config: AppConfig = request.app["app_config"]
    mgr: SessionManager = request.app["session_manager"]
    config = read_config(app_config)
    util_cfg = config.get("util_container", {})
    util_name = util_cfg.get("name", "supreme-claudemander-util")
    probe_timeout = float(config.get("probe_timeout", 90))

    card = ClaudeUsageCard(
        identity=profile,
        session_manager=mgr,
        container=util_name,
        probe_timeout=probe_timeout,
        interval_seconds=999999,
    )
    try:
        session_id = await card.start_visible_probe()
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    except Exception:
        logger.exception("probe_claude_usage_handler: failed to start probe for '{}'", profile)
        raise web.HTTPInternalServerError(text="Failed to start probe")

    logger.info("probe_claude_usage_handler: started visible probe for '{}', session={}", profile, session_id)
    return web.json_response({"session_id": session_id, "profile": profile})


# ── Production Claude terminal control API ─────────────────────────────────


async def _ephemeral_timeout_watcher(session_id: str, timeout_seconds: int, mgr: SessionManager) -> None:
    """Sleep ``timeout_seconds`` then destroy the ephemeral session if still alive."""
    await asyncio.sleep(timeout_seconds)
    session = mgr.get_session(session_id)
    if session is not None and session.alive:
        logger.info("Ephemeral timeout: destroying session {} after {}s", session_id, timeout_seconds)
        mgr.destroy_session(session_id)


async def claude_terminal_create(request: web.Request) -> web.Response:
    """POST /api/claude/terminal/create — create a TerminalCard + PTY session.

    New query params (all optional):
      - ``ephemeral`` — ``"true"`` / ``"false"`` (default false). When true the
        session is NOT registered in CardRegistry (no visible card, no
        card:registered broadcast).  read/write/delete_terminal still work via
        session_id.
      - ``spawner_id`` — card id of the Canvas Claude card that owns this
        terminal. When set, the spawn counts toward the per-spawner cap (10).
      - ``timeout`` — integer seconds (default 60). Only meaningful when
        ``ephemeral=true``; ignored for regular (non-ephemeral) terminals.
        Must be in [1, 120].
    """
    cmd = request.query.get("cmd", "").strip()
    hub = request.query.get("hub", "")
    container = request.query.get("container", "").strip()
    if not cmd:
        raise web.HTTPBadRequest(text="Missing 'cmd' query parameter")

    ephemeral = request.query.get("ephemeral", "false").lower() in ("true", "1", "yes")
    spawner_id = request.query.get("spawner_id", "").strip() or None
    # Epic #236 child 5 (#241): canvas_name records which canvas snapshot
    # the new card belongs to so the write-through hook in
    # ``CardRegistry.apply_state_patch`` can persist mutations to the
    # right ``~/.supreme-claudemander/canvases/{name}.json`` file. The
    # frontend passes the active canvas name; an absent value means the
    # card is not tied to a canvas (no write-through).
    canvas_name = request.query.get("canvas_name", "").strip() or None

    # Validate timeout.  Explicitly passing timeout without ephemeral=true is an
    # error — the parameter has no effect on non-ephemeral terminals and silently
    # accepting it would confuse callers.
    timeout_raw = request.query.get("timeout", "60")
    try:
        timeout = int(timeout_raw)
    except ValueError:
        raise web.HTTPBadRequest(text="'timeout' must be an integer")

    if "timeout" in request.query and not ephemeral:
        return web.json_response(
            {
                "error": "timeout_requires_ephemeral",
                "message": (
                    "timeout is only meaningful with ephemeral=true. "
                    "Use run_task (ephemeral) for timed ops, or open_terminal without timeout for visible terminals."
                ),
            },
            status=400,
        )

    if ephemeral:
        if timeout < 1:
            return web.json_response(
                {
                    "error": "ephemeral_timeout_too_long",
                    "message": (
                        "timeout must be at least 1 second for ephemeral terminals. "
                        "Use open_terminal for long-running work."
                    ),
                    "max_allowed": 120,
                },
                status=400,
            )
        if timeout > 120:
            return web.json_response(
                {
                    "error": "ephemeral_timeout_too_long",
                    "message": (
                        "Timeout > 120s is not permitted for ephemeral terminals — "
                        "ephemeral terminals are for short-running operations (ls, git pull, probes). "
                        "Use open_terminal for longer work."
                    ),
                    "max_allowed": 120,
                },
                status=400,
            )

    try:
        cols = int(request.query.get("cols", 80))
        rows = int(request.query.get("rows", 24))
    except ValueError:
        raise web.HTTPBadRequest(text="'cols' and 'rows' must be integers")

    # Parse optional layout hints before creating the card so they are
    # available in to_descriptor() when the EventBus broadcast fires.
    layout: dict = {}
    try:
        for key in ("x", "y", "w", "h"):
            val = request.query.get(key)
            if val is not None:
                layout[key] = int(val)
    except ValueError:
        raise web.HTTPBadRequest(text="Layout params (x, y, w, h) must be integers")

    mgr: SessionManager = request.app["session_manager"]
    card_registry: CardRegistry = request.app["card_registry"]
    canvas_claude_spawns: dict[str, set[str]] = request.app["canvas_claude_spawns"]
    canvas_claude_spawn_locks: dict[str, asyncio.Lock] = request.app["canvas_claude_spawn_locks"]

    # Cap enforcement: when spawner_id is set, refuse if 10 live sessions already.
    # Acquire a per-spawner lock so the check+add is atomic across concurrent requests.
    if spawner_id is not None:
        spawner_lock = canvas_claude_spawn_locks.setdefault(spawner_id, asyncio.Lock())
        async with spawner_lock:
            live_ids = canvas_claude_spawns.get(spawner_id, set())
            # Prune any session ids that are no longer alive
            live_ids = {sid for sid in live_ids if mgr.get_session(sid) is not None}
            canvas_claude_spawns[spawner_id] = live_ids
            if len(live_ids) >= 10:
                return web.json_response(
                    {
                        "error": "terminal_cap_reached",
                        "message": (
                            "Canvas Claude has reached the 10 live terminal cap. Close one before spawning another."
                        ),
                        "live_session_ids": sorted(live_ids),
                    },
                    status=429,
                )

            # --- Spawn inside the lock so the new session_id is added before release ---
            if ephemeral:
                try:
                    session = mgr.create_session(
                        cmd,
                        hub=hub or None,
                        container=container or None,
                        dimensions=(rows, cols),
                        kind="probe",
                    )
                except Exception:
                    logger.exception("claude_terminal_create (ephemeral): failed for cmd={!r}", cmd)
                    return web.json_response({"error": "Failed to spawn terminal"}, status=500)

                session_id = session.session_id
                canvas_claude_spawns.setdefault(spawner_id, set()).add(session_id)
            else:
                card = TerminalCard(
                    session_manager=mgr,
                    cmd=cmd,
                    hub=hub or None,
                    container=container or None,
                    layout=layout,
                )
                try:
                    await card.start()
                    card_registry.register(card, canvas_name=canvas_name)
                except Exception:
                    logger.exception("claude_terminal_create: failed for cmd={!r}", cmd)
                    return web.json_response({"error": "Failed to spawn terminal"}, status=500)
                canvas_claude_spawns.setdefault(spawner_id, set()).add(card.session_id)
        # Lock released — fall through to common post-spawn logic below.
    else:
        # No spawner_id: no cap enforcement, no lock needed.
        if ephemeral:
            try:
                session = mgr.create_session(
                    cmd,
                    hub=hub or None,
                    container=container or None,
                    dimensions=(rows, cols),
                    kind="probe",
                )
            except Exception:
                logger.exception("claude_terminal_create (ephemeral): failed for cmd={!r}", cmd)
                return web.json_response({"error": "Failed to spawn terminal"}, status=500)
        else:
            card = TerminalCard(
                session_manager=mgr,
                cmd=cmd,
                hub=hub or None,
                container=container or None,
                layout=layout,
            )
            try:
                await card.start()
                card_registry.register(card, canvas_name=canvas_name)
            except Exception:
                logger.exception("claude_terminal_create: failed for cmd={!r}", cmd)
                return web.json_response({"error": "Failed to spawn terminal"}, status=500)

    # --- Common post-spawn logic ---
    if ephemeral:
        session_id = session.session_id  # type: ignore[possibly-undefined]

        # Schedule timeout watcher
        timer_task = asyncio.create_task(
            _ephemeral_timeout_watcher(session_id, timeout, mgr),
            name=f"ephemeral-timeout-{session_id}",
        )
        request.app["ephemeral_timers"][session_id] = timer_task

        logger.info(
            "claude_terminal_create: ephemeral session {} created for cmd={!r} (timeout={}s, spawner={})",
            session_id,
            cmd,
            timeout,
            spawner_id,
        )
        return web.json_response(
            {
                "session_id": session_id,
                "ephemeral": True,
                "cmd": cmd,
            }
        )

    # --- Non-ephemeral response ---
    card = card  # type: ignore[possibly-undefined]

    # Resize if non-default dimensions requested
    if cols != 80 or rows != 24:
        try:
            card.session.pty.setwinsize(rows, cols)
        except Exception:
            pass

    desc = card.to_descriptor()

    logger.info("claude_terminal_create: created {} for cmd={!r}", card.session_id, cmd)
    return web.json_response(desc)


async def claude_terminal_send(request: web.Request) -> web.Response:
    """POST /api/claude/terminal/{id}/send — write text to PTY.

    Works for both regular (card-registered) and ephemeral (session-only) terminals.
    The ``{id}`` path segment is the session_id in both cases.
    """
    card_id = request.match_info["id"]
    card_registry: CardRegistry = request.app["card_registry"]
    mgr: SessionManager = request.app["session_manager"]

    card = card_registry.get_terminal(card_id)
    if card and card.alive:
        session = card.session
    else:
        # Fallback: check session_manager directly (ephemeral session)
        session = mgr.get_session(card_id)
        if not session or not session.alive:
            raise web.HTTPNotFound(text="Terminal not found")

    text = await request.text()
    session.pty.write(text)
    # Touch last_client_time to prevent orphan reaping
    session.last_client_time = time.monotonic()

    return web.json_response({"status": "ok", "sent": len(text)})


async def claude_terminal_read(request: web.Request) -> web.Response:
    """GET /api/claude/terminal/{id}/read — return scrollback (optionally ANSI-stripped).

    Works for both regular (card-registered) and ephemeral (session-only) terminals.
    """
    card_id = request.match_info["id"]
    card_registry: CardRegistry = request.app["card_registry"]
    mgr: SessionManager = request.app["session_manager"]

    card = card_registry.get_terminal(card_id)
    if card and card.alive:
        session = card.session
    else:
        # Fallback: check session_manager directly (ephemeral session)
        session = mgr.get_session(card_id)
        if not session or not session.alive:
            raise web.HTTPNotFound(text="Terminal not found")

    # Touch last_client_time to prevent orphan reaping
    session.last_client_time = time.monotonic()

    data = session.scrollback.get_all()
    output = data.decode("utf-8", errors="replace")

    do_strip = request.query.get("strip_ansi", "").lower() in ("true", "1", "yes")
    if do_strip:
        output = strip_ansi(output)

    return web.json_response(
        {
            "output": output,
            "size": len(data),
            "total_written": session.scrollback.total_written,
        }
    )


async def claude_terminal_status(request: web.Request) -> web.Response:
    """GET /api/claude/terminal/{id}/status — session metadata."""
    card_id = request.match_info["id"]
    card_registry: CardRegistry = request.app["card_registry"]
    card = card_registry.get_terminal(card_id)
    if not card:
        raise web.HTTPNotFound(text="Terminal not found")

    session = card.session
    now = time.monotonic()
    return web.json_response(
        {
            "session_id": card.session_id,
            "cmd": card.cmd,
            "hub": card.hub,
            "container": card.container,
            "alive": card.alive,
            "client_count": len(session.clients) if session else 0,
            "scrollback_size": session.scrollback.size if session else 0,
            "age_seconds": int(now - session.created_at) if session else 0,
            "idle_seconds": int(now - session.last_client_time) if session else 0,
        }
    )


async def claude_terminal_delete(request: web.Request) -> web.Response:
    """DELETE /api/claude/terminal/{id} — stop card, clean up.

    Works for both regular (card-registered) and ephemeral (session-only)
    terminals.  For ephemerals the on_destroy hook handles spawner-set cleanup
    and timer cancellation automatically.
    """
    card_id = request.match_info["id"]
    card_registry: CardRegistry = request.app["card_registry"]
    mgr: SessionManager = request.app["session_manager"]

    card = card_registry.get_terminal(card_id)
    if card:
        # Regular terminal: stop via card lifecycle, then unregister
        await card.stop()
        card_registry.unregister(card_id)
        logger.info("claude_terminal_delete: removed card {}", card_id)
        return web.json_response({"status": "ok"})

    # Ephemeral (or any) session not in card_registry — look up by session_id
    session = mgr.get_session(card_id)
    if not session:
        raise web.HTTPNotFound(text="Terminal not found")

    mgr.destroy_session(card_id)
    logger.info("claude_terminal_delete: destroyed ephemeral session {}", card_id)
    return web.json_response({"status": "ok"})


async def _apply_card_state_patch(request: web.Request, card_id: str, fields: dict) -> dict:
    """Shared body of the generic + legacy state-mutation handlers.

    This is the single authoritative code path for mutating server-owned card
    state (see ``docs/state-model.md`` and epic #236 / issue #238). Both the
    generic ``PUT /api/cards/{id}/state`` handler and the legacy
    ``/rename`` + ``/recovery-script`` aliases funnel through here so exactly
    one implementation performs allowlist validation, attribute mutation via
    ``CardRegistry.apply_state_patch``, and the ``card_updated`` broadcast.

    Raises ``web.HTTPNotFound`` / ``web.HTTPBadRequest`` for the caller to
    propagate. Returns the dict of applied fields.
    """
    card_registry: CardRegistry = request.app["card_registry"]
    try:
        applied = card_registry.apply_state_patch(card_id, fields)
    except LookupError:
        raise web.HTTPNotFound(text="Card not found")
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))

    if applied:
        await _broadcast_card_updated(request.app, card_id, applied)
    return applied


async def cards_state_put(request: web.Request) -> web.Response:
    """PUT /api/cards/{id}/state — generic server-owned state mutation.

    Accepts a partial JSON dict of card state fields. Each field is validated
    against the target card's ``MUTABLE_FIELDS`` allowlist; unknown fields get
    HTTP 400. On success the patched fields are broadcast to every
    ``/ws/control`` client via ``card_updated``.

    This endpoint is the structural embodiment of Decision Prior DP-2 from
    epic #236 — a single generic mutation path, not per-field endpoints.
    """
    card_id = request.match_info["id"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="Invalid JSON")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="Body must be a JSON object")

    applied = await _apply_card_state_patch(request, card_id, body)
    logger.info("cards_state_put: {} patched fields={}", card_id, list(applied.keys()))
    return web.json_response({"status": "ok", **applied})


async def claude_terminal_rename(request: web.Request) -> web.Response:
    """PUT /api/claude/terminal/{id}/rename — legacy alias for display_name.

    Thin wrapper around the generic ``PUT /api/cards/{id}/state`` path.
    Retained for one release per epic #236 invariant I-6 so MCP callers
    (``mcp_server.py``) keep working. The body ``{"display_name": ...}`` is
    translated into the generic patch shape and delegated to the shared
    ``_apply_card_state_patch`` helper.
    """
    card_id = request.match_info["id"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="Invalid JSON")

    display_name = body.get("display_name", "")
    # Preserve the legacy 404-for-terminal-only semantics so existing tests
    # keep passing: if the card exists but isn't a terminal, the legacy URL
    # must still report "not found".
    card_registry: CardRegistry = request.app["card_registry"]
    if card_registry.get_terminal(card_id) is None:
        raise web.HTTPNotFound(text="Terminal not found")

    applied = await _apply_card_state_patch(request, card_id, {"display_name": display_name})
    logger.info("claude_terminal_rename: {} -> {!r}", card_id, display_name)
    return web.json_response({"status": "ok", "display_name": applied.get("display_name", display_name)})


async def claude_terminal_recovery_get(request: web.Request) -> web.Response:
    """GET /api/claude/terminal/{id}/recovery-script — read recovery script."""
    card_id = request.match_info["id"]
    card_registry: CardRegistry = request.app["card_registry"]
    card = card_registry.get_terminal(card_id)
    if not card:
        raise web.HTTPNotFound(text="Terminal not found")

    return web.json_response({"recovery_script": card.recovery_script})


async def claude_terminal_recovery_put(request: web.Request) -> web.Response:
    """PUT /api/claude/terminal/{id}/recovery-script — legacy alias for recovery_script.

    Thin wrapper around the generic ``PUT /api/cards/{id}/state`` path.
    Retained for one release per epic #236 invariant I-6.
    """
    card_id = request.match_info["id"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="Invalid JSON")

    script = body.get("recovery_script", "")
    card_registry: CardRegistry = request.app["card_registry"]
    if card_registry.get_terminal(card_id) is None:
        raise web.HTTPNotFound(text="Terminal not found")

    applied = await _apply_card_state_patch(request, card_id, {"recovery_script": script})
    logger.info("claude_terminal_recovery_put: {} -> {!r}", card_id, script[:80])
    return web.json_response({"status": "ok", "recovery_script": applied.get("recovery_script", script)})


async def claude_terminals_list(request: web.Request) -> web.Response:
    """GET /api/claude/terminals — list all terminal cards."""
    card_registry: CardRegistry = request.app["card_registry"]
    terminals = card_registry.list_terminals()

    result = []
    now = time.monotonic()
    for card in terminals:
        desc = card.to_descriptor()
        session = card.session
        desc["alive"] = card.alive
        # Always include display_name and recovery_script for MCP consumers
        desc["display_name"] = card.display_name
        desc["recovery_script"] = card.recovery_script
        if session:
            desc["age_seconds"] = int(now - session.created_at)
            desc["idle_seconds"] = int(now - session.last_client_time)
            desc["scrollback_size"] = session.scrollback.size
        result.append(desc)

    return web.json_response(result)


# ── Canvas Claude Code API ────────────────────────────────────────────────────


async def canvas_claude_create(request: web.Request) -> web.Response:
    """POST /api/canvas-claude/create — create a CanvasClaudeCard + PTY session."""
    hub = request.query.get("hub", "canvas-claude")
    container = request.query.get("container", "supreme-claudemander-util").strip()
    profile = request.query.get("profile", "").strip() or None
    canvas_name = request.query.get("canvas_name", "").strip() or None
    api_base_url = request.query.get("api_base_url", "http://host.docker.internal:3000").strip()

    # Fall back to the configured main profile slot when none was specified.
    # The main slot is always a valid target; its credentials are populated
    # via PUT /api/profiles/main. If the file is absent the Canvas Claude
    # card will surface a retry overlay on failed auth — we do not 400 here.
    if not profile:
        app_cfg: AppConfig = request.app["app_config"]
        cfg = read_config(app_cfg)
        profile = cfg.get("main_profile_name") or "main"

    layout: dict = {}
    try:
        for key in ("x", "y", "w", "h"):
            val = request.query.get(key)
            if val is not None:
                layout[key] = int(val)
    except ValueError:
        raise web.HTTPBadRequest(text="Layout params (x, y, w, h) must be integers")

    mgr: SessionManager = request.app["session_manager"]
    card_registry: CardRegistry = request.app["card_registry"]

    card = CanvasClaudeCard(
        session_manager=mgr,
        hub=hub or None,
        container=container or None,
        layout=layout,
        api_base_url=api_base_url,
        profile=profile,
        canvas_name=canvas_name,
    )
    try:
        await card.start()
        card_registry.register(card, canvas_name=canvas_name)
    except Exception:
        logger.exception("canvas_claude_create: failed to start card")
        return web.json_response({"error": "Failed to spawn Canvas Claude card"}, status=500)

    desc = card.to_descriptor()
    logger.info("canvas_claude_create: created {}", card.session_id)
    return web.json_response(desc)


async def canvas_claude_new_session(request: web.Request) -> web.Response:
    """POST /api/canvas-claude/{id}/new-session — restart the claude process.

    Unregisters the card under the old session_id before restarting, then
    re-registers under the new session_id so the registry stays consistent.
    """
    card_id = request.match_info["id"]
    card_registry: CardRegistry = request.app["card_registry"]
    card = card_registry.get_canvas_claude(card_id)
    if not card:
        raise web.HTTPNotFound(text="Canvas Claude card not found")
    # Preserve canvas membership across the restart so write-through still
    # targets the right canvas snapshot after the new session_id is assigned.
    canvas_name = card_registry.get_canvas_name(card_id)
    try:
        card_registry.unregister(card_id)
        await card.new_session()
        card_registry.register(card, canvas_name=canvas_name)
    except Exception:
        logger.exception("canvas_claude_new_session: failed for {}", card_id)
        # Re-register the card so it is not orphaned — use whatever id it
        # currently has (may be the old one if new_session() failed before
        # allocating a new PTY).
        try:
            card_registry.register(card, canvas_name=canvas_name)
        except Exception:
            logger.exception("canvas_claude_new_session: failed to re-register card {}", card_id)
        return web.json_response({"error": "Failed to restart session"}, status=500)
    return web.json_response({"status": "ok", "session_id": card.session_id})


async def canvas_claude_clear(request: web.Request) -> web.Response:
    """POST /api/canvas-claude/{id}/clear — send /clear to the claude PTY."""
    card_id = request.match_info["id"]
    card_registry: CardRegistry = request.app["card_registry"]
    card = card_registry.get_canvas_claude(card_id)
    if not card:
        raise web.HTTPNotFound(text="Canvas Claude card not found")
    await card.clear_session()
    return web.json_response({"status": "ok"})


# ── Blueprint API ──────────────────────────────────────────────────────────


async def blueprints_list_handler(request: web.Request) -> web.Response:
    """GET /api/blueprints — list all blueprints."""
    app_config: AppConfig = request.app["app_config"]
    names = blueprint_mod.list_blueprints(app_config)
    return web.json_response(names)


async def blueprint_get_handler(request: web.Request) -> web.Response:
    """GET /api/blueprints/{name} — get a single blueprint."""
    name = request.match_info["name"]
    app_config: AppConfig = request.app["app_config"]
    data = blueprint_mod.read_blueprint(app_config, name)
    if data is None:
        raise web.HTTPNotFound(text=f"Blueprint '{name}' not found")
    return web.json_response(data)


async def blueprint_create_handler(request: web.Request) -> web.Response:
    """POST /api/blueprints — create a new blueprint."""
    app_config: AppConfig = request.app["app_config"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="Invalid JSON")

    name = body.get("name", "").strip()
    if not name:
        raise web.HTTPBadRequest(text="Blueprint must have a 'name' field")

    # Check if already exists
    existing = blueprint_mod.read_blueprint(app_config, name)
    if existing is not None:
        raise web.HTTPConflict(text=f"Blueprint '{name}' already exists")

    ok = blueprint_mod.write_blueprint(app_config, name, body)
    if not ok:
        raise web.HTTPBadRequest(text=f"Invalid blueprint name '{name}'")
    return web.json_response(body, status=201)


async def blueprint_update_handler(request: web.Request) -> web.Response:
    """PUT /api/blueprints/{name} — update an existing blueprint."""
    name = request.match_info["name"]
    app_config: AppConfig = request.app["app_config"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="Invalid JSON")

    ok = blueprint_mod.write_blueprint(app_config, name, body)
    if not ok:
        raise web.HTTPBadRequest(text=f"Invalid blueprint name '{name}'")
    return web.json_response(body)


async def blueprint_delete_handler(request: web.Request) -> web.Response:
    """DELETE /api/blueprints/{name} — delete a blueprint."""
    name = request.match_info["name"]
    app_config: AppConfig = request.app["app_config"]
    ok = blueprint_mod.delete_blueprint(app_config, name)
    if not ok:
        raise web.HTTPNotFound(text=f"Blueprint '{name}' not found")
    return web.json_response({"status": "ok", "name": name})


async def blueprint_validate_handler(request: web.Request) -> web.Response:
    """POST /api/blueprints/validate — pre-spawn validation."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="Invalid JSON")

    blueprint_data = body.get("blueprint")
    if not blueprint_data:
        raise web.HTTPBadRequest(text="Request must include 'blueprint' field")

    context = body.get("context", {})
    result = blueprint_mod.validate_blueprint(blueprint_data, context)
    return web.json_response(result)


async def blueprint_spawn_handler(request: web.Request) -> web.Response:
    """POST /api/blueprints/spawn — create a BlueprintCard and begin execution."""
    app_config: AppConfig = request.app["app_config"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="Invalid JSON")

    # Load blueprint by name or use inline definition
    blueprint_data = body.get("blueprint")
    bp_name = body.get("name", "").strip()

    if not blueprint_data and bp_name:
        blueprint_data = blueprint_mod.read_blueprint(app_config, bp_name)
        if blueprint_data is None:
            raise web.HTTPNotFound(text=f"Blueprint '{bp_name}' not found")
    elif not blueprint_data:
        raise web.HTTPBadRequest(text="Provide 'name' or 'blueprint' in request body")

    context = body.get("context", {})

    # Validate before spawning
    validation = blueprint_mod.validate_blueprint(blueprint_data, context)
    if not validation["valid"]:
        return web.json_response(
            {"error": "Validation failed", "errors": validation["errors"]},
            status=400,
        )

    # Parse optional layout hints
    layout = {}
    for key in ("x", "y", "w", "h"):
        val = body.get(key)
        if val is not None:
            try:
                layout[key] = int(val)
            except (ValueError, TypeError):
                raise web.HTTPBadRequest(text=f"Layout param '{key}' must be an integer")

    card_registry: CardRegistry = request.app["card_registry"]
    event_bus: EventBus = request.app["event_bus"]

    card = BlueprintCard(
        blueprint=blueprint_data,
        app=request.app,
        context=context,
    )
    card.bus = event_bus
    card.layout = layout
    card_registry.register(card)

    try:
        await card.start()
    except Exception:
        logger.exception("blueprint_spawn: failed to start BlueprintCard")
        card_registry.unregister(card.id)
        return web.json_response({"error": "Failed to start blueprint"}, status=500)

    desc = card.to_descriptor()
    logger.info("blueprint_spawn: created BlueprintCard {} for '{}'", card.id, blueprint_data.get("name"))
    return web.json_response(desc, status=201)


# ── Control WebSocket — broadcast card lifecycle to frontends ─────────────


async def ws_control_handler(request: web.Request) -> web.WebSocketResponse:
    """GET /ws/control — WebSocket that broadcasts card_created/card_deleted events.

    The frontend connects on page load.  The server subscribes to EventBus
    ``card:registered`` and ``card:unregistered`` and pushes JSON text frames
    to every connected client.
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    logger.info("Control WebSocket connected from {}", request.remote)

    control_clients: list = request.app["control_ws_clients"]
    control_clients.append(ws)

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                # Route blueprint:widget_ack from the frontend to the EventBus
                # so BlueprintCard._step_open_widget() receives the ack.
                try:
                    data = json.loads(msg.data)
                    msg_type = data.get("type", "")
                    if msg_type == "blueprint:widget_ack":
                        event_bus: EventBus | None = request.app.get("event_bus")
                        if event_bus:
                            await event_bus.emit("blueprint:widget_ack", data)
                except (json.JSONDecodeError, Exception):
                    pass
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        control_clients.remove(ws)
        logger.info("Control WebSocket disconnected from {}", request.remote)

    return ws


async def _broadcast_card_event(app: web.Application, event_type: str, payload: dict) -> None:
    """Push a card lifecycle event to all connected /ws/control clients."""
    if event_type == "card:registered":
        msg_type = "card_created"
    elif event_type == "card:unregistered":
        msg_type = "card_deleted"
    else:
        return

    # Build the message.  For card_created we include the full descriptor
    # so the frontend can spawn the card without a round-trip.
    message: dict = {"type": msg_type, "card_id": payload.get("card_id"), "card_type": payload.get("card_type")}

    if msg_type == "card_created":
        card_registry: CardRegistry = app["card_registry"]
        card = card_registry.get(payload["card_id"])
        if card is not None and hasattr(card, "to_descriptor"):
            message["descriptor"] = card.to_descriptor()

    data = json.dumps(message)
    clients: list = app.get("control_ws_clients", [])
    for ws in list(clients):
        if not ws.closed:
            try:
                await ws.send_str(data)
            except Exception:
                logger.debug("Failed to send control event to a client")


async def _broadcast_card_updated(app: web.Application, card_id: str, fields: dict) -> None:
    """Push a card_updated event to all connected /ws/control clients."""
    message = {"type": "card_updated", "card_id": card_id, **fields}
    data = json.dumps(message)
    clients: list = app.get("control_ws_clients", [])
    for ws in list(clients):
        if not ws.closed:
            try:
                await ws.send_str(data)
            except Exception:
                logger.debug("Failed to send card_updated event to a client")


async def hydrate_canvas_into_registry(
    app: web.Application,
    canvas_name: str,
    retry_delays: tuple[float, ...] | list[float] | None = None,
) -> int:
    """Load a canvas JSON file and populate ``CardRegistry`` with its cards.

    Epic #254 child 2 (#257): the boot hydration path. For each entry in the
    snapshot whose ``type`` is supported by a hydration-capable card class,
    build the card via ``from_descriptor`` and register it. For TerminalCards,
    also invoke ``start()`` — which wraps ``SessionManager.create_session`` in
    a bounded jittered retry and, on exhaustion, lands the card in
    ``error_state`` (broadcast via ``_broadcast_card_updated``).

    Returns the number of cards hydrated (registered, regardless of PTY
    success). Missing / malformed / unknown-type entries are logged and
    skipped so a single bad entry does not abort startup.

    Only TerminalCard dispatch ships in this child; Children #5/#6 extend the
    dispatch table for Widget and CanvasClaude types.
    """
    app_config: AppConfig = app["app_config"]
    data = read_canvas(app_config, canvas_name)
    if data is None:
        logger.warning("hydrate_canvas_into_registry: canvas '{}' not found or invalid", canvas_name)
        return 0
    cards = data.get("cards") if isinstance(data, dict) else None
    if not isinstance(cards, list):
        logger.warning("hydrate_canvas_into_registry: canvas '{}' has no 'cards' list", canvas_name)
        return 0

    card_registry: CardRegistry = app["card_registry"]
    session_manager: SessionManager = app["session_manager"]
    hydrated = 0

    for idx, entry in enumerate(cards):
        if not isinstance(entry, dict):
            logger.warning(
                "hydrate_canvas_into_registry: canvas '{}' entry #{} is not an object, skipping",
                canvas_name,
                idx,
            )
            continue
        card_type = entry.get("type")
        try:
            if card_type == "terminal":
                card = TerminalCard.from_descriptor(entry, session_manager=session_manager)
            else:
                # Children #5/#6 extend dispatch here for widget / canvas-claude.
                logger.debug(
                    "hydrate_canvas_into_registry: canvas '{}' entry #{} type '{}' has no hydrator, skipping",
                    canvas_name,
                    idx,
                    card_type,
                )
                continue
        except Exception:
            logger.exception(
                "hydrate_canvas_into_registry: canvas '{}' entry #{} from_descriptor failed",
                canvas_name,
                idx,
            )
            continue

        # On retry-ceiling error, emit a card_updated broadcast so any
        # observer (Child #3 client, MCP probe) sees the new error_state.
        def _on_error_state(c: BaseCard, _app=app) -> "object":
            return _broadcast_card_updated(_app, c.id, {"error_state": c.error_state})

        try:
            await card.start(retry_delays=retry_delays, on_error_state=_on_error_state)
        except TypeError:
            # Cards whose ``start`` signature does not accept retry kwargs
            # (e.g. non-TerminalCard hydration targets added by Children #5/#6
            # before they gain the retry contract) fall back to a bare start.
            try:
                await card.start()
            except Exception:
                logger.exception(
                    "hydrate_canvas_into_registry: canvas '{}' entry #{} start() failed",
                    canvas_name,
                    idx,
                )
        except Exception:
            logger.exception(
                "hydrate_canvas_into_registry: canvas '{}' entry #{} start() failed",
                canvas_name,
                idx,
            )

        card_registry.register(card, canvas_name=canvas_name)
        hydrated += 1

    logger.info("hydrate_canvas_into_registry: canvas '{}' hydrated {} card(s)", canvas_name, hydrated)
    return hydrated


async def _broadcast_blueprint_event(app: web.Application, event_type: str, payload: dict) -> None:
    """Push blueprint events (log, completed, failed, open_widget) to /ws/control clients."""
    message = {"type": event_type, **payload}
    data = json.dumps(message)
    clients: list = app.get("control_ws_clients", [])
    for ws in list(clients):
        if not ws.closed:
            try:
                await ws.send_str(data)
            except Exception:
                logger.debug("Failed to send blueprint event to a client")


def create_app(
    app_config: AppConfig,
    test_mode: bool = False,
    skip_canvas_schema_check: bool = False,
) -> web.Application:
    app = web.Application()
    app["app_config"] = app_config
    app["test_mode"] = test_mode
    # Epic #236 child 5 (#241): the canvas-schema check is opt-out at create
    # time so test fixtures and dev-config mode don't trip it. The flag may
    # also be set by ``__main__.main`` after ``create_app`` returns.
    app["_skip_canvas_schema_check"] = skip_canvas_schema_check
    app["discovered_profiles"] = []
    app["control_ws_clients"] = []
    # Per-spawner live session tracking for Canvas Claude terminal cap
    app["canvas_claude_spawns"]: dict[str, set[str]] = {}
    # Per-spawner asyncio.Lock to make the cap-check+add atomic
    app["canvas_claude_spawn_locks"]: dict[str, asyncio.Lock] = {}
    # Global lock for the 4-container cap (#205): serialises the count-check +
    # create so concurrent requests can't race past the cap.
    app["container_create_lock"] = asyncio.Lock()
    # Pending ephemeral timeout tasks keyed by session_id
    app["ephemeral_timers"]: dict[str, asyncio.Task] = {}

    # Static + API routes
    app.router.add_get("/", index_handler)
    app.router.add_get("/api/hubs", hubs_handler)
    app.router.add_get("/api/startup", startup_handler)
    app.router.add_get("/api/config", config_get_handler)
    app.router.add_put("/api/config", config_put_handler)
    app.router.add_get("/api/canvases", canvases_list_handler)
    app.router.add_get("/api/canvases/{name}", canvas_get_handler)
    # Epic #236 child 5 (#241): no PUT — canvas JSON is server-authored.
    app.router.add_delete("/api/canvases/{name}", canvas_delete_handler)

    # Generic card state mutation (epic #236 / issue #238 — single mutation path)
    app.router.add_put("/api/cards/{id}/state", cards_state_put)
    # Epic #254 child 2 (#257): server-authored card listing for observers
    # before any browser attaches.
    app.router.add_get("/api/cards", cards_list_handler)
    app.router.add_get("/api/widgets/system-info", widget_system_info_handler)
    app.router.add_get("/api/widgets/container-stats", widget_container_stats_handler)

    # Container Manager API
    app.router.add_get("/api/containers/discover", container_discover_handler)
    app.router.add_get("/api/containers/{name}/stats", container_single_stats_handler)
    app.router.add_get("/api/containers/favorites", container_favorites_get_handler)
    app.router.add_put("/api/containers/favorites", container_favorites_put_handler)
    app.router.add_post("/api/containers/{name}/start", container_start_handler)
    app.router.add_post("/api/containers/{name}/stop", container_stop_handler)
    app.router.add_put("/api/containers/favorites/{name}/actions", container_favorites_actions_put_handler)
    app.router.add_post("/api/containers/create", container_create_handler)
    app.router.add_post("/api/containers/{name}/rebuild", container_rebuild_handler)

    app.router.add_get("/api/profiles", profiles_list_handler)
    app.router.add_get("/api/profiles/discover", profiles_discover_handler)
    app.router.add_get("/api/profiles/main", main_profile_get_handler)
    app.router.add_put("/api/profiles/main", main_profile_set_handler)
    app.router.add_post("/api/claude-usage", claude_usage_handler)
    app.router.add_post("/api/probe/claude-usage", probe_claude_usage_handler)

    # Claude terminal control API (production)
    app.router.add_post("/api/claude/terminal/create", claude_terminal_create)
    app.router.add_post("/api/claude/terminal/{id}/send", claude_terminal_send)
    app.router.add_get("/api/claude/terminal/{id}/read", claude_terminal_read)
    app.router.add_get("/api/claude/terminal/{id}/status", claude_terminal_status)
    app.router.add_put("/api/claude/terminal/{id}/rename", claude_terminal_rename)
    app.router.add_get("/api/claude/terminal/{id}/recovery-script", claude_terminal_recovery_get)
    app.router.add_put("/api/claude/terminal/{id}/recovery-script", claude_terminal_recovery_put)
    app.router.add_delete("/api/claude/terminal/{id}", claude_terminal_delete)
    app.router.add_get("/api/claude/terminals", claude_terminals_list)

    # Canvas Claude Code API
    app.router.add_post("/api/canvas-claude/create", canvas_claude_create)
    app.router.add_post("/api/canvas-claude/{id}/new-session", canvas_claude_new_session)
    app.router.add_post("/api/canvas-claude/{id}/clear", canvas_claude_clear)

    # Blueprint API
    app.router.add_get("/api/blueprints", blueprints_list_handler)
    app.router.add_post("/api/blueprints", blueprint_create_handler)
    app.router.add_post("/api/blueprints/validate", blueprint_validate_handler)
    app.router.add_post("/api/blueprints/spawn", blueprint_spawn_handler)
    app.router.add_get("/api/blueprints/{name}", blueprint_get_handler)
    app.router.add_put("/api/blueprints/{name}", blueprint_update_handler)
    app.router.add_delete("/api/blueprints/{name}", blueprint_delete_handler)

    # Session routes (must be before /ws/{hub} catch-all)
    app.router.add_get("/api/sessions", sessions_list_handler)
    app.router.add_get("/ws/session/new", session_new_handler)
    app.router.add_get("/ws/session/{session_id}", session_attach_handler)
    app.router.add_get("/ws/exec", exec_websocket_handler)
    app.router.add_get("/ws/control", ws_control_handler)

    # Test puppeting API (only in test mode)
    if test_mode:
        app.router.add_post("/api/test/session/create", test_session_create)
        app.router.add_post("/api/test/session/{id}/send", test_session_send)
        app.router.add_get("/api/test/session/{id}/read", test_session_read)
        app.router.add_get("/api/test/session/{id}/status", test_session_status)
        app.router.add_delete("/api/test/session/{id}", test_session_delete)
        app.router.add_get("/api/test/sessions", test_sessions_list)
        app.router.add_put("/api/test/containers", test_containers_put)
        app.router.add_get("/api/test/containers", test_containers_get)
        app.router.add_post("/api/test/canvases/{name}", test_canvas_seed)

    # Lifecycle hooks
    async def on_startup(app: web.Application) -> None:
        # Epic #236 child 5 (#241): refuse to boot when any canvas file is in
        # the pre-epic schema and lacks a backup sidecar — the user must run
        # ``python -m claude_rts --migrate-canvases`` first. The check is
        # opt-out for ``--dev-config`` (preset canvases are wiped+rebuilt on
        # every startup so they never carry old state) and the test-mode
        # harness (which routinely instantiates servers with no canvases dir).
        if not app.get("_skip_canvas_schema_check"):
            blocking = canvas_236.check_canvas_dir(app_config.canvases_dir)
            if blocking:
                # Log every offending file then raise — aiohttp converts the
                # exception into a startup failure with a non-zero exit code.
                for path in blocking:
                    logger.error(canvas_236.STARTUP_ERROR_TEMPLATE.format(path=path))
                raise RuntimeError(canvas_236.STARTUP_ERROR_TEMPLATE.format(path=blocking[0]))

        config = read_config(app_config)
        session_config = config.get("sessions", {})
        mgr = SessionManager(
            orphan_timeout=session_config.get("orphan_timeout", 300),
            scrollback_size=session_config.get("scrollback_size", 65536),
            tmux_enabled=session_config.get("tmux_persistence", True),
        )
        app["session_manager"] = mgr
        event_bus = EventBus()
        app["event_bus"] = event_bus
        registry = ServiceCardRegistry(session_manager=mgr)
        registry.register_type("claude-usage", ClaudeUsageCard)
        app["service_card_registry"] = registry

        # Epic #236 child 5 (#241): wire the write-through persistence hook.
        # Whenever ``apply_state_patch`` mutates a card belonging to a known
        # canvas, the registry calls ``_persist_canvas(canvas_name)`` which
        # rewrites ``~/.supreme-claudemander/canvases/{canvas_name}.json``
        # from the live registry state. This is the single disk-write path
        # for canvas snapshots — no other call site invokes
        # ``write_state_snapshot`` directly.
        card_registry = CardRegistry(bus=event_bus)
        app["card_registry"] = card_registry

        def _persist_canvas_snapshot(canvas_name: str) -> None:
            cards = card_registry.cards_on_canvas(canvas_name)
            descriptors = []
            for card in cards:
                # Hidden cards (ServiceCards) and any card whose subclass does
                # not define ``to_descriptor`` are skipped — only visible
                # cards belong in the canvas snapshot.
                if getattr(card, "hidden", False):
                    continue
                if not hasattr(card, "to_descriptor"):
                    continue
                # Issue #194 / epic #236: only starred cards persist across
                # reload. Unstarred cards are ephemeral — they participate in
                # broadcasts while live, but are excluded from the canvas
                # JSON snapshot so a reload starts with a clean slate. The
                # pre-#241 saveLayout() applied the same filter
                # (``cards.filter(c => c.starred)``).
                if not getattr(card, "starred", False):
                    continue
                try:
                    desc = card.to_descriptor()
                except Exception:
                    logger.exception("persist_callback: card '{}' to_descriptor failed", card.id)
                    continue
                # Epic #254 child 2 (#257): ``error_state`` is server-computed
                # recovery metadata (kind/attempts/last_error) and must NOT be
                # persisted to the canvas JSON snapshot — retries reset to 0
                # on every server restart. Strip it here so the snapshot stays
                # clean even though ``to_descriptor`` includes it for the GET
                # endpoint and ``card_updated`` broadcast.
                desc.pop("error_state", None)
                descriptors.append(desc)
            write_state_snapshot(app_config, canvas_name, descriptors)

        card_registry.set_persist_callback(_persist_canvas_snapshot)

        # Wire EventBus → /ws/control broadcast
        async def _on_card_event(event_type: str, payload: dict) -> None:
            await _broadcast_card_event(app, event_type, payload)

        event_bus.subscribe("card:registered", _on_card_event)
        event_bus.subscribe("card:unregistered", _on_card_event)

        # Wire SessionManager on_destroy → spawner tracking + timer cancellation
        def _on_session_destroy(session_id: str, session) -> None:
            """Prune destroyed session from spawner sets and cancel timeout timer."""
            canvas_claude_spawns: dict[str, set[str]] = app["canvas_claude_spawns"]
            for spawner_set in canvas_claude_spawns.values():
                spawner_set.discard(session_id)

            ephemeral_timers: dict[str, asyncio.Task] = app["ephemeral_timers"]
            timer = ephemeral_timers.pop(session_id, None)
            if timer is not None and not timer.done():
                timer.cancel()

        mgr.add_on_destroy(_on_session_destroy)

        # On CanvasClaudeCard unregister: destroy all sessions owned by that spawner
        async def _on_canvas_claude_unregistered(event_type: str, payload: dict) -> None:
            if payload.get("card_type") != "canvas-claude":
                return
            spawner_id = payload.get("card_id")
            if not spawner_id:
                return
            canvas_claude_spawns: dict[str, set[str]] = app["canvas_claude_spawns"]
            owned = canvas_claude_spawns.pop(spawner_id, set())
            # Remove the per-spawner lock entry (lock is never re-used after card removal)
            app["canvas_claude_spawn_locks"].pop(spawner_id, None)
            session_manager: SessionManager = app["session_manager"]
            for sid in list(owned):
                if session_manager.get_session(sid) is not None:
                    logger.info(
                        "canvas-claude unregistered: destroying session {} (spawner={})",
                        sid,
                        spawner_id,
                    )
                    session_manager.destroy_session(sid)

        event_bus.subscribe("card:unregistered", _on_canvas_claude_unregistered)

        # Wire blueprint events → /ws/control broadcast
        async def _on_blueprint_event(event_type: str, payload: dict) -> None:
            await _broadcast_blueprint_event(app, event_type, payload)

        event_bus.subscribe("blueprint:log", _on_blueprint_event)
        event_bus.subscribe("blueprint:completed", _on_blueprint_event)
        event_bus.subscribe("blueprint:failed", _on_blueprint_event)
        event_bus.subscribe("blueprint:open_widget", _on_blueprint_event)

        mgr.start_orphan_reaper()

        # Epic #254 child 2 (#257): server-authored hydration — iterate every
        # canvas JSON file and populate ``CardRegistry`` from the snapshot
        # before any browser connects. Starred cards whose containers are
        # unavailable enter ``error_state`` after a bounded retry; the new
        # ``GET /api/cards?canvas=X`` endpoint exposes the live state to MCP
        # tools and monitoring observers independent of any WebSocket attach.
        #
        # Retry runs as a background task so ``on_startup`` does not block
        # the event loop for up to ~130s waiting on a missing container.
        hydrate_retry_delays = app.get("_hydrate_retry_delays")
        canvas_names = list_canvases(app_config)
        for _cn in canvas_names:
            try:
                await hydrate_canvas_into_registry(app, _cn, retry_delays=hydrate_retry_delays)
            except Exception:
                logger.exception("on_startup: hydrate_canvas_into_registry failed for '{}'", _cn)

        # Probe tmux availability and recover existing sessions
        if mgr.tmux_enabled:
            try:
                hubs = await discover_hubs()
                for hub in hubs:
                    await mgr.probe_tmux(hub["container"])
                recovered = await mgr.recover_tmux_sessions(hubs)
                if recovered:
                    logger.info("Recovered {} tmux session(s) from running containers", recovered)
            except Exception:
                logger.warning("Failed to recover tmux sessions on startup (non-fatal)")

        try:
            await ensure_util_container(app_config)
        except Exception:
            logger.warning("Failed to start utility container (non-fatal)")

        # Probe tmux availability in the util container
        config = read_config(app_config)
        util_cfg = config.get("util_container", {})
        util_name = util_cfg.get("name", "supreme-claudemander-util")
        if mgr.tmux_enabled:
            await mgr.probe_tmux(util_name)

        # Discover profiles from util container
        try:
            discovered = await discover_profiles(app_config)
            app["discovered_profiles"] = discovered
            logger.info("Auto-discovered {} profile(s): {}", len(discovered), discovered)
        except Exception:
            logger.warning("Profile discovery failed (non-fatal)")
            app["discovered_profiles"] = []

        # Start claude-usage probes for discovered + configured profiles
        probe_interval = config.get("probe_interval", 1800)
        config_profiles = config.get("probe_profiles", [])
        all_profiles = sorted(set(app["discovered_profiles"] + config_profiles))
        for profile in all_profiles:

            def _log_usage(result, _p=profile):
                logger.info(
                    "claude-usage [{}]: 5hr={}% 7d={}% burn={}/day resets={}",
                    _p,
                    result.get("five_hour_pct"),
                    result.get("seven_day_pct"),
                    result.get("burn_rate"),
                    result.get("seven_day_resets"),
                )

            async def _start_probe(_p=profile, _cb=_log_usage):
                try:
                    await registry.subscribe(
                        "claude-usage",
                        _p,
                        _cb,
                        container=util_name,
                        interval_seconds=probe_interval,
                    )
                except Exception:
                    logger.exception("Failed to start claude-usage probe for profile '{}'", _p)

            asyncio.create_task(_start_probe())

    async def on_shutdown(app: web.Application) -> None:
        # Close all control WebSocket clients
        clients = app.get("control_ws_clients", [])
        for ws in list(clients):
            if not ws.closed:
                await ws.close()
        clients.clear()

        if "card_registry" in app:
            await app["card_registry"].stop_all()
        if "service_card_registry" in app:
            await app["service_card_registry"].stop_all()
        if "event_bus" in app:
            app["event_bus"].clear()
        if "session_manager" in app:
            app["session_manager"].stop_all()

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    logger.info("Application routes registered")
    return app
