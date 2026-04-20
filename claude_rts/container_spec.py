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


@dataclass
class ContainerSpec:
    """Generic container specification.

    v1 only implements the ``devcontainer`` preset. The dataclass is kept
    intentionally thin — richer preset-specific fields belong on subclasses or
    preset-specific helpers so the abstraction can grow without breaking v1.
    """

    image: str
    name: str | None = None
    preset: Literal["devcontainer", "image-only"] = "devcontainer"
    labels: dict[str, str] = field(default_factory=dict)
    mounts: list[str] = field(default_factory=list)
    workspace_volume: str | None = None
    workspace_hint: str | None = None

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

        mounts = list(self.mounts) or [
            f"source={self.workspace_volume},target=/workspace,type=volume",
        ]

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
