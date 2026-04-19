"""Manage the supreme-claudemander utility container.

The utility container is a lightweight Linux container for background tasks,
monitoring, and status probing. It is NOT for coding or LLM calls.

It stays alive via `sleep infinity` and commands are executed via `docker exec`.
"""

import asyncio
import json
import pathlib
import time

_DOCKER = "docker"

from loguru import logger  # noqa: E402

from .config import AppConfig, read_config  # noqa: E402

DOCKERFILE = pathlib.Path(__file__).parent / "Dockerfile.util"
MCP_SERVER_PY = pathlib.Path(__file__).parent / "mcp_server.py"
CONTAINER_MCP_PATH = "/home/util/mcp_server.py"
DEFAULT_CONTAINER_NAME = "supreme-claudemander-util"
DEFAULT_IMAGE_NAME = "supreme-claudemander-util:latest"
GHCR_IMAGE = "ghcr.io/hyang0129/supreme-claudemander-util:latest"


def _get_config(app_config: AppConfig) -> dict:
    """Get utility container config with defaults."""
    config = read_config(app_config)
    util = config.get("util_container", {})
    return {
        "name": util.get("name", DEFAULT_CONTAINER_NAME),
        "image": util.get("image", DEFAULT_IMAGE_NAME),
        "auto_start": util.get("auto_start", True),
        "auto_stop": util.get("auto_stop", False),
        "mounts": util.get("mounts", {}),
        "volumes": util.get("volumes", {}),
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


async def is_util_running(app_config: AppConfig) -> bool:
    """Check if the utility container is running."""
    cfg = _get_config(app_config)
    rc, stdout, _ = await _run(f'{_DOCKER} ps --filter "name=^/{cfg["name"]}$" --format "{{{{.Status}}}}"')
    return rc == 0 and "Up" in stdout


async def build_image(app_config: AppConfig) -> bool:
    """Build the utility container image if not already built.

    Tries, in order:
    1. Use existing local image (instant).
    2. Pull prebuilt image from ghcr.io and tag it locally.
    3. Fall back to a local ``docker build`` (slow, but works offline).
    """
    cfg = _get_config(app_config)
    # Check if image exists locally
    rc, stdout, _ = await _run(f"{_DOCKER} images -q {cfg['image']}")
    if rc == 0 and stdout.strip():
        logger.debug("Utility image {} already exists", cfg["image"])
        return True

    # Try pulling the prebuilt image from GHCR
    logger.info("Pulling prebuilt utility image from {}...", GHCR_IMAGE)
    rc, _, stderr = await _run(f"{_DOCKER} pull {GHCR_IMAGE}", timeout=120)
    if rc == 0:
        # Tag as the local image name so subsequent checks find it
        tag_rc, _, tag_err = await _run(f"{_DOCKER} tag {GHCR_IMAGE} {cfg['image']}")
        if tag_rc == 0:
            logger.info("Pulled and tagged prebuilt utility image as {}", cfg["image"])
            return True
        logger.warning("GHCR pull succeeded but tag failed ({}), falling back to local build", tag_err)
    else:
        logger.warning("GHCR pull failed ({}), falling back to local build", stderr)

    # Fall back to local build
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


async def start_container(app_config: AppConfig) -> bool:
    """Start the utility container. Builds image if needed."""
    cfg = _get_config(app_config)

    if await is_util_running(app_config):
        logger.debug("Utility container '{}' already running", cfg["name"])
        return True

    # Build image if needed
    if not await build_image(app_config):
        return False

    # mcp_server.py is synced into the container via `docker cp` in
    # CanvasClaudeCard._sync_mcp_server on each card start — not bind-mounted.
    # A bind mount on a single file caused a WSL/Docker Desktop quirk where
    # docker cp against the mount point turned it into a directory.
    mount_args = ""

    for host_path, container_path in cfg["mounts"].items():
        # Expand ~ in host path and normalize to forward slashes for Docker
        expanded_path = pathlib.Path(host_path.replace("~", str(pathlib.Path.home())))
        if expanded_path.exists():
            # Use POSIX-style path (forward slashes) — Docker rejects mixed-slash paths
            mount_src = expanded_path.as_posix()
            mount_args += f' -v "{mount_src}:{container_path}"'
            logger.info("Mounting {} -> {}", mount_src, container_path)
        else:
            logger.warning("Mount source does not exist, skipping: {}", expanded_path)

    for vol_name, container_path in cfg["volumes"].items():
        mount_args += f" --mount type=volume,source={vol_name},target={container_path}"
        logger.info("Mounting volume {} -> {}", vol_name, container_path)

    # Remove old stopped container if exists
    await _run(f"{_DOCKER} rm -f {cfg['name']}")

    # Start container
    cmd = f"{_DOCKER} run -d --name {cfg['name']}{mount_args} {cfg['image']}"
    logger.info("Starting utility container: {}", cmd)
    rc, stdout, stderr = await _run(cmd, timeout=60)
    if rc != 0:
        logger.error("Failed to start utility container: {}", stderr)
        return False

    logger.info("Utility container '{}' started (id: {})", cfg["name"], stdout[:12])
    return True


async def stop_container(app_config: AppConfig) -> bool:
    """Stop the utility container."""
    cfg = _get_config(app_config)
    rc, _, stderr = await _run(f"{_DOCKER} stop {cfg['name']}")
    if rc != 0:
        logger.warning("Failed to stop utility container: {}", stderr)
        return False
    await _run(f"{_DOCKER} rm {cfg['name']}")
    logger.info("Utility container '{}' stopped", cfg["name"])
    return True


async def exec_in_util(app_config: AppConfig, cmd: str, timeout: float = 60) -> tuple[int, str]:
    """Execute a command in the utility container.

    Returns (returncode, stdout). Raises RuntimeError if container not running.
    """
    cfg = _get_config(app_config)

    if not await is_util_running(app_config):
        raise RuntimeError(f"Utility container '{cfg['name']}' is not running")

    full_cmd = f"{_DOCKER} exec {cfg['name']} {cmd}"
    logger.debug("exec_in_util: {}", cmd)
    rc, stdout, stderr = await _run(full_cmd, timeout=timeout)
    if rc != 0:
        logger.warning("exec_in_util failed (rc={}): {}", rc, stderr)
    return rc, stdout


async def exec_in_util_pty(app_config: AppConfig, cmd: str, timeout: float = 60) -> tuple[int, str]:
    """Execute a command in the utility container using a real PTY.

    Required for commands that need a TTY (e.g., claude-usage-plz which uses pexpect).
    Uses the POSIX PTY wrapper in `pty_compat.PtyProcess` (ptyprocess-backed), same as terminal WebSocket connections.
    """
    from .pty_compat import PtyProcess

    cfg = _get_config(app_config)

    if not await is_util_running(app_config):
        raise RuntimeError(f"Utility container '{cfg['name']}' is not running")

    full_cmd = f"{_DOCKER} exec -it {cfg['name']} {cmd}"
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
                        if "{\n" in combined and combined.rstrip().endswith("}"):
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


_METADATA_DIRS = {"backups", "cache", "telemetry", "plugins", "sessions", "projects"}


async def discover_profiles(app_config: AppConfig) -> list[str]:
    """Scan /profiles in the util container and return sorted profile names.

    The main profile slot (default name "main", configurable via
    ``main_profile_name`` in config.json) is a credential swap target, not a
    tracked profile, so it is excluded from the returned list.
    """
    cfg = _get_config(app_config)
    app_cfg = read_config(app_config)
    main_name = app_cfg.get("main_profile_name") or "main"
    if main_name in _METADATA_DIRS:
        # Hand-configured footgun: the user chose a slot name that clashes
        # with a Claude-managed metadata directory (e.g. "sessions"). Warn
        # loudly so the misconfiguration is obvious; discover_profiles still
        # excludes it so the slot doesn't surface in the Profile Manager.
        logger.warning(
            "discover_profiles: main_profile_name={!r} clashes with a reserved metadata dir — "
            "Claude state may be corrupted. Rename the slot in config.json.",
            main_name,
        )
    excluded = _METADATA_DIRS | {main_name}
    try:
        cmd = f"{_DOCKER} exec {cfg['name']} find /profiles -mindepth 1 -maxdepth 1 -type d"
        rc, stdout, _ = await _run(cmd, timeout=10)
        if rc != 0:
            logger.warning("discover_profiles: failed to list /profiles (rc={})", rc)
            return []
        names = []
        for line in stdout.splitlines():
            name = line.strip().rsplit("/", 1)[-1]
            if name and not name.startswith(".") and name not in excluded:
                names.append(name)
        names.sort()
        logger.info("discover_profiles: found {} profile(s): {}", len(names), names)
        return names
    except Exception:
        logger.exception("discover_profiles: failed to scan /profiles")
        return []


async def _mounts_match(app_config: AppConfig) -> bool:
    """Return True if the running container's bind mounts match the configured mounts."""
    cfg = _get_config(app_config)
    rc, stdout, _ = await _run(f'{_DOCKER} inspect {cfg["name"]} --format "{{{{json .Mounts}}}}"')
    if rc != 0:
        return False
    mounts = json.loads(stdout or "[]")
    actual_bind = {m["Destination"]: pathlib.Path(m["Source"]) for m in mounts if m.get("Type") == "bind"}
    actual_vol = {m["Destination"]: m["Name"] for m in mounts if m.get("Type") == "volume"}

    # If a prior container version bind-mounted mcp_server.py, the new container
    # spec no longer does — treat any stale mcp_server.py bind as a mismatch so
    # the container is recreated.
    if CONTAINER_MCP_PATH in actual_bind:
        logger.debug("_mounts_match: stale mcp_server.py bind mount — will recreate")
        return False

    for host_path, container_path in cfg["mounts"].items():
        expanded = pathlib.Path(host_path.replace("~", str(pathlib.Path.home())))
        if not expanded.exists():
            continue
        if actual_bind.get(container_path) != expanded:
            logger.debug(
                "_mounts_match: {} expected {} got {}",
                container_path,
                expanded,
                actual_bind.get(container_path),
            )
            return False

    for vol_name, container_path in cfg["volumes"].items():
        if actual_vol.get(container_path) != vol_name:
            logger.debug(
                "_mounts_match: volume {} expected {} got {}",
                container_path,
                vol_name,
                actual_vol.get(container_path),
            )
            return False

    return True


async def ensure_util_container(app_config: AppConfig) -> bool:
    """Ensure the utility container is running with the correct mounts.

    If the container is already running with matching mounts, this is a no-op.
    If it is running with stale/wrong mounts, it is recreated.
    tmux session cleanup is handled by CanvasClaudeCard._ensure_tmux_session() on demand.

    Called during server startup if auto_start is enabled.
    """
    cfg = _get_config(app_config)
    if not cfg["auto_start"]:
        logger.info("Utility container auto_start disabled, skipping")
        return False

    if await is_util_running(app_config):
        if await _mounts_match(app_config):
            logger.info("Utility container '{}' already running", cfg["name"])
            return True
        logger.warning("Utility container '{}' running with stale mounts, recreating", cfg["name"])
        await _run(f"{_DOCKER} rm -f {cfg['name']}")

    return await start_container(app_config)
