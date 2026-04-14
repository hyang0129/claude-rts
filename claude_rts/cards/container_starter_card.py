"""ContainerStarterCard: transient card that starts a container and probes exec-readiness."""

import asyncio
import sys

from loguru import logger

from .base import BaseCard

_DOCKER_CMD = "docker.exe" if sys.platform == "win32" else "docker"


class ContainerStarterCard(BaseCard):
    """Transient service card that starts a Docker container and probes exec-readiness.

    Lifecycle:
    1. Calls POST /api/vms/{name}/start (via the app reference)
    2. Probes exec-readiness: ``docker exec <name> true`` with exponential backoff
    3. On success: emits ``container:ready:{name}`` with result payload; self-closes
    4. On timeout/failure: emits ``container:failed:{name}`` with error; self-closes

    BlueprintCard subscribes to the events before spawning this card.
    """

    card_type: str = "container_starter"
    hidden: bool = True  # Not visible on canvas

    # Probe retry settings
    INITIAL_BACKOFF: float = 1.0
    MAX_BACKOFF: float = 10.0
    BACKOFF_FACTOR: float = 2.0
    DEFAULT_TIMEOUT: float = 120.0

    def __init__(
        self,
        container_name: str,
        app=None,
        card_id: str | None = None,
        timeout: float | None = None,
    ):
        super().__init__(card_id=card_id)
        self.container_name = container_name
        self._app = app
        self._timeout = timeout or self.DEFAULT_TIMEOUT
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the container and begin readiness probing."""
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the running task if still active."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _run(self) -> None:
        """Start the container, probe readiness, emit events, self-close."""
        name = self.container_name
        try:
            # Step 1: Start the container
            logger.info("ContainerStarterCard {}: starting container '{}'", self.id, name)
            await self._start_container(name)

            # Step 2: Probe exec-readiness with exponential backoff
            logger.info("ContainerStarterCard {}: probing exec-readiness for '{}'", self.id, name)
            await self._probe_readiness(name)

            # Success
            logger.info("ContainerStarterCard {}: container '{}' is ready", self.id, name)
            if self.bus is not None:
                await self.bus.emit(
                    f"container:ready:{name}",
                    {"container_name": name},
                )

        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            error_msg = f"Container '{name}' exec-readiness probe timed out after {self._timeout}s"
            logger.warning("ContainerStarterCard {}: {}", self.id, error_msg)
            if self.bus is not None:
                await self.bus.emit(
                    f"container:failed:{name}",
                    {"container_name": name, "error": error_msg},
                )
        except Exception as exc:
            error_msg = f"Container '{name}' start failed: {exc}"
            logger.exception("ContainerStarterCard {}: {}", self.id, error_msg)
            if self.bus is not None:
                await self.bus.emit(
                    f"container:failed:{name}",
                    {"container_name": name, "error": error_msg},
                )

        # Self-close: unregister from CardRegistry
        if self._app is not None and "card_registry" in self._app:
            card = self._app["card_registry"].unregister(self.id)
            if card:
                logger.debug("ContainerStarterCard {}: self-unregistered", self.id)

    async def _start_container(self, name: str) -> None:
        """Start the container via docker start."""
        # In test mode, check for mock containers
        if self._app is not None:
            test_containers = self._app.get("_test_vm_containers")
            if test_containers is not None:
                for c in test_containers:
                    if c["name"] == name:
                        c["state"] = "online"
                        logger.info("ContainerStarterCard: (test) flipped '{}' to online", name)
                        return
                raise RuntimeError(f"No such container: {name}")

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
            raise RuntimeError(f"docker start failed for '{name}': {err}")

    async def _probe_readiness(self, name: str) -> None:
        """Probe exec-readiness with exponential backoff until success or timeout."""
        # In test mode with mock containers, skip the real docker exec probe
        if self._app is not None:
            test_containers = self._app.get("_test_vm_containers")
            if test_containers is not None:
                # Mock mode: container is already "started", consider it ready
                return

        backoff = self.INITIAL_BACKOFF
        deadline = asyncio.get_event_loop().time() + self._timeout

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError()

            try:
                proc = await asyncio.wait_for(
                    asyncio.create_subprocess_exec(
                        _DOCKER_CMD,
                        "exec",
                        name,
                        "true",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    ),
                    timeout=min(backoff * 2, remaining),
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=min(backoff * 2, remaining),
                )
                if proc.returncode == 0:
                    return  # Ready!
            except asyncio.TimeoutError:
                if asyncio.get_event_loop().time() + backoff > deadline:
                    raise
            except Exception:
                pass  # Retry on any error

            await asyncio.sleep(min(backoff, remaining))
            backoff = min(backoff * self.BACKOFF_FACTOR, self.MAX_BACKOFF)
