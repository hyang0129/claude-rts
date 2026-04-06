"""Manage the supreme-claudemander utility container.

The utility container is a lightweight Linux container for background tasks,
monitoring, and status probing. It is NOT for coding or LLM calls.

It stays alive via `sleep infinity` and commands are executed via `docker exec`.
"""

import asyncio
import pathlib
import sys
import time

_DOCKER = "docker.exe" if sys.platform == "win32" else "docker"

from loguru import logger

from .config import read_config

DOCKERFILE = pathlib.Path(__file__).parent / "Dockerfile.util"
DEFAULT_CONTAINER_NAME = "supreme-claudemander-util"
DEFAULT_IMAGE_NAME = "supreme-claudemander-util:latest"


def _get_config() -> dict:
    """Get utility container config with defaults."""
    config = read_config()
    util = config.get("util_container", {})
    return {
        "name": util.get("name", DEFAULT_CONTAINER_NAME),
        "image": util.get("image", DEFAULT_IMAGE_NAME),
        "auto_start": util.get("auto_start", True),
        "auto_stop": util.get("auto_stop", False),
        "mounts": util.get("mounts", {}),
    }


async def _run(cmd: str, timeout: float = 30) -> tuple[int, str, str]:
    """Run a shell command and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", "timeout"
    return proc.returncode, stdout.decode(errors="replace").strip(), stderr.decode(errors="replace").strip()


async def is_util_running() -> bool:
    """Check if the utility container is running."""
    cfg = _get_config()
    rc, stdout, _ = await _run(
        f'{_DOCKER} ps --filter "name=^/{cfg["name"]}$" --format "{{{{.Status}}}}"'
    )
    return rc == 0 and "Up" in stdout


async def build_image() -> bool:
    """Build the utility container image if not already built."""
    cfg = _get_config()
    # Check if image exists
    rc, stdout, _ = await _run(f'{_DOCKER} images -q {cfg["image"]}')
    if rc == 0 and stdout.strip():
        logger.debug("Utility image {} already exists", cfg["image"])
        return True

    logger.info("Building utility container image {}...", cfg["image"])
    rc, stdout, stderr = await _run(
        f'{_DOCKER} build -t {cfg["image"]} -f "{DOCKERFILE}" "{DOCKERFILE.parent}"',
        timeout=300,
    )
    if rc != 0:
        logger.error("Failed to build utility image: {}", stderr)
        return False
    logger.info("Utility image built successfully")
    return True


async def start_container() -> bool:
    """Start the utility container. Builds image if needed."""
    cfg = _get_config()

    if await is_util_running():
        logger.debug("Utility container '{}' already running", cfg["name"])
        return True

    # Build image if needed
    if not await build_image():
        return False

    # Build mount args
    mount_args = ""
    for host_path, container_path in cfg["mounts"].items():
        # Expand ~ in host path
        expanded = host_path.replace("~", str(pathlib.Path.home()))
        if pathlib.Path(expanded).exists():
            mount_args += f' -v "{expanded}:{container_path}"'
            logger.info("Mounting {} -> {}", expanded, container_path)
        else:
            logger.warning("Mount source does not exist, skipping: {}", expanded)

    # Remove old stopped container if exists
    await _run(f'{_DOCKER} rm -f {cfg["name"]}')

    # Start container
    cmd = f'{_DOCKER} run -d --name {cfg["name"]}{mount_args} {cfg["image"]}'
    logger.info("Starting utility container: {}", cmd)
    rc, stdout, stderr = await _run(cmd, timeout=60)
    if rc != 0:
        logger.error("Failed to start utility container: {}", stderr)
        return False

    logger.info("Utility container '{}' started (id: {})", cfg["name"], stdout[:12])
    return True


async def stop_container() -> bool:
    """Stop the utility container."""
    cfg = _get_config()
    rc, _, stderr = await _run(f'{_DOCKER} stop {cfg["name"]}')
    if rc != 0:
        logger.warning("Failed to stop utility container: {}", stderr)
        return False
    await _run(f'{_DOCKER} rm {cfg["name"]}')
    logger.info("Utility container '{}' stopped", cfg["name"])
    return True


async def exec_in_util(cmd: str, timeout: float = 60) -> tuple[int, str]:
    """Execute a command in the utility container.

    Returns (returncode, stdout). Raises RuntimeError if container not running.
    """
    cfg = _get_config()

    if not await is_util_running():
        raise RuntimeError(f"Utility container '{cfg['name']}' is not running")

    full_cmd = f'{_DOCKER} exec {cfg["name"]} {cmd}'
    logger.debug("exec_in_util: {}", cmd)
    rc, stdout, stderr = await _run(full_cmd, timeout=timeout)
    if rc != 0:
        logger.warning("exec_in_util failed (rc={}): {}", rc, stderr)
    return rc, stdout


async def exec_in_util_pty(cmd: str, timeout: float = 60) -> tuple[int, str]:
    """Execute a command in the utility container using a real PTY.

    Required for commands that need a TTY (e.g., claude-usage-plz which uses pexpect).
    Uses pywinpty to provide a ConPTY, same as terminal WebSocket connections.
    """
    from .pty_compat import PtyProcess

    cfg = _get_config()

    if not await is_util_running():
        raise RuntimeError(f"Utility container '{cfg['name']}' is not running")

    full_cmd = f'{_DOCKER} exec -it {cfg["name"]} {cmd}'
    logger.debug("exec_in_util_pty: {}", cmd)

    loop = asyncio.get_event_loop()

    def _run_pty():
        try:
            pty = PtyProcess.spawn(full_cmd, dimensions=(24, 120))
        except Exception as exc:
            logger.error("Failed to spawn PTY for exec_in_util_pty: {}", exc)
            return -1, ""

        output = []
        deadline = time.monotonic() + timeout
        try:
            while pty.isalive() and time.monotonic() < deadline:
                try:
                    data = pty.read()
                    if data:
                        output.append(data.decode("utf-8", errors="replace"))
                        # Early exit: if we see a complete JSON object, we're done
                        combined = "".join(output)
                        if '{\n' in combined and combined.rstrip().endswith('}'):
                            logger.debug("exec_in_util_pty: detected complete JSON, exiting early")
                            break
                except EOFError:
                    break
                except Exception:
                    break
        finally:
            try:
                pty.terminate(force=True)
            except Exception:
                pass

        if time.monotonic() >= deadline:
            logger.warning("exec_in_util_pty timed out after {}s", timeout)
            return -1, "".join(output)

        return 0, "".join(output)

    rc, stdout = await loop.run_in_executor(None, _run_pty)
    if rc != 0:
        logger.warning("exec_in_util_pty failed (rc={})", rc)
    return rc, stdout


async def ensure_util_container() -> bool:
    """Ensure the utility container is running. Start if needed.

    Called during server startup if auto_start is enabled.
    """
    cfg = _get_config()
    if not cfg["auto_start"]:
        logger.info("Utility container auto_start disabled, skipping")
        return False
    return await start_container()

