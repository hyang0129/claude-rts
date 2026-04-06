"""Session persistence: PTY sessions that survive WebSocket disconnects.

Each Session owns a PTY process and a scrollback ring buffer. The PTY read
loop runs continuously, feeding the scrollback regardless of whether any
WebSocket client is attached. On reconnect, the client receives the
scrollback contents before live data.
"""

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import sys

from aiohttp import web
from loguru import logger
from .pty_compat import PtyProcess

_DOCKER = "docker.exe" if sys.platform == "win32" else "docker"


class ScrollbackBuffer:
    """Fixed-size ring buffer for raw PTY output bytes."""

    def __init__(self, capacity: int = 65536):
        self._buf = bytearray(capacity)
        self._capacity = capacity
        self._write_pos = 0
        self._total_written = 0

    @property
    def total_written(self) -> int:
        return self._total_written

    @property
    def size(self) -> int:
        return min(self._total_written, self._capacity)

    def append(self, data: bytes) -> None:
        """Append data to the ring buffer."""
        n = len(data)
        if n == 0:
            return
        if n >= self._capacity:
            # Data larger than buffer — keep only the tail
            data = data[-self._capacity :]
            n = self._capacity
            self._buf[:] = data
            self._write_pos = 0
            self._total_written += len(data)
            return

        end = self._write_pos + n
        if end <= self._capacity:
            self._buf[self._write_pos : end] = data
        else:
            first = self._capacity - self._write_pos
            self._buf[self._write_pos :] = data[:first]
            self._buf[: n - first] = data[first:]
        self._write_pos = end % self._capacity
        self._total_written += n

    def get_all(self) -> bytes:
        """Return all buffered data in order."""
        if self._total_written == 0:
            return b""
        if self._total_written < self._capacity:
            return bytes(self._buf[: self._write_pos])
        # Buffer is full or has wrapped
        if self._write_pos == 0:
            return bytes(self._buf[: self._capacity])
        return bytes(self._buf[self._write_pos :] + self._buf[: self._write_pos])


@dataclass
class Session:
    """A persistent PTY session."""

    session_id: str
    cmd: str
    hub: Optional[str]
    container: Optional[str]
    pty: PtyProcess
    scrollback: ScrollbackBuffer
    created_at: float = field(default_factory=time.monotonic)
    last_client_time: float = field(default_factory=time.monotonic)
    clients: set = field(default_factory=set)
    read_task: Optional[asyncio.Task] = None
    alive: bool = True
    tmux_backed: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# Docker container names: alphanumeric, underscores, hyphens, dots
_CONTAINER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def _valid_container_name(name: str) -> bool:
    """Return True if name is a safe Docker container name."""
    return bool(name) and len(name) <= 128 and bool(_CONTAINER_NAME_RE.match(name))


class SessionManager:
    """Registry of persistent PTY sessions."""

    def __init__(self, orphan_timeout: float = 300, scrollback_size: int = 65536, tmux_enabled: bool = True):
        self._sessions: dict[str, Session] = {}
        self.orphan_timeout = orphan_timeout
        self.scrollback_size = scrollback_size
        self.tmux_enabled = tmux_enabled
        self._reaper_task: Optional[asyncio.Task] = None
        self._tmux_cache: dict[str, bool] = {}

    def create_session(
        self,
        cmd: str,
        hub: str | None = None,
        container: str | None = None,
        dimensions: tuple[int, int] = (24, 80),
    ) -> Session:
        """Spawn a PTY and register a new session."""
        session_id = "rts-" + uuid.uuid4().hex[:8]
        logger.info("Creating session {} for cmd={!r} hub={} container={}", session_id, cmd, hub, container)

        if container and not _valid_container_name(container):
            logger.warning("Invalid container name rejected: {!r}", container)
            container = None

        use_tmux = bool(container and self.tmux_enabled and self._tmux_cache.get(container))
        if use_tmux:
            spawn_cmd = f"{_DOCKER} exec -it {container} tmux new-session -As {session_id}"
            logger.info("Using tmux persistence: {}", spawn_cmd)
        else:
            spawn_cmd = cmd

        pty = PtyProcess.spawn(spawn_cmd, dimensions=dimensions)

        session = Session(
            session_id=session_id,
            cmd=cmd,
            hub=hub,
            container=container,
            pty=pty,
            scrollback=ScrollbackBuffer(self.scrollback_size),
            tmux_backed=use_tmux,
        )
        session.read_task = asyncio.create_task(self._pty_read_loop(session))
        self._sessions[session_id] = session

        logger.info("Session {} created (PTY spawned)", session_id)
        return session

    def get_session(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    async def attach(self, session_id: str, ws: web.WebSocketResponse) -> bytes | None:
        """Attach a WebSocket client to a session. Returns scrollback for replay."""
        session = self._sessions.get(session_id)
        if not session or not session.alive:
            return None

        async with session._lock:
            scrollback = session.scrollback.get_all()
            session.clients.add(ws)
            session.last_client_time = time.monotonic()

        logger.info(
            "Session {}: client attached ({} total), scrollback={} bytes",
            session_id,
            len(session.clients),
            len(scrollback),
        )
        return scrollback

    def detach(self, session_id: str, ws: web.WebSocketResponse) -> None:
        """Detach a WebSocket client from a session."""
        session = self._sessions.get(session_id)
        if not session:
            return
        session.clients.discard(ws)
        session.last_client_time = time.monotonic()
        logger.info("Session {}: client detached ({} remaining)", session_id, len(session.clients))

    def destroy_session(self, session_id: str, kill_tmux: bool = False) -> None:
        """Kill a session's PTY and remove it from the registry.

        If kill_tmux is True and the session has a container, also kill the
        tmux session inside the container so it doesn't persist.
        """
        session = self._sessions.pop(session_id, None)
        if not session:
            return
        session.alive = False
        if session.read_task:
            session.read_task.cancel()
        try:
            session.pty.terminate(force=True)
        except Exception:
            pass
        if kill_tmux and session.container:
            task = asyncio.create_task(self._kill_tmux_session(session))
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        logger.info("Session {} destroyed (kill_tmux={})", session_id, kill_tmux)

    async def _kill_tmux_session(self, session: Session) -> None:
        """Kill a tmux session inside its container."""
        if not session.container:
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                _DOCKER,
                "exec",
                session.container,
                "tmux",
                "kill-session",
                "-t",
                session.session_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            logger.info("Killed tmux session {} in container {}", session.session_id, session.container)
        except Exception:
            logger.warning("Failed to kill tmux session {} in {}", session.session_id, session.container)

    def list_sessions(self) -> list[dict]:
        """Return metadata for all active sessions."""
        now = time.monotonic()
        return [
            {
                "session_id": s.session_id,
                "cmd": s.cmd,
                "hub": s.hub,
                "container": s.container,
                "alive": s.alive,
                "client_count": len(s.clients),
                "scrollback_size": s.scrollback.size,
                "age_seconds": int(now - s.created_at),
                "idle_seconds": int(now - s.last_client_time),
            }
            for s in self._sessions.values()
        ]

    async def _pty_read_loop(self, session: Session) -> None:
        """Continuously read PTY output into scrollback and fan out to clients."""
        loop = asyncio.get_event_loop()
        logger.debug("Session {}: read loop started", session.session_id)
        try:
            while session.pty.isalive() and session.alive:
                try:
                    data = await loop.run_in_executor(None, session.pty.read)
                    if not data:
                        continue
                    raw = data

                    async with session._lock:
                        session.scrollback.append(raw)
                        dead = []
                        for ws in session.clients:
                            try:
                                await ws.send_bytes(raw)
                            except Exception:
                                dead.append(ws)
                        for ws in dead:
                            session.clients.discard(ws)
                except EOFError:
                    logger.info("Session {}: PTY EOF", session.session_id)
                    break
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.exception("Session {}: read error", session.session_id)
                    break
        finally:
            session.alive = False
            logger.info("Session {}: read loop ended", session.session_id)

    def start_orphan_reaper(self) -> None:
        """Start background task to clean up orphaned sessions."""
        self._reaper_task = asyncio.create_task(self._orphan_reaper())

    async def _orphan_reaper(self) -> None:
        """Periodically kill sessions with no clients past the timeout."""
        while True:
            await asyncio.sleep(30)
            now = time.monotonic()
            to_kill = [
                sid
                for sid, s in self._sessions.items()
                if len(s.clients) == 0 and (now - s.last_client_time) > self.orphan_timeout
            ]
            for sid in to_kill:
                logger.info(
                    "Orphan reaper: detaching session {} (no clients for {}s)",
                    sid,
                    int(now - self._sessions[sid].last_client_time),
                )
                self.destroy_session(sid, kill_tmux=False)

    def stop_all(self) -> None:
        """Detach all local PTY handles and stop the reaper.

        tmux sessions inside containers are NOT killed — they persist for
        recovery on the next server start.
        """
        if self._reaper_task:
            self._reaper_task.cancel()
        for sid in list(self._sessions.keys()):
            self.destroy_session(sid, kill_tmux=False)
        logger.info("SessionManager: all sessions detached (tmux sessions preserved)")

    async def probe_tmux(self, container: str) -> bool:
        """Check if tmux is available in a container and cache the result."""
        if container in self._tmux_cache:
            return self._tmux_cache[container]
        try:
            proc = await asyncio.create_subprocess_exec(
                _DOCKER,
                "exec",
                container,
                "tmux",
                "-V",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            available = proc.returncode == 0
        except Exception:
            available = False
        self._tmux_cache[container] = available
        if available:
            logger.info("tmux available in container {}", container)
        else:
            logger.debug("tmux not available in container {}", container)
        return available

    async def recover_tmux_sessions(self, hubs: list[dict]) -> int:
        """Discover and re-attach to existing tmux sessions in containers."""
        recovered = 0
        for hub in hubs:
            container = hub["container"]
            hub_name = hub["hub"]
            proc = await asyncio.create_subprocess_exec(
                _DOCKER,
                "exec",
                container,
                "tmux",
                "list-sessions",
                "-F",
                "#{session_name}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                continue

            self._tmux_cache[container] = True

            for line in stdout.decode().strip().splitlines():
                session_name = line.strip()
                if not session_name.startswith("rts-"):
                    continue
                if session_name in self._sessions:
                    continue

                tmux_cmd = f"{_DOCKER} exec -it {container} tmux attach-session -t {session_name}"
                try:
                    pty = PtyProcess.spawn(tmux_cmd, dimensions=(24, 80))
                except Exception:
                    logger.warning("Failed to reattach to tmux session {} in {}", session_name, container)
                    continue

                session = Session(
                    session_id=session_name,
                    cmd=tmux_cmd,
                    hub=hub_name,
                    container=container,
                    pty=pty,
                    scrollback=ScrollbackBuffer(self.scrollback_size),
                    tmux_backed=True,
                )

                # Seed scrollback from tmux history
                try:
                    cap_proc = await asyncio.create_subprocess_exec(
                        _DOCKER,
                        "exec",
                        container,
                        "tmux",
                        "capture-pane",
                        "-t",
                        session_name,
                        "-p",
                        "-S",
                        "-500",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    cap_stdout, _ = await cap_proc.communicate()
                    if cap_proc.returncode == 0 and cap_stdout:
                        session.scrollback.append(cap_stdout)
                except Exception:
                    pass

                session.read_task = asyncio.create_task(self._pty_read_loop(session))
                self._sessions[session_name] = session
                recovered += 1
                logger.info("Recovered tmux session {} from container {}", session_name, container)

        return recovered
