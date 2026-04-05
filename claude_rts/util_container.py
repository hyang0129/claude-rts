"""Manage the supreme-claudemander utility container.

The utility container is a lightweight Linux container for background tasks,
monitoring, and status probing. It is NOT for coding or LLM calls.

It stays alive via `sleep infinity` and commands are executed via `docker exec`.
"""

import asyncio
import json
import pathlib
import re
import shutil
import time

from loguru import logger

from .config import read_config

_VALID_PROFILE_NAME = re.compile(r'^[a-zA-Z0-9_-]+$')


def _validate_profile_name(name: str) -> None:
    """Raise ValueError if name contains path traversal or shell metacharacters."""
    if not name or not _VALID_PROFILE_NAME.match(name):
        raise ValueError(f"Invalid profile name: {name!r}. Must match [a-zA-Z0-9_-]+")


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
        f'docker.exe ps --filter "name=^/{cfg["name"]}$" --format "{{{{.Status}}}}"'
    )
    return rc == 0 and "Up" in stdout


async def build_image() -> bool:
    """Build the utility container image if not already built."""
    cfg = _get_config()
    # Check if image exists
    rc, stdout, _ = await _run(f'docker.exe images -q {cfg["image"]}')
    if rc == 0 and stdout.strip():
        logger.debug("Utility image {} already exists", cfg["image"])
        return True

    logger.info("Building utility container image {}...", cfg["image"])
    rc, stdout, stderr = await _run(
        f'docker.exe build -t {cfg["image"]} -f "{DOCKERFILE}" "{DOCKERFILE.parent}"',
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
    await _run(f'docker.exe rm -f {cfg["name"]}')

    # Start container
    cmd = f'docker.exe run -d --name {cfg["name"]}{mount_args} {cfg["image"]}'
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
    rc, _, stderr = await _run(f'docker.exe stop {cfg["name"]}')
    if rc != 0:
        logger.warning("Failed to stop utility container: {}", stderr)
        return False
    await _run(f'docker.exe rm {cfg["name"]}')
    logger.info("Utility container '{}' stopped", cfg["name"])
    return True


async def exec_in_util(cmd: str, timeout: float = 60) -> tuple[int, str]:
    """Execute a command in the utility container.

    Returns (returncode, stdout). Raises RuntimeError if container not running.
    """
    cfg = _get_config()

    if not await is_util_running():
        raise RuntimeError(f"Utility container '{cfg['name']}' is not running")

    full_cmd = f'docker.exe exec {cfg["name"]} {cmd}'
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
    from winpty import PtyProcess

    cfg = _get_config()

    if not await is_util_running():
        raise RuntimeError(f"Utility container '{cfg['name']}' is not running")

    full_cmd = f'docker.exe exec -it {cfg["name"]} {cmd}'
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
                        output.append(data)
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


async def list_profiles() -> list[str]:
    """List available claude profiles in the utility container's /profiles dir."""
    cfg = _get_config()
    # Only return subdirs that contain .credentials.json (actual profiles)
    rc, stdout, _ = await _run(
        f'docker.exe exec {cfg["name"]} bash -c "for d in /profiles/*/; do [ -f \\"$d/.credentials.json\\" ] && basename \\"$d\\"; done"',
        timeout=10,
    )
    if rc != 0:
        return []
    return [name.strip() for name in stdout.split("\n") if name.strip()]


async def probe_usage(claude_dir: str, timeout: float = 60) -> dict | None:
    """Run claude-usage inside the utility container for a specific config dir.

    Uses `script -qc` to provide a PTY (required by claude-usage-plz/pexpect).
    Returns parsed JSON dict or None on failure.
    """
    cfg = _get_config()

    if not await is_util_running():
        return None

    # Write a temp probe script to disk, docker cp it in, then execute.
    # This avoids Windows -> docker -> bash quoting nightmares.
    inner_timeout = max(timeout - 5, 10)
    import tempfile
    script_content = f"#!/bin/bash\nscript -qc 'timeout {inner_timeout} claude-usage --claude-dir {claude_dir} --json' /dev/null 2>/dev/null\n"
    with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False, newline='\n') as f:
        f.write(script_content)
        tmp_path = f.name
    try:
        await _run(f'docker.exe cp "{tmp_path}" {cfg["name"]}:/tmp/_probe.sh', timeout=5)
        cmd = f'docker.exe exec {cfg["name"]} bash /tmp/_probe.sh'
    finally:
        import os
        os.unlink(tmp_path)
    logger.debug("probe_usage: {}", cmd)
    rc, stdout, stderr = await _run(cmd, timeout=timeout)

    if rc != 0:
        logger.warning("claude-usage probe failed for {} (rc={}): {}", claude_dir, rc, stderr[:200] if stderr else "")
        return None

    clean = stdout.replace('\r', '').strip()
    json_start = clean.find('{')
    json_end = clean.rfind('}')
    if json_start < 0 or json_end <= json_start:
        logger.warning("No JSON found in probe output for {}: {}", claude_dir, clean[:200])
        return None

    try:
        return json.loads(clean[json_start:json_end + 1])
    except json.JSONDecodeError as exc:
        logger.warning("claude-usage returned invalid JSON for {}: {}", claude_dir, exc)
        return None


_ANSI_ESCAPE = re.compile(r'\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


def parse_json_from_output(output: str) -> dict | None:
    """Extract and parse a JSON object from raw PTY/ANSI output."""
    clean = _ANSI_ESCAPE.sub('', output).replace('\r', '').strip()
    json_start = clean.find('{')
    json_end = clean.rfind('}')
    if json_start < 0 or json_end <= json_start:
        return None
    try:
        return json.loads(clean[json_start:json_end + 1])
    except json.JSONDecodeError:
        return None


async def probe_usage_via_session(
    name: str,
    session_mgr,
    container_name: str | None = None,
    timeout: float = 90,
) -> dict | None:
    """Run claude-usage inside the utility container via a real ConPTY session.

    Uses SessionManager.create_session() without a container= argument so that
    tmux-wrapping is NOT triggered — the full docker exec command is passed
    directly, giving pexpect the genuine Windows ConPTY it requires.

    Each probe takes 30-60s; the background probe loop staggers calls.
    """
    if container_name is None:
        cfg = _get_config()
        container_name = cfg["name"]

    cmd = f'docker.exe exec -it {container_name} claude-usage --claude-dir /profiles/{name} --json'
    logger.info("probe_usage_via_session: name={} container={}", name, container_name)

    try:
        session = session_mgr.create_session(cmd)
    except Exception as exc:
        logger.error("probe_usage_via_session: failed to create session for {}: {}", name, exc)
        return None

    try:
        deadline = time.monotonic() + timeout
        while session.alive and time.monotonic() < deadline:
            await asyncio.sleep(1)

        if time.monotonic() >= deadline:
            logger.warning("probe_usage_via_session: timed out after {}s for name={}", timeout, name)

        output = session.scrollback.get_all().decode("utf-8", errors="replace")
        result = parse_json_from_output(output)
        if result is None:
            logger.warning(
                "probe_usage_via_session: no JSON found for name={}; raw={!r}",
                name, output[:200],
            )
        return result
    finally:
        session_mgr.destroy_session(session.session_id)


async def health_check_profile(name: str) -> bool:
    """Run `claude --version` with a profile's CLAUDE_CONFIG_DIR to verify token validity.

    Returns True if the command exits successfully (credentials healthy), False if stale or
    the container is not running.
    """
    _validate_profile_name(name)
    try:
        rc, _ = await exec_in_util(
            f'bash -c "CLAUDE_CONFIG_DIR=/profiles/{name}/.claude claude --version"',
            timeout=30,
        )
        return rc == 0
    except RuntimeError:
        logger.warning("health_check_profile({}): utility container not running", name)
        return False
    except Exception as exc:
        logger.warning("health_check_profile({}) error: {}", name, exc)
        return False


async def create_profile_dir(name: str) -> bool:
    """Create the profile directory structure on the host: ~/.claude-profiles/<name>/.claude/

    Returns True on success, False on failure.
    """
    _validate_profile_name(name)
    try:
        profile_dir = pathlib.Path.home() / ".claude-profiles" / name / ".claude"
        profile_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Created profile directory: {}", profile_dir)
        return True
    except Exception as exc:
        logger.error("Failed to create profile dir for '{}': {}", name, exc)
        return False


async def delete_profile_dir(name: str) -> bool:
    """Remove ~/.claude-profiles/<name>/ directory tree on the host.

    Returns True if removed (or didn't exist), False on error.
    """
    _validate_profile_name(name)
    try:
        profile_root = pathlib.Path.home() / ".claude-profiles" / name
        if profile_root.exists():
            shutil.rmtree(profile_root)
            logger.info("Deleted profile directory: {}", profile_root)
        else:
            logger.debug("delete_profile_dir({}): directory does not exist, nothing to do", name)
        return True
    except Exception as exc:
        logger.error("Failed to delete profile dir for '{}': {}", name, exc)
        return False


async def get_account_id(name: str) -> str | None:
    """Try to parse accountId from the profile's .credentials.json.

    Looks for 'accountId', 'account_id', or 'sub' fields in the JSON.
    Returns None if not found or the file doesn't exist.
    """
    _validate_profile_name(name)
    creds_path = pathlib.Path.home() / ".claude-profiles" / name / ".claude" / ".credentials.json"
    if not creds_path.exists():
        return None
    try:
        data = json.loads(creds_path.read_text(encoding="utf-8"))
        for field in ("accountId", "account_id", "sub"):
            value = data.get(field)
            if value:
                return str(value)
        # Also check nested structures (e.g. data inside an "oauth" or "token" key)
        for nested_key in data.values():
            if isinstance(nested_key, dict):
                for field in ("accountId", "account_id", "sub"):
                    value = nested_key.get(field)
                    if value:
                        return str(value)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("get_account_id({}): failed to read credentials.json: {}", name, exc)
    return None


async def read_account_id_file(name: str) -> str | None:
    """Read ~/.claude-profiles/<name>/account_id.txt.

    Returns the stored account ID string, or None if the file doesn't exist.
    """
    _validate_profile_name(name)
    id_path = pathlib.Path.home() / ".claude-profiles" / name / "account_id.txt"
    if not id_path.exists():
        return None
    try:
        return id_path.read_text(encoding="utf-8").strip() or None
    except OSError as exc:
        logger.warning("read_account_id_file({}): {}", name, exc)
        return None


async def write_account_id_file(name: str, account_id: str) -> bool:
    """Write ~/.claude-profiles/<name>/account_id.txt.

    Returns True on success, False on error.
    """
    _validate_profile_name(name)
    id_path = pathlib.Path.home() / ".claude-profiles" / name / "account_id.txt"
    try:
        id_path.parent.mkdir(parents=True, exist_ok=True)
        id_path.write_text(account_id, encoding="utf-8")
        logger.debug("Wrote account_id for profile '{}': {}", name, account_id)
        return True
    except OSError as exc:
        logger.error("write_account_id_file({}): {}", name, exc)
        return False
