"""aiohttp server: static files, hub discovery API, WebSocket-to-docker bridge via ConPTY."""

import asyncio
import json
import pathlib
import platform
import sys
import time

from aiohttp import web
from loguru import logger
from .pty_compat import PtyProcess

_start_time = time.monotonic()

from .config import AppConfig, read_config, write_config, list_canvases, read_canvas, write_canvas, delete_canvas  # noqa: E402
from .discovery import discover_hubs  # noqa: E402
from .startup import run_startup  # noqa: E402
from .util_container import ensure_util_container, discover_profiles  # noqa: E402
from .sessions import SessionManager  # noqa: E402
from .cards import ServiceCardRegistry, ClaudeUsageCard, TerminalCard, CardRegistry, CanvasClaudeCard  # noqa: E402
from .event_bus import EventBus  # noqa: E402
from .ansi_strip import strip_ansi  # noqa: E402

STATIC_DIR = pathlib.Path(__file__).parent / "static"


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


async def canvas_put_handler(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    logger.info("Canvas '{}' save requested by {}", name, request.remote)
    app_config: AppConfig = request.app["app_config"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="Invalid JSON")
    ok = write_canvas(app_config, name, body)
    if not ok:
        raise web.HTTPBadRequest(text=f"Invalid canvas name '{name}'")
    return web.json_response({"status": "ok", "name": name})


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


_DOCKER_CMD = "docker.exe" if sys.platform == "win32" else "docker"


# ── VM Manager API ───────────────────────────────────────────────────────────


async def vm_discover_handler(request: web.Request) -> web.Response:
    """Discover all Docker containers (running + stopped) with status."""
    # In test mode, return injected mock data if available
    test_containers = request.app.get("_test_vm_containers")
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
        logger.warning("vm_discover: docker ps failed: {}", err)
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


async def vm_favorites_get_handler(request: web.Request) -> web.Response:
    """Read the VM Manager favorites list from config."""
    app_config: AppConfig = request.app["app_config"]
    config = read_config(app_config)
    vm_config = config.get("vm_manager", {})
    favorites = vm_config.get("favorites", [])
    return web.json_response(favorites)


async def vm_favorites_put_handler(request: web.Request) -> web.Response:
    """Write the VM Manager favorites list to config."""
    app_config: AppConfig = request.app["app_config"]
    body = await request.json()
    favorites = body if isinstance(body, list) else body.get("favorites", [])
    config = read_config(app_config)
    if "vm_manager" not in config:
        config["vm_manager"] = {}
    config["vm_manager"]["favorites"] = favorites
    write_config(app_config, config)
    return web.json_response(favorites)


async def vm_start_handler(request: web.Request) -> web.Response:
    """Start a stopped Docker container by name."""
    name = request.match_info["name"]

    # In test mode, flip mock container state instead of calling Docker
    test_containers = request.app.get("_test_vm_containers")
    if test_containers is not None:
        for c in test_containers:
            if c["name"] == name:
                c["state"] = "online"
                logger.info("vm_start (test): flipped '{}' to online", name)
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
        logger.warning("vm_start: failed to start container '{}': {}", name, err)
        return web.json_response({"error": err}, status=500)

    logger.info("vm_start: started container '{}'", name)
    return web.json_response({"name": name, "state": "online"})


async def vm_stop_handler(request: web.Request) -> web.Response:
    """Stop a running Docker container by name."""
    name = request.match_info["name"]

    # In test mode, flip mock container state instead of calling Docker
    test_containers = request.app.get("_test_vm_containers")
    if test_containers is not None:
        for c in test_containers:
            if c["name"] == name:
                c["state"] = "offline"
                logger.info("vm_stop (test): flipped '{}' to offline", name)
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
        logger.warning("vm_stop: failed to stop container '{}': {}", name, err)
        return web.json_response({"error": err}, status=500)

    logger.info("vm_stop: stopped container '{}'", name)
    return web.json_response({"name": name, "state": "offline"})


async def vm_favorites_actions_put_handler(request: web.Request) -> web.Response:
    """Update actions for a specific favorite container by name."""
    name = request.match_info["name"]
    app_config: AppConfig = request.app["app_config"]
    cfg = read_config(app_config)
    vm_config = cfg.get("vm_manager", {})
    favorites = vm_config.get("favorites", [])

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
    if "vm_manager" not in cfg:
        cfg["vm_manager"] = {}
    cfg["vm_manager"]["favorites"] = favorites
    write_config(app_config, cfg)

    return web.json_response(actions)


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
    if not cmd:
        raise web.HTTPBadRequest(text="Missing 'cmd' query parameter")

    mgr: SessionManager = request.app["session_manager"]
    card_registry: CardRegistry = request.app["card_registry"]

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    try:
        card = TerminalCard(
            session_manager=mgr,
            cmd=cmd,
            hub=hub or None,
            container=container or None,
        )
        await card.start()
        card_registry.register(card)
    except Exception:
        logger.exception("Failed to create session for cmd={!r}", cmd)
        await ws.send_str(json.dumps({"error": "Failed to spawn terminal"}))
        await ws.close()
        return ws

    session = card.session
    await ws.send_str(json.dumps({"session_id": session.session_id, "tmux": session.tmux_backed}))
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


async def test_vm_containers_put(request: web.Request) -> web.Response:
    """PUT /api/test/vm-containers — inject fake container list for E2E tests."""
    data = await request.json()
    containers = data if isinstance(data, list) else data.get("containers", [])
    request.app["_test_vm_containers"] = containers
    return web.json_response(containers)


async def test_vm_containers_get(request: web.Request) -> web.Response:
    """GET /api/test/vm-containers — read back fake container list."""
    containers = request.app.get("_test_vm_containers", [])
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
    priority_profile = config.get("priority_profile")

    # Merge discovered profiles with any manually configured ones
    discovered = request.app.get("discovered_profiles", [])
    config_profiles = config.get("probe_profiles", [])
    probe_profiles = sorted(set(discovered + config_profiles))

    profiles = []
    for profile in probe_profiles:
        card = registry.get("claude-usage", profile)
        entry = {"profile": profile, "is_priority": profile == priority_profile}
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


async def priority_get_handler(request: web.Request) -> web.Response:
    """GET /api/profiles/priority — return the current priority profile."""
    app_config: AppConfig = request.app["app_config"]
    config = read_config(app_config)
    return web.json_response({"priority_profile": config.get("priority_profile")})


async def priority_put_handler(request: web.Request) -> web.Response:
    """PUT /api/profiles/priority — set the priority profile."""
    app_config: AppConfig = request.app["app_config"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="Invalid JSON")

    priority = body.get("priority_profile")
    config = read_config(app_config)
    if priority is not None:
        discovered = request.app.get("discovered_profiles", [])
        config_profiles = config.get("probe_profiles", [])
        all_profiles = set(discovered + config_profiles)
        if priority not in all_profiles:
            raise web.HTTPBadRequest(text=f"Profile '{priority}' not found in discovered or configured profiles")

    config["priority_profile"] = priority
    write_config(app_config, config)
    return web.json_response({"priority_profile": priority})


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


async def claude_terminal_create(request: web.Request) -> web.Response:
    """POST /api/claude/terminal/create — create a TerminalCard + PTY session."""
    cmd = request.query.get("cmd", "").strip()
    hub = request.query.get("hub", "")
    container = request.query.get("container", "").strip()
    if not cmd:
        raise web.HTTPBadRequest(text="Missing 'cmd' query parameter")

    # Interpolate ${priority_credential} with the current priority profile from
    # config. Mirrors the client-side substitution in static/index.html so
    # Canvas Claude (MCP) can pass the placeholder through unchanged. If no
    # priority profile is set, the placeholder is left as-is so callers can
    # detect the misconfiguration downstream.
    if "${priority_credential}" in cmd:
        app_config: AppConfig = request.app["app_config"]
        cfg = read_config(app_config)
        priority_profile = cfg.get("priority_profile")
        if priority_profile:
            cmd = cmd.replace("${priority_credential}", str(priority_profile))
            logger.info(
                "claude_terminal_create: interpolated ${{priority_credential}} -> {!r}",
                priority_profile,
            )
        else:
            logger.warning("claude_terminal_create: cmd contains ${{priority_credential}} but no priority_profile set")

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

    card = TerminalCard(
        session_manager=mgr,
        cmd=cmd,
        hub=hub or None,
        container=container or None,
        layout=layout,
    )
    try:
        await card.start()
        card_registry.register(card)
    except Exception:
        logger.exception("claude_terminal_create: failed for cmd={!r}", cmd)
        return web.json_response({"error": "Failed to spawn terminal"}, status=500)

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
    """POST /api/claude/terminal/{id}/send — write text to PTY."""
    card_id = request.match_info["id"]
    card_registry: CardRegistry = request.app["card_registry"]
    card = card_registry.get_terminal(card_id)
    if not card or not card.alive:
        raise web.HTTPNotFound(text="Terminal not found")

    text = await request.text()
    card.session.pty.write(text)
    # Touch last_client_time to prevent orphan reaping
    card.session.last_client_time = time.monotonic()

    return web.json_response({"status": "ok", "sent": len(text)})


async def claude_terminal_read(request: web.Request) -> web.Response:
    """GET /api/claude/terminal/{id}/read — return scrollback (optionally ANSI-stripped)."""
    card_id = request.match_info["id"]
    card_registry: CardRegistry = request.app["card_registry"]
    card = card_registry.get_terminal(card_id)
    if not card or not card.alive:
        raise web.HTTPNotFound(text="Terminal not found")

    # Touch last_client_time to prevent orphan reaping
    card.session.last_client_time = time.monotonic()

    data = card.session.scrollback.get_all()
    output = data.decode("utf-8", errors="replace")

    do_strip = request.query.get("strip_ansi", "").lower() in ("true", "1", "yes")
    if do_strip:
        output = strip_ansi(output)

    return web.json_response(
        {
            "output": output,
            "size": len(data),
            "total_written": card.session.scrollback.total_written,
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
    """DELETE /api/claude/terminal/{id} — stop card, clean up."""
    card_id = request.match_info["id"]
    card_registry: CardRegistry = request.app["card_registry"]

    card = card_registry.get_terminal(card_id)
    if not card:
        raise web.HTTPNotFound(text="Terminal not found")

    # Stop card (destroys PTY via SessionManager)
    await card.stop()
    card_registry.unregister(card_id)

    logger.info("claude_terminal_delete: removed {}", card_id)
    return web.json_response({"status": "ok"})


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

    # Fall back to the configured priority profile if none was specified.
    if not profile:
        app_cfg: AppConfig = request.app["app_config"]
        cfg = read_config(app_cfg)
        profile = cfg.get("priority_profile") or None

    if not profile:
        raise web.HTTPBadRequest(text="profile is required (no priority_profile configured)")

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
        card_registry.register(card)
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
    try:
        card_registry.unregister(card_id)
        await card.new_session()
        card_registry.register(card)
    except Exception:
        logger.exception("canvas_claude_new_session: failed for {}", card_id)
        # Re-register the card so it is not orphaned — use whatever id it
        # currently has (may be the old one if new_session() failed before
        # allocating a new PTY).
        try:
            card_registry.register(card)
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
            # The control channel is server→client only; ignore client messages.
            if msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
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


def create_app(app_config: AppConfig, test_mode: bool = False) -> web.Application:
    app = web.Application()
    app["app_config"] = app_config
    app["test_mode"] = test_mode
    app["discovered_profiles"] = []
    app["control_ws_clients"] = []

    # Static + API routes
    app.router.add_get("/", index_handler)
    app.router.add_get("/api/hubs", hubs_handler)
    app.router.add_get("/api/startup", startup_handler)
    app.router.add_get("/api/config", config_get_handler)
    app.router.add_put("/api/config", config_put_handler)
    app.router.add_get("/api/canvases", canvases_list_handler)
    app.router.add_get("/api/canvases/{name}", canvas_get_handler)
    app.router.add_put("/api/canvases/{name}", canvas_put_handler)
    app.router.add_delete("/api/canvases/{name}", canvas_delete_handler)
    app.router.add_get("/api/widgets/system-info", widget_system_info_handler)

    # VM Manager API
    app.router.add_get("/api/vms/discover", vm_discover_handler)
    app.router.add_get("/api/vms/favorites", vm_favorites_get_handler)
    app.router.add_put("/api/vms/favorites", vm_favorites_put_handler)
    app.router.add_post("/api/vms/{name}/start", vm_start_handler)
    app.router.add_post("/api/vms/{name}/stop", vm_stop_handler)
    app.router.add_put("/api/vms/favorites/{name}/actions", vm_favorites_actions_put_handler)

    app.router.add_get("/api/profiles", profiles_list_handler)
    app.router.add_get("/api/profiles/discover", profiles_discover_handler)
    app.router.add_get("/api/profiles/priority", priority_get_handler)
    app.router.add_put("/api/profiles/priority", priority_put_handler)
    app.router.add_post("/api/claude-usage", claude_usage_handler)
    app.router.add_post("/api/probe/claude-usage", probe_claude_usage_handler)

    # Claude terminal control API (production)
    app.router.add_post("/api/claude/terminal/create", claude_terminal_create)
    app.router.add_post("/api/claude/terminal/{id}/send", claude_terminal_send)
    app.router.add_get("/api/claude/terminal/{id}/read", claude_terminal_read)
    app.router.add_get("/api/claude/terminal/{id}/status", claude_terminal_status)
    app.router.add_delete("/api/claude/terminal/{id}", claude_terminal_delete)
    app.router.add_get("/api/claude/terminals", claude_terminals_list)

    # Canvas Claude Code API
    app.router.add_post("/api/canvas-claude/create", canvas_claude_create)
    app.router.add_post("/api/canvas-claude/{id}/new-session", canvas_claude_new_session)
    app.router.add_post("/api/canvas-claude/{id}/clear", canvas_claude_clear)

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
        app.router.add_put("/api/test/vm-containers", test_vm_containers_put)
        app.router.add_get("/api/test/vm-containers", test_vm_containers_get)

    # Lifecycle hooks
    async def on_startup(app: web.Application) -> None:
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
        app["card_registry"] = CardRegistry(bus=event_bus)

        # Wire EventBus → /ws/control broadcast
        async def _on_card_event(event_type: str, payload: dict) -> None:
            await _broadcast_card_event(app, event_type, payload)

        event_bus.subscribe("card:registered", _on_card_event)
        event_bus.subscribe("card:unregistered", _on_card_event)

        mgr.start_orphan_reaper()

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
