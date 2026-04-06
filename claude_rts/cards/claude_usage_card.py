"""ClaudeUsageCard: service card that probes claude-usage-plz in the utility container."""

import json
import re
import sys
from .service_card import ServiceCard

_DOCKER = "docker.exe" if sys.platform == "win32" else "docker"
_ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_RESET_HOURS = re.compile(r"(\d+)h\s*(\d+)?m?")
_RESET_MINUTES_ONLY = re.compile(r"(\d+)m")
_AUTH_REQUIRED = re.compile(r"Select login method|Claude Code can be used with your Claude subscription", re.IGNORECASE)
_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z0-9._-]+$")


def _hours_until_reset(reset_str: str) -> float | None:
    """Parse 'in 2h 14m', 'in 45m', etc. into hours. Returns None if unparseable."""
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


class ClaudeUsageCard(ServiceCard):
    """Probes claude-usage for a single profile via the utility container.

    identity  — profile name, e.g. "acct-alice"
    container — utility container name (default: "supreme-claudemander-util")

    Result dict keys:
      profile, five_hour_pct, five_hour_resets, seven_day_pct, seven_day_resets,
      burn_rate (urgency: pct_used/hrs_remaining, higher = more urgent, or None), sonnet_week_pct (optional)
    """

    card_type: str = "claude-usage"

    def probe_command(self) -> str:
        util = self._container or "supreme-claudemander-util"
        if not _SAFE_IDENTIFIER.match(self.identity):
            raise ValueError(
                f"ClaudeUsageCard: identity {self.identity!r} contains invalid characters; must match ^[a-zA-Z0-9._-]+$"
            )
        if not _SAFE_IDENTIFIER.match(util):
            raise ValueError(
                f"ClaudeUsageCard: container {util!r} contains invalid characters; must match ^[a-zA-Z0-9._-]+$"
            )
        return f"{_DOCKER} exec -it {util} claude-usage --claude-dir /profiles/{self.identity} --json"

    def parse_output(self, output: str) -> dict:
        clean = _ANSI_ESCAPE.sub("", output).replace("\r", "").strip()
        if _AUTH_REQUIRED.search(clean):
            raise ValueError(f"claude-usage: profile '{self.identity}' is not authenticated")
        start = clean.find("{")
        end = clean.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"No JSON in probe output: {clean[:200]!r}")
        data = json.loads(clean[start : end + 1])

        if data.get("seven_day_resets") is None:
            raise ValueError(f"claude-usage: profile '{self.identity}' is not authenticated (seven_day_resets is null)")

        five_hr_pct = data.get("five_hour_pct")
        five_hr_resets = data.get("five_hour_resets")
        burn_rate = None
        if five_hr_pct is not None and five_hr_resets:
            hours = _hours_until_reset(five_hr_resets)
            if hours and hours > 0:
                burn_rate = float(five_hr_pct) / hours

        result = {
            "profile": self.identity,
            "five_hour_pct": five_hr_pct,
            "five_hour_resets": five_hr_resets,
            "seven_day_pct": data.get("seven_day_pct"),
            "seven_day_resets": data.get("seven_day_resets"),
            "burn_rate": round(burn_rate, 2) if burn_rate is not None else None,
        }
        if "sonnet_week_pct" in data:
            result["sonnet_week_pct"] = data["sonnet_week_pct"]
        return result
