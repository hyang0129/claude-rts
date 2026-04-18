"""ClaudeUsageCard: probes claude /usage via interactive PTY puppeting."""

import asyncio
import re
import time

import pyte
from loguru import logger

from .service_card import ServiceCard

_DOCKER = "docker"
_RESET_HOURS = re.compile(r"(\d+)h\s*(\d+)?m?")
_RESET_MINUTES_ONLY = re.compile(r"(\d+)m")
_AUTH_REQUIRED = re.compile(
    r"Select login method|Claude Code can be used with your Claude subscription",
    re.IGNORECASE,
)
_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z0-9._-]+$")

_SCREEN_COLS = 120
_SCREEN_ROWS = 40


def _hours_until_reset(reset_str: str) -> float | None:
    """Parse 'in 2h 14m', 'in 45m', etc. into hours."""
    if not reset_str:
        return None
    m = _RESET_HOURS.search(reset_str)
    if m:
        hours = int(m.group(1))
        minutes = int(m.group(2)) if m.group(2) else 0
        return hours + minutes / 60
    m = _RESET_MINUTES_ONLY.search(reset_str)
    if m:
        return int(m.group(1)) / 60
    return None


def _parse_screen(text: str) -> dict:
    """Extract usage percentages and reset times from the /usage screen."""
    lines = text.split("\n")
    result = {
        "five_hour_pct": None,
        "five_hour_resets": None,
        "seven_day_pct": None,
        "seven_day_resets": None,
        "sonnet_week_pct": None,
    }
    for i, line in enumerate(lines):
        m = re.search(r"(\d+)%\s*used", line)
        if not m:
            continue
        pct = float(m.group(1))
        ctx = " ".join(lines[max(0, i - 3) : i]).lower()
        resets = None
        for j in range(i, min(len(lines), i + 3)):
            rm = re.search(r"[Rr]eset[s ]+(.*?)$", lines[j])
            if rm:
                resets = rm.group(1).strip()
                break
        if "session" in ctx and result["five_hour_pct"] is None:
            result["five_hour_pct"] = pct
            result["five_hour_resets"] = resets
        elif "sonnet" in ctx:
            result["sonnet_week_pct"] = pct
        elif "week" in ctx and result["seven_day_pct"] is None:
            result["seven_day_pct"] = pct
            result["seven_day_resets"] = resets
    return result


class ClaudeUsageCard(ServiceCard):
    """Probes claude /usage for a single profile via interactive PTY puppeting.

    identity  — profile name, e.g. "acct-alice" (mounted at /profiles/{identity})
    container — utility container name (default: "supreme-claudemander-util")

    Result dict keys:
      profile, five_hour_pct, five_hour_resets, seven_day_pct, seven_day_resets,
      burn_rate, sonnet_week_pct (optional)
    """

    card_type: str = "claude-usage"

    def probe_command(self) -> str:
        util = self._container or "supreme-claudemander-util"
        if not _SAFE_IDENTIFIER.match(self.identity):
            raise ValueError(f"ClaudeUsageCard: identity {self.identity!r} must match ^[a-zA-Z0-9._-]+$")
        if not _SAFE_IDENTIFIER.match(util):
            raise ValueError(f"ClaudeUsageCard: container {util!r} must match ^[a-zA-Z0-9._-]+$")
        return (
            f"{_DOCKER} exec -it {util} "
            f"env CLAUDE_CONFIG_DIR=/profiles/{self.identity} "
            f"claude --dangerously-skip-permissions"
        )

    def parse_output(self, output: str) -> dict:
        """Fallback parser — not used by _puppet_probe but required by ServiceCard ABC."""
        result = _parse_screen(output)
        if result["five_hour_pct"] is None and result["seven_day_pct"] is None:
            raise ValueError(f"No usage data in output: {output[:200]!r}")
        return result

    async def run_probe(self) -> dict | None:
        """Override ServiceCard.run_probe to use interactive PTY puppeting.

        Respects the per-credential cooldown from ServiceCard: if this identity
        was probed within PROBE_COOLDOWN_SECONDS, returns the cached result.
        """
        last_probe_time = ServiceCard._probe_cooldowns.get(self.identity)
        if last_probe_time is not None:
            elapsed = time.monotonic() - last_probe_time
            if elapsed < self.PROBE_COOLDOWN_SECONDS:
                logger.debug(
                    "ClaudeUsageCard {}: probe skipped — cooldown active ({:.0f}s remaining)",
                    self.identity,
                    self.PROBE_COOLDOWN_SECONDS - elapsed,
                )
                return self._last_result

        return await self._puppet_probe()

    async def start_visible_probe(self) -> str:
        """Create a visible PTY session and start the puppet probe as a background task.

        Returns the session_id so the frontend can attach a terminal card to it.
        """
        cmd = self.probe_command()
        logger.info("ClaudeUsageCard {}: starting visible probe cmd={!r}", self.identity, cmd)
        session = self._session_manager.create_session(
            cmd,
            hub=None,
            container=None,
            dimensions=(_SCREEN_ROWS, _SCREEN_COLS),
        )
        asyncio.create_task(self._puppet_probe(session=session))
        return session.session_id

    async def _puppet_probe(self, session=None) -> dict | None:
        """Spawn claude interactively inside the util container, send /usage, parse result.

        Flow:
          1. Create (or reuse) a PTY session running claude with CLAUDE_CONFIG_DIR=/profiles/{identity}
          2. Feed scrollback into a pyte virtual screen
          3. Handle trust-folder / bypass-permissions dialogs
          4. On welcome splash: send /usage once
          5. On % used appearing: parse and return structured result
        """
        owns_session = session is None
        if owns_session:
            cmd = self.probe_command()
            logger.info("ClaudeUsageCard {}: puppet probe starting cmd={!r}", self.identity, cmd)
            try:
                session = self._session_manager.create_session(
                    cmd,
                    hub=None,
                    container=None,
                    dimensions=(_SCREEN_ROWS, _SCREEN_COLS),
                )
            except Exception:
                logger.exception("ClaudeUsageCard {}: failed to create session", self.identity)
                return None

        screen = pyte.Screen(_SCREEN_COLS, _SCREEN_ROWS)
        stream = pyte.Stream(screen)
        consumed_size = 0

        def _feed_new() -> None:
            nonlocal consumed_size
            all_data = session.scrollback.get_all()
            new_bytes = all_data[consumed_size:]
            if new_bytes:
                stream.feed(new_bytes.decode("utf-8", errors="replace"))
                consumed_size = len(all_data)

        def _screen_text() -> str:
            return "\n".join(screen.display[i].rstrip() for i in range(screen.lines))

        def _log_screen(tag: str) -> None:
            lines = [ln for ln in _screen_text().split("\n") if ln.strip()]
            logger.debug("ClaudeUsageCard {} [{}]: {} screen lines", self.identity, tag, len(lines))
            for ln in lines:
                logger.debug("  | {}", ln)

        deadline = asyncio.get_running_loop().time() + self._probe_timeout
        trust_accepted = False
        bypass_accepted = False
        usage_sent = False
        last_log_at = asyncio.get_running_loop().time()
        result = None

        try:
            while asyncio.get_running_loop().time() < deadline:
                if not session.alive:
                    logger.warning("ClaudeUsageCard {}: session died unexpectedly", self.identity)
                    break

                _feed_new()
                t = _screen_text()
                now = asyncio.get_running_loop().time()
                elapsed = now - (deadline - self._probe_timeout)

                # Log screen every 5s
                if now - last_log_at >= 5:
                    _log_screen(f"t={elapsed:.1f}s")
                    last_log_at = now

                # Not authenticated
                if _AUTH_REQUIRED.search(t):
                    logger.error("ClaudeUsageCard {}: profile not authenticated", self.identity)
                    break

                # Rate limited — exit early rather than timing out
                if usage_sent and "rate_limit_error" in t:
                    logger.warning("ClaudeUsageCard {}: usage API rate limited, will retry later", self.identity)
                    _log_screen("rate-limited")
                    break

                # First-run theme picker
                if "Choose the text style" in t or "Let's get started" in t:
                    logger.info("ClaudeUsageCard {}: first-run theme picker — accepting default", self.identity)
                    _log_screen("theme-picker")
                    session.pty.write("\r")
                    await asyncio.sleep(2)
                    continue

                # Trust-folder dialog
                if "Yes, I trust this folder" in t and not trust_accepted:
                    logger.info("ClaudeUsageCard {}: trust-folder dialog — accepting", self.identity)
                    _log_screen("trust-dialog")
                    session.pty.write("\r")
                    trust_accepted = True
                    await asyncio.sleep(2)
                    continue

                # Bypass-permissions dialog
                if ("Yes, I accept" in t or "Bypass Permissions" in t) and not bypass_accepted:
                    logger.info("ClaudeUsageCard {}: bypass-permissions dialog — accepting", self.identity)
                    _log_screen("bypass-dialog")
                    session.pty.write("\x1b[B")  # down arrow to "Yes, I accept"
                    await asyncio.sleep(0.3)
                    session.pty.write("\r")
                    bypass_accepted = True
                    await asyncio.sleep(3)
                    continue

                # Claude ready — send /usage once
                if not usage_sent and ("Welcome back" in t or "Tips for getting started" in t):
                    logger.info(
                        "ClaudeUsageCard {}: claude ready at t={:.1f}s, sending /usage",
                        self.identity,
                        elapsed,
                    )
                    _log_screen("ready")
                    for ch in "/usage":
                        session.pty.write(ch)
                        await asyncio.sleep(0.05)
                    await asyncio.sleep(1)
                    _feed_new()
                    session.pty.write("\r")
                    usage_sent = True
                    await asyncio.sleep(1)
                    continue

                # Usage data on screen
                if usage_sent and "%" in t and "used" in t:
                    await asyncio.sleep(1)  # let rendering settle
                    _feed_new()
                    _log_screen("usage-result")
                    parsed = _parse_screen(_screen_text())
                    logger.info(
                        "ClaudeUsageCard {}: parsed 5hr={}% 7d={}% sonnet={}%",
                        self.identity,
                        parsed["five_hour_pct"],
                        parsed["seven_day_pct"],
                        parsed["sonnet_week_pct"],
                    )
                    if parsed["five_hour_pct"] is None and parsed["seven_day_pct"] is None:
                        logger.warning("ClaudeUsageCard {}: screen had % but no usage data parsed", self.identity)
                        _log_screen("parse-fail")
                        break

                    five_hr_pct = parsed["five_hour_pct"]
                    five_hr_resets = parsed["five_hour_resets"]
                    seven_day_pct = parsed["seven_day_pct"]
                    seven_day_resets = parsed["seven_day_resets"]
                    burn_rate = None
                    if seven_day_pct is not None and seven_day_resets:
                        hours = _hours_until_reset(seven_day_resets)
                        if hours and hours > 0:
                            burn_rate = (100.0 - float(seven_day_pct)) / hours * 24

                    result = {
                        "profile": self.identity,
                        "five_hour_pct": five_hr_pct,
                        "five_hour_resets": five_hr_resets,
                        "seven_day_pct": parsed["seven_day_pct"],
                        "seven_day_resets": parsed["seven_day_resets"],
                        "burn_rate": round(burn_rate, 2) if burn_rate is not None else None,
                    }
                    if parsed["sonnet_week_pct"] is not None:
                        result["sonnet_week_pct"] = parsed["sonnet_week_pct"]
                    break

                await asyncio.sleep(0.5)

            else:
                logger.warning(
                    "ClaudeUsageCard {}: timed out after {}s",
                    self.identity,
                    self._probe_timeout,
                )
                _log_screen("timeout")

        finally:
            # Graceful exit; only destroy if we created the session (visible sessions are
            # destroyed when the user closes the terminal card)
            try:
                session.pty.write("\x1b")
                await asyncio.sleep(0.3)
                session.pty.write("/exit\r")
                await asyncio.sleep(0.5)
            except Exception:
                pass
            if owns_session:
                self._session_manager.destroy_session(session.session_id)

        if result is not None:
            self._last_result = result
            ServiceCard._probe_cooldowns[self.identity] = time.monotonic()
            logger.info(
                "ClaudeUsageCard {}: probe succeeded, notifying {} subscriber(s)",
                self.identity,
                len(self._subscribers),
            )
            await self._notify_subscribers(result)

        return result
