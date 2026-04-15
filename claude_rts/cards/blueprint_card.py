"""BlueprintCard: server-side orchestrator card that executes a blueprint step list."""

import asyncio
import datetime
import pathlib
import uuid

from loguru import logger

from .base import BaseCard
from .container_starter_card import ContainerStarterCard
from ..blueprint import (
    interpolate_value,
    DEFAULT_TIMEOUTS,
    _VAR_NAME_RE,
)


class BlueprintCard(BaseCard):
    """First-class server-side card that executes a blueprint step list.

    Steps execute strictly sequentially. Each step may produce an output
    variable (via the ``out`` field) that subsequent steps can reference.

    The card emits ``blueprint:log`` events during execution for real-time
    frontend rendering, and ``blueprint:completed`` or ``blueprint:failed``
    on termination. After emitting the terminal event, the card unregisters
    itself from CardRegistry.

    Execution is logged server-side to an append-only log file.
    """

    card_type: str = "blueprint"
    hidden: bool = False

    def __init__(
        self,
        blueprint: dict,
        app=None,
        card_id: str | None = None,
        context: dict | None = None,
    ):
        super().__init__(card_id=card_id)
        self.blueprint = blueprint
        self._app = app
        self._context = context or {}
        self.run_id = uuid.uuid4().hex[:12]
        self.variables: dict = {}
        self.log_lines: list[str] = []
        self._task: asyncio.Task | None = None
        self._execution_log_path: pathlib.Path | None = None

        # Descriptor for frontend rendering
        self.layout: dict = {}

    def to_descriptor(self) -> dict:
        """Return the JSON-serializable descriptor the frontend expects."""
        desc = {
            "type": self.card_type,
            "card_id": self.id,
            "run_id": self.run_id,
            "blueprint_name": self.blueprint.get("name", "unknown"),
        }
        if self.layout:
            desc.update(self.layout)
        return desc

    async def start(self) -> None:
        """Begin blueprint execution in a background task."""
        # Initialize variables from context/defaults
        for param in self.blueprint.get("parameters", []):
            pname = param["name"]
            if pname in self._context:
                self.variables[pname] = self._context[pname]
            elif "default" in param:
                self.variables[pname] = param["default"]

        # Set up execution log
        if self._app and "app_config" in self._app:
            log_dir = self._app["app_config"].config_dir / "blueprint_logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            self._execution_log_path = log_dir / f"{self.run_id}.log"

        self._task = asyncio.create_task(self._execute())

    async def stop(self) -> None:
        """Cancel execution if still running."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _log(self, message: str) -> None:
        """Emit a timestamped log line."""
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        line = f"[{ts}] {message}"
        self.log_lines.append(line)

        # Write to execution log file
        if self._execution_log_path:
            try:
                with open(self._execution_log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass

        # Emit to EventBus
        if self.bus is not None:
            await self.bus.emit(
                "blueprint:log",
                {"run_id": self.run_id, "message": message, "ts": ts},
            )

    async def _execute(self) -> None:
        """Execute the blueprint step list sequentially."""
        bp_name = self.blueprint.get("name", "unknown")
        steps = self.blueprint.get("steps", [])

        await self._log(f"Starting blueprint '{bp_name}' (run_id={self.run_id})")

        i = -1
        try:
            for i, step in enumerate(steps):
                action = step.get("action", "unknown")
                await self._log(f"Step {i}: {action}")

                timeout = step.get("timeout", DEFAULT_TIMEOUTS.get(action, 30))

                # Add a 5s buffer to the outer wait_for so that steps with
                # their own internal timeouts (e.g. start_container via
                # ContainerStarterCard, open_widget ack) fire first and
                # produce a more descriptive error.
                outer_timeout = timeout + 5

                try:
                    result = await asyncio.wait_for(
                        self._execute_step(step, i),
                        timeout=outer_timeout,
                    )
                except asyncio.TimeoutError:
                    raise RuntimeError(f"Step {i} ({action}) timed out after {timeout}s")

                # Bind output variable
                out = step.get("out")
                if out and result is not None:
                    self.variables[out] = result
                    await self._log(f"  -> ${out} = {result!r}")

            # Success
            await self._log(f"Blueprint '{bp_name}' completed successfully")
            if self.bus is not None:
                await self.bus.emit(
                    "blueprint:completed",
                    {"run_id": self.run_id, "blueprint_name": bp_name},
                )

        except asyncio.CancelledError:
            await self._log(f"Blueprint '{bp_name}' cancelled")
            raise
        except Exception as exc:
            await self._log(f"Blueprint '{bp_name}' FAILED: {exc}")
            if self.bus is not None:
                await self.bus.emit(
                    "blueprint:failed",
                    {
                        "run_id": self.run_id,
                        "blueprint_name": bp_name,
                        "step": i,
                        "error": str(exc),
                    },
                )
        finally:
            # Self-close: unregister from CardRegistry
            if self._app is not None and "card_registry" in self._app:
                self._app["card_registry"].unregister(self.id)
                logger.debug("BlueprintCard {}: self-unregistered", self.id)

    async def _execute_step(self, step: dict, index: int) -> object:
        """Execute a single step and return the result."""
        action = step["action"]

        # Resolve variable references in step fields
        resolved = {}
        for key, val in step.items():
            if key in ("action", "out", "steps"):
                resolved[key] = val
            else:
                resolved[key] = interpolate_value(val, self.variables, field_name=key)

        if action == "get_priority_profile":
            return await self._step_get_priority_profile(resolved)
        elif action == "discover_containers":
            return await self._step_discover_containers(resolved)
        elif action == "start_container":
            return await self._step_start_container(resolved)
        elif action == "open_terminal":
            return await self._step_open_terminal(resolved)
        elif action == "open_claude_terminal":
            return await self._step_open_claude_terminal(resolved)
        elif action == "open_widget":
            return await self._step_open_widget(resolved)
        elif action == "for_each":
            return await self._step_for_each(step, index)
        else:
            raise RuntimeError(f"Unknown action: {action}")

    # ── Step implementations ──────────────────────────────────────────

    async def _step_get_priority_profile(self, step: dict) -> str:
        """Retrieve the current priority profile from config."""
        if self._app is None:
            raise RuntimeError("No app reference — cannot read config")

        from ..config import read_config

        app_config = self._app["app_config"]
        config = read_config(app_config)
        profile = config.get("priority_profile")
        if not profile:
            raise RuntimeError("No priority_profile configured")
        await self._log(f"  Priority profile: {profile}")
        return profile

    async def _step_discover_containers(self, step: dict) -> list:
        """Discover containers via the VM discover API."""
        if self._app is None:
            raise RuntimeError("No app reference — cannot discover containers")

        # Use mock data if in test mode
        test_containers = self._app.get("_test_vm_containers")
        if test_containers is not None:
            containers = [c["name"] for c in test_containers]
            await self._log(f"  Discovered {len(containers)} container(s): {containers}")
            return containers

        import sys

        _docker_cmd = "docker.exe" if sys.platform == "win32" else "docker"
        proc = await asyncio.create_subprocess_exec(
            _docker_cmd,
            "ps",
            "-a",
            "--format",
            "{{.Names}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"docker ps failed: {stderr.decode().strip()}")

        containers = [line.strip() for line in stdout.decode().strip().splitlines() if line.strip()]
        await self._log(f"  Discovered {len(containers)} container(s)")
        return containers

    async def _step_start_container(self, step: dict) -> str:
        """Start a container and wait for exec-readiness via ContainerStarterCard."""
        container_name = step.get("container")
        if not container_name:
            raise RuntimeError("start_container step requires 'container' field")

        if self._app is None:
            raise RuntimeError("No app reference — cannot start container")

        # Create an event to wait for readiness
        ready_event = asyncio.Event()
        result_holder = {"error": None}

        async def on_ready(event_type, payload):
            ready_event.set()

        async def on_failed(event_type, payload):
            result_holder["error"] = payload.get("error", "unknown error")
            ready_event.set()

        # Subscribe to events BEFORE spawning the starter card
        if self.bus is not None:
            self.bus.subscribe(f"container:ready:{container_name}", on_ready)
            self.bus.subscribe(f"container:failed:{container_name}", on_failed)

        try:
            # Spawn ContainerStarterCard
            starter = ContainerStarterCard(
                container_name=container_name,
                app=self._app,
                timeout=step.get("timeout", DEFAULT_TIMEOUTS["start_container"]),
            )
            starter.bus = self.bus
            self._app["card_registry"].register(starter)
            await starter.start()

            # Wait for the event
            await ready_event.wait()

            if result_holder["error"]:
                raise RuntimeError(result_holder["error"])

            await self._log(f"  Container '{container_name}' is ready")
            return container_name

        finally:
            # Unsubscribe
            if self.bus is not None:
                self.bus.unsubscribe(f"container:ready:{container_name}", on_ready)
                self.bus.unsubscribe(f"container:failed:{container_name}", on_failed)

    async def _step_open_terminal(self, step: dict) -> dict:
        """Open a terminal card via the server API.

        Always starts an interactive shell session. When a ``cmd`` is provided
        it is injected into the PTY after the shell settles, matching the ADR
        "injected into terminal's PTY at spawn time" model.
        """
        if self._app is None:
            raise RuntimeError("No app reference — cannot open terminal")

        from .terminal_card import TerminalCard

        cmd = step.get("cmd", "")
        hub = step.get("hub")
        container = step.get("container")
        layout = {}
        for key in ("x", "y", "w", "h"):
            if key in step:
                layout[key] = step[key]

        mgr = self._app["session_manager"]
        card_registry = self._app["card_registry"]

        # Always spawn with bash so the session stays interactive.
        # The user-supplied cmd is injected into the PTY after the shell settles.
        card = TerminalCard(
            session_manager=mgr,
            cmd="bash -l",
            hub=hub,
            container=container,
            layout=layout,
        )
        await card.start()
        card_registry.register(card)

        # Inject the user cmd via tmux send-keys (tmux-backed) or raw PTY write
        # (non-tmux). tmux send-keys bypasses the terminal handshake entirely —
        # raw PTY writes race with tmux's device-attributes response and corrupt input.
        if cmd and card.session:
            import sys as _sys

            _docker = "docker.exe" if _sys.platform == "win32" else "docker"
            session_id = card.session_id
            if card.session.tmux_backed and container:
                await asyncio.sleep(0.5)  # let tmux shell settle
                proc = await asyncio.create_subprocess_exec(
                    _docker,
                    "exec",
                    container,
                    "tmux",
                    "send-keys",
                    "-t",
                    session_id,
                    cmd,
                    "Enter",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode != 0:
                    await self._log(f"  Warning: tmux send-keys failed: {stderr.decode().strip()}")
                else:
                    await self._log(f"  Injected cmd via tmux send-keys into {session_id}: {cmd!r}")
            elif card.session.pty:
                # Non-tmux: write directly — no handshake race since there's no tmux layer.
                await asyncio.sleep(0.3)
                card.session.pty.write(cmd.encode() + b"\n")
                await self._log(f"  Injected cmd via PTY write into {session_id}: {cmd!r}")

        desc = card.to_descriptor()
        # Expose the actual user cmd in the descriptor exec field for the frontend label.
        if cmd:
            desc["exec"] = cmd
        await self._log(f"  Opened terminal {card.session_id}")
        return desc

    async def _step_open_claude_terminal(self, step: dict) -> dict:
        """Open a Canvas Claude terminal card."""
        if self._app is None:
            raise RuntimeError("No app reference — cannot open Claude terminal")

        from .canvas_claude_card import CanvasClaudeCard
        from ..config import read_config

        container = step.get("container", "supreme-claudemander-util")
        hub = step.get("hub", "canvas-claude")
        profile = step.get("profile")
        canvas_name = step.get("canvas_name")
        api_base_url = step.get("api_base_url", "http://host.docker.internal:3000")

        # Resolve profile from inject or priority
        inject = step.get("inject", {})
        if not profile and "credential" in inject:
            profile = inject["credential"]
        if not profile:
            app_config = self._app["app_config"]
            config = read_config(app_config)
            profile = config.get("priority_profile")

        if not profile:
            raise RuntimeError("open_claude_terminal requires a profile (no priority_profile configured)")

        layout = {}
        for key in ("x", "y", "w", "h"):
            if key in step:
                layout[key] = step[key]

        mgr = self._app["session_manager"]
        card_registry = self._app["card_registry"]

        card = CanvasClaudeCard(
            session_manager=mgr,
            hub=hub,
            container=container,
            layout=layout,
            api_base_url=api_base_url,
            profile=profile,
            canvas_name=canvas_name,
        )
        await card.start()
        card_registry.register(card)

        desc = card.to_descriptor()
        await self._log(f"  Opened Claude terminal {card.session_id} (profile={profile})")
        return desc

    async def _step_open_widget(self, step: dict) -> str:
        """Open a widget card by broadcasting to the frontend.

        Emits a ``blueprint:open_widget`` event and waits for an ack
        from the frontend via the EventBus.
        """
        widget_type = step.get("widget_type")
        if not widget_type:
            raise RuntimeError("open_widget step requires 'widget_type' field")

        ack_event = asyncio.Event()
        result_holder = {"card_id": None}

        async def on_ack(event_type, payload):
            if payload.get("run_id") == self.run_id:
                result_holder["card_id"] = payload.get("card_id")
                ack_event.set()

        if self.bus is not None:
            self.bus.subscribe("blueprint:widget_ack", on_ack)

        try:
            # Emit the open_widget request
            layout = {}
            for key in ("x", "y", "w", "h"):
                if key in step:
                    layout[key] = step[key]

            if self.bus is not None:
                await self.bus.emit(
                    "blueprint:open_widget",
                    {
                        "run_id": self.run_id,
                        "widget_type": widget_type,
                        "layout": layout,
                    },
                )

            await asyncio.wait_for(ack_event.wait(), timeout=step.get("timeout", 30))
            await self._log(f"  Opened widget '{widget_type}' (card_id={result_holder['card_id']})")
            return result_holder["card_id"]
        finally:
            if self.bus is not None:
                self.bus.unsubscribe("blueprint:widget_ack", on_ack)

    async def _step_for_each(self, step: dict, index: int) -> None:
        """Iterate sub-steps over a list variable."""
        list_var = step.get("list")
        if not list_var:
            raise RuntimeError("for_each step requires 'list' field")

        # Resolve the list reference. If it's a bare $variable reference
        # pointing to a list, retrieve the list directly instead of
        # string-interpolating it (which would stringify the list).
        _bare_var = None
        if isinstance(list_var, str) and list_var.startswith("$"):
            candidate = list_var[1:]
            if _VAR_NAME_RE.match(candidate):
                _bare_var = candidate
        if _bare_var:
            var_name = _bare_var
            if var_name not in self.variables:
                raise RuntimeError(f"Unresolvable variable: ${var_name}")
            list_value = self.variables[var_name]
        elif isinstance(list_var, list):
            list_value = list_var
        else:
            list_value = interpolate_value(list_var, self.variables)

        if not isinstance(list_value, list):
            raise RuntimeError(f"for_each 'list' must resolve to a list, got {type(list_value).__name__}")

        item_var = step.get("item_var", "item")
        sub_steps = step.get("steps", [])

        await self._log(f"  for_each over {len(list_value)} item(s)")

        for j, item in enumerate(list_value):
            self.variables[item_var] = item
            await self._log(f"  [{j}] ${item_var} = {item!r}")

            for k, sub_step in enumerate(sub_steps):
                action = sub_step.get("action", "unknown")
                await self._log(f"    Step {index}.{j}.{k}: {action}")

                sub_timeout = sub_step.get("timeout", DEFAULT_TIMEOUTS.get(action, 30))
                result = await asyncio.wait_for(
                    self._execute_step(sub_step, f"{index}.{j}.{k}"),
                    timeout=sub_timeout,
                )

                sub_out = sub_step.get("out")
                if sub_out and result is not None:
                    self.variables[sub_out] = result

        return None
