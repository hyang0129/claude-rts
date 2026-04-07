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
from .util_container import ensure_util_container  # noqa: E402
from .sessions import SessionManager  # noqa: E402
from .cards import ServiceCardRegistry, ClaudeUsageCard  # noqa: E402

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


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    hub_name = request.match_info["hub"]
    logger.info("WebSocket connection request for hub '{}'", hub_name)

    # Look up the container name for this hub
    hubs = await discover_hubs()
    hub = next((h for h in hubs if h["hub"] == hub_name), None)
    if hub is None:
        logger.warning("Hub '{}' not found in running containers", hub_name)
        raise web.HTTPNotFound(text=f"Hub '{hub_name}' not found")

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    logger.info("WebSocket established: {} -> container '{}'", hub_name, hub["container"])

    # Spawn docker exec via ConPTY for full terminal support
    _docker = "docker.exe" if sys.platform == "win32" else "docker"
    cmd = f"{_docker} exec -it -u vscode -w /workspaces/{hub_name} {hub['container']} bash -l"
    logger.info("Spawning PTY process: {}", cmd)

    try:
        pty = PtyProcess.spawn(cmd, dimensions=(24, 80))
    except Exception:
        logger.exception("Failed to spawn PTY for hub '{}'", hub_name)
        await ws.close(code=1011, message=b"Failed to spawn terminal")
        return ws

    logger.info("PTY spawned successfully for hub '{}' (pid-like handle active)", hub_name)

    async def pty_read_loop():
        """Read from PTY and forward to WebSocket."""
        loop = asyncio.get_event_loop()
        logger.debug("Starting PTY read loop for '{}'", hub_name)
        try:
            while pty.isalive():
                try:
                    data = await loop.run_in_executor(None, pty.read)
                    if data:
                        await ws.send_bytes(data)
                except EOFError:
                    logger.info("PTY EOF for hub '{}'", hub_name)
                    break
                except Exception:
                    logger.exception("PTY read error for hub '{}'", hub_name)
                    break
        finally:
            logger.info("PTY read loop ended for hub '{}'", hub_name)
            if not ws.closed:
                await ws.close()

    read_task = asyncio.create_task(pty_read_loop())

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.BINARY:
                # Terminal input from browser
                text = msg.data.decode("utf-8", errors="replace")
                pty.write(text)
            elif msg.type == web.WSMsgType.TEXT:
                # Control messages
                try:
                    control = json.loads(msg.data)
                    if control.get("type") == "resize":
                        cols = control.get("cols", 80)
                        rows = control.get("rows", 24)
                        logger.info("Resize hub '{}': {}x{}", hub_name, cols, rows)
                        pty.setwinsize(rows, cols)
                except json.JSONDecodeError:
                    logger.warning("Invalid JSON control message from hub '{}'", hub_name)
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                logger.info("WebSocket {} for hub '{}'", msg.type.name, hub_name)
                break
    except Exception:
        logger.exception("WebSocket handler error for hub '{}'", hub_name)
    finally:
        logger.info("Cleaning up hub '{}' session", hub_name)
        read_task.cancel()
        try:
            pty.terminate(force=True)
            logger.info("PTY terminated for hub '{}'", hub_name)
        except Exception:
            logger.warning("PTY terminate failed for hub '{}' (may already be dead)", hub_name)

    return ws


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
    """Create a new persistent session and attach via WebSocket."""
    cmd = request.query.get("cmd", "").strip()
    hub = request.query.get("hub", "")
    container = request.query.get("container", "").strip()
    if not cmd:
        raise web.HTTPBadRequest(text="Missing 'cmd' query parameter")

    mgr: SessionManager = request.app["session_manager"]

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    try:
        session = mgr.create_session(cmd, hub=hub or None, container=container or None)
    except Exception:
        logger.exception("Failed to create session for cmd={!r}", cmd)
        await ws.send_str(json.dumps({"error": "Failed to spawn terminal"}))
        await ws.close()
        return ws

    await ws.send_str(json.dumps({"session_id": session.session_id, "tmux": session.tmux_backed}))
    await mgr.attach(session.session_id, ws)

    # Send resize if client sends it as first message
    await _session_ws_input_loop(ws, session, mgr)
    return ws


async def session_attach_handler(request: web.Request) -> web.WebSocketResponse:
    """Attach to an existing persistent session via WebSocket."""
    session_id = request.match_info["session_id"]
    mgr: SessionManager = request.app["session_manager"]

    ws = web.WebSocketResponse()
    await ws.prepare(request)

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


def create_app(app_config: AppConfig, test_mode: bool = False) -> web.Application:
    app = web.Application()
    app["app_config"] = app_config
    app["test_mode"] = test_mode

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
    app.router.add_post("/api/claude-usage", claude_usage_handler)
    app.router.add_post("/api/probe/claude-usage", probe_claude_usage_handler)

    # Session routes (must be before /ws/{hub} catch-all)
    app.router.add_get("/api/sessions", sessions_list_handler)
    app.router.add_get("/ws/session/new", session_new_handler)
    app.router.add_get("/ws/session/{session_id}", session_attach_handler)
    app.router.add_get("/ws/exec", exec_websocket_handler)

    # Test puppeting API (only in test mode)
    if test_mode:
        app.router.add_post("/api/test/session/create", test_session_create)
        app.router.add_post("/api/test/session/{id}/send", test_session_send)
        app.router.add_get("/api/test/session/{id}/read", test_session_read)
        app.router.add_get("/api/test/session/{id}/status", test_session_status)
        app.router.add_delete("/api/test/session/{id}", test_session_delete)
        app.router.add_get("/api/test/sessions", test_sessions_list)

    # Legacy hub WebSocket (catch-all, must be last)
    app.router.add_get("/ws/{hub}", websocket_handler)

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
        registry = ServiceCardRegistry(session_manager=mgr)
        registry.register_type("claude-usage", ClaudeUsageCard)
        app["service_card_registry"] = registry
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

        # Start claude-usage probes after util container is ready
        probe_interval = config.get("probe_interval", 1800)
        for profile in config.get("probe_profiles", []):

            def _log_usage(result, _p=profile):
                logger.info(
                    "claude-usage [{}]: 5hr={}% burn={}/hr resets={}",
                    _p,
                    result.get("five_hour_pct"),
                    result.get("burn_rate"),
                    result.get("five_hour_resets"),
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
        if "service_card_registry" in app:
            await app["service_card_registry"].stop_all()
        if "session_manager" in app:
            app["session_manager"].stop_all()

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    logger.info("Application routes registered")
    return app
