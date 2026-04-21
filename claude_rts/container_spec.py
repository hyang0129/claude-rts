"""ContainerSpec abstraction + devcontainer-based container creation.

v1 preset: "devcontainer" — runs `devcontainer up --override-config` with a
temporary devcontainer.json that references a named Docker volume for workspace
storage. No `--workspace-folder` flag is passed (the workspace lives inside a
named volume managed by devcontainer; see OQ-1 resolution on epic #199).

Every created container is stamped with `created_by=canvas-claude` via runArgs
so that Child 1's guard recognises it as Canvas-Claude-owned.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import string
import tempfile
import time
from dataclasses import dataclass, field
from typing import Literal

from loguru import logger

_DEVCONTAINER_CLI = os.environ.get(
    "SUPREME_CLAUDEMANDER_DEVCONTAINER_CLI",
    os.path.expanduser("~/.local/bin/devcontainer"),
)


def generate_container_name() -> str:
    """Generate a short unique container name, e.g. ``cc-<ts>-<rand>``."""
    ts = int(time.time())
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
    return f"cc-{ts}-{rand}"


#: v1 target per epic #199 intent §8 — 2 vCPU / 8 GB RAM / 10 GB workspace.
DEFAULT_CPU_LIMIT: float = 2.0
DEFAULT_MEMORY_LIMIT: str = "8g"
DEFAULT_DISK_LIMIT: str = "10g"
DEFAULT_PIDS_LIMIT: int = 1024

#: Name of the named Docker volume holding Claude credentials. Same volume
#: used by the util container and the ``start-claude`` dev preset so managed
#: containers see the same ``/profiles/<profile>/.credentials.json`` layout.
#: Auto-created by Docker on first use if it does not already exist.
DEFAULT_PROFILES_VOLUME: str = "claude-profiles"


@dataclass
class ContainerSpec:
    """Generic container specification.

    v1 only implements the ``devcontainer`` preset. The dataclass is kept
    intentionally thin — richer preset-specific fields belong on subclasses or
    preset-specific helpers so the abstraction can grow without breaking v1.

    Resource caps (#204) are STRONG invariants: every container gets
    ``--cpus``/``--memory``/``--pids-limit`` stamped into runArgs at creation
    time. ``disk_limit`` is advisory for v1 — Docker named volumes have no
    native size cap on overlay2/ext4. Visibility is Child 7's stats widget.
    """

    image: str
    name: str | None = None
    preset: Literal["devcontainer", "image-only"] = "devcontainer"
    labels: dict[str, str] = field(default_factory=dict)
    mounts: list[str] = field(default_factory=list)
    workspace_volume: str | None = None
    workspace_hint: str | None = None
    # Resource caps (#204). Defaults are the v1 targets; humans can override
    # via ``container_manager.defaults`` in config.
    cpu_limit: float = DEFAULT_CPU_LIMIT
    memory_limit: str = DEFAULT_MEMORY_LIMIT
    disk_limit: str = DEFAULT_DISK_LIMIT
    pids_limit: int = DEFAULT_PIDS_LIMIT
    # Profiles volume mount (#207). Default on — managed containers need
    # access to ``/profiles/<profile>/.credentials.json`` so Canvas Claude can
    # start Claude sessions inside them using the same credentials the util
    # container and ``start-claude`` preset already use. Opt-out via
    # ``mount_profiles=False`` for future flexibility.
    mount_profiles: bool = True
    profiles_volume: str = DEFAULT_PROFILES_VOLUME

    def __post_init__(self) -> None:
        if not self.name:
            self.name = generate_container_name()
        # Canvas-Claude stamp is an ABSOLUTE invariant — always present.
        self.labels.setdefault("created_by", "canvas-claude")
        self.labels.setdefault("supreme-claudemander.managed", "true")
        if not self.workspace_volume:
            self.workspace_volume = f"{self.name}-workspace"

    # ── devcontainer.json generation ────────────────────────────────────

    def devcontainer_preset(self) -> dict:
        """Return the devcontainer.json dict for this spec.

        Uses a named Docker volume for `/workspace` (not a bind mount) so that
        devcontainer-in-devcontainer works inside the RTS devcontainer.
        `runArgs` carries all labels through to `docker run` as ``--label k=v``.
        """
        run_args: list[str] = []
        for k, v in self.labels.items():
            run_args.extend(["--label", f"{k}={v}"])

        # Resource caps (#204) — appended to runArgs as Docker flags. The
        # devcontainer CLI forwards runArgs verbatim to ``docker run``.
        run_args.append(f"--cpus={self.cpu_limit}")
        run_args.append(f"--memory={self.memory_limit}")
        run_args.append(f"--pids-limit={self.pids_limit}")

        mounts = list(self.mounts) or [
            f"source={self.workspace_volume},target=/workspace,type=volume",
        ]
        # Profiles volume mount (#207). Always appended to the default mount
        # list so managed containers see ``/profiles`` with the same layout as
        # the util container. Custom ``mounts=[...]`` callers are respected
        # verbatim (they can add the profiles mount themselves or set
        # ``mount_profiles=False`` explicitly); we only auto-append when the
        # caller did not supply custom mounts AND opted in.
        if self.mount_profiles and not self.mounts:
            profiles_mount = f"source={self.profiles_volume},target=/profiles,type=volume"
            if profiles_mount not in mounts:
                mounts.append(profiles_mount)

        return {
            "image": self.image,
            "mounts": mounts,
            "containerEnv": {},
            "runArgs": run_args,
        }


async def _run_devcontainer_up(spec: ContainerSpec) -> tuple[int, str, str]:
    """Invoke ``devcontainer up --override-config <tmp>`` asynchronously.

    Returns ``(returncode, stdout, stderr)``. The caller interprets the code.
    The subprocess MUST run via ``asyncio.create_subprocess_exec`` so the
    aiohttp event loop is not blocked (STRONG invariant).
    """
    cfg = spec.devcontainer_preset()
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".devcontainer.json",
        delete=False,
    ) as tmp:
        json.dump(cfg, tmp)
        tmp_path = tmp.name

    try:
        argv = [
            _DEVCONTAINER_CLI,
            "up",
            "--id-label",
            f"supreme-claudemander.container={spec.name}",
            "--override-config",
            tmp_path,
        ]
        logger.info("container_create: running {}", " ".join(argv))
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return (
            proc.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def create(spec: ContainerSpec) -> dict:
    """Create a container per its spec. Returns a result dict on success.

    Raises ``RuntimeError`` with the subprocess stderr on failure so the
    handler can return a structured 500 response.
    """
    rc, stdout, stderr = await _run_devcontainer_up(spec)
    if rc != 0:
        logger.warning(
            "container_create: devcontainer up failed (rc={}): {}",
            rc,
            stderr.strip(),
        )
        raise RuntimeError(stderr.strip() or f"devcontainer up exited {rc}")
    logger.info("container_create: created '{}' (image={})", spec.name, spec.image)
    return {
        "name": spec.name,
        "image": spec.image,
        "labels": dict(spec.labels),
        "state": "created",
    }
