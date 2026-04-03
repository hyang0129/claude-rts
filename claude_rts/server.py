"""aiohttp server: static files, hub discovery API, WebSocket-to-docker bridge via ConPTY."""

import asyncio
import json
import pathlib

from aiohttp import web
from loguru import logger
from winpty import PtyProcess

from .config import read_config, write_config, list_canvases, read_canvas, write_canvas
from .discovery import discover_hubs

STATIC_DIR = pathlib.Path(__file__).parent / "static"


async def index_handler(request: web.Request) -> web.FileResponse:
    logger.debug("Serving index.html to {}", request.remote)
    return web.FileResponse(STATIC_DIR / "index.html")


async def hubs_handler(request: web.Request) -> web.Response:
    logger.info("Hub discovery requested by {}", request.remote)
    hubs = await discover_hubs()
    logger.info("Discovered {} hub(s): {}", len(hubs), [h["hub"] for h in hubs])
    return web.json_response(hubs)


async def config_get_handler(request: web.Request) -> web.Response:
    logger.debug("Config read requested by {}", request.remote)
    data = read_config()
    return web.json_response(data)


async def config_put_handler(request: web.Request) -> web.Response:
    logger.info("Config update requested by {}", request.remote)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="Invalid JSON")
    saved = write_config(body)
    return web.json_response(saved)


async def canvases_list_handler(request: web.Request) -> web.Response:
    logger.debug("Canvas list requested by {}", request.remote)
    names = list_canvases()
    return web.json_response(names)


async def canvas_get_handler(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    logger.debug("Canvas '{}' read requested by {}", name, request.remote)
    data = read_canvas(name)
    if data is None:
        raise web.HTTPNotFound(text=f"Canvas '{name}' not found")
    return web.json_response(data)


async def canvas_put_handler(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    logger.info("Canvas '{}' save requested by {}", name, request.remote)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="Invalid JSON")
    ok = write_canvas(name, body)
    if not ok:
        raise web.HTTPBadRequest(text=f"Invalid canvas name '{name}'")
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
    cmd = f'docker.exe exec -it -u vscode {hub["container"]} bash -l'
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
                        await ws.send_bytes(data.encode("utf-8", errors="replace"))
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


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index_handler)
    app.router.add_get("/api/hubs", hubs_handler)
    app.router.add_get("/api/config", config_get_handler)
    app.router.add_put("/api/config", config_put_handler)
    app.router.add_get("/api/canvases", canvases_list_handler)
    app.router.add_get("/api/canvases/{name}", canvas_get_handler)
    app.router.add_put("/api/canvases/{name}", canvas_put_handler)
    app.router.add_get("/ws/{hub}", websocket_handler)
    logger.info("Application routes registered")
    return app
