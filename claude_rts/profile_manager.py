"""Credential Manager: burn rate math, health checking, and CRUD for Claude profiles.

Probing is handled by the frontend service card (credential-manager widget), which
opens a WebSocket session to run claude-usage inside the utility container and POSTs
the parsed JSON to POST /api/credentials/{name}/probe-result. The backend is
intentionally stateless with respect to probing.
"""

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger

from .util_container import (
    create_profile_dir,
    delete_profile_dir,
    get_account_id,
    health_check_profile,
    read_account_id_file,
    write_account_id_file,
)


@dataclass
class CredentialState:
    name: str
    usage_5hr_pct: Optional[float] = None
    usage_daily_pct: Optional[float] = None
    five_hour_resets: Optional[str] = None
    seven_day_resets: Optional[str] = None
    burn_rate: Optional[float] = None          # usage_pct / hours_until_reset
    burn_class: str = "unknown"                 # "overburning", "normal", "underburning"
    health: str = "unknown"                     # "healthy", "stale", "unknown"
    account_id: Optional[str] = None
    last_probe_time: Optional[float] = None    # time.monotonic() — last probe attempt
    last_probe_wall: Optional[float] = None    # time.time() — wall clock of last probe
    data_timestamp: Optional[float] = None     # wall time when probe result was ingested
    last_health_check: Optional[float] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "usage_5hr_pct": self.usage_5hr_pct,
            "usage_daily_pct": self.usage_daily_pct,
            "five_hour_resets": self.five_hour_resets,
            "seven_day_resets": self.seven_day_resets,
            "burn_rate": self.burn_rate,
            "burn_class": self.burn_class,
            "health": self.health,
            "account_id": self.account_id,
            "last_probe_time": self.last_probe_time,
            "last_probe_wall": self.last_probe_wall,
            "data_timestamp": self.data_timestamp,
            "last_health_check": self.last_health_check,
            "error": self.error,
        }


def parse_hours_until_reset(reset_str: str) -> Optional[float]:
    """Parse a reset time string like '11pm (UTC)' or 'Apr 7, 3pm (UTC)' into hours from now.

    Also handles 'in Xh Ym' format from some probe outputs.
    Returns None if unparseable.
    """
    if not reset_str:
        return None

    # Try "in Xh Ym" or "Xh Ym" format first (e.g. "in 2h 14m", "1h 30m")
    m = re.search(r'(\d+)h\s*(\d+)?m?', reset_str)
    if m:
        hours = int(m.group(1))
        minutes = int(m.group(2)) if m.group(2) else 0
        return hours + minutes / 60

    # Try time-of-day format like "11pm (UTC)" or "3:30pm (UTC)"
    now_utc = datetime.now(timezone.utc)
    m = re.search(r'(\d+)(?::(\d+))?(am|pm)', reset_str, re.IGNORECASE)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        ampm = m.group(3).lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        target = now_utc.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now_utc:
            target = target + timedelta(days=1)
        return (target - now_utc).total_seconds() / 3600

    return None


def compute_burn_rate(usage_pct: float, reset_str: str) -> Optional[float]:
    """burn_rate = usage_pct / hours_until_reset. Returns None if reset_str is unparseable."""
    hours = parse_hours_until_reset(reset_str)
    if hours is None:
        return None
    if hours <= 0:
        return float("inf")
    return usage_pct / hours


def classify_burn(burn_rate: float, reset_window_hours: float = 5.0) -> str:
    """Return 'overburning', 'normal', or 'underburning'."""
    if burn_rate == float("inf"):
        return "overburning"
    nominal = 100.0 / reset_window_hours  # 20/hr for 5hr window
    if burn_rate > nominal:
        return "overburning"
    elif burn_rate < nominal * 0.5:
        return "underburning"
    return "normal"


class CredentialManager:
    """Manages cached credential state, health checking, and profile CRUD.

    Probing is frontend-driven: the credential-manager widget opens a WebSocket
    session to run claude-usage in the utility container and POSTs parsed JSON
    to /api/credentials/{name}/probe-result. This class only stores the results.
    """

    def __init__(self):
        self._cache: dict[str, CredentialState] = {}

    def get_all(self) -> list[CredentialState]:
        """Return all cached credentials sorted by burn rate (highest first, None last)."""
        states = list(self._cache.values())

        def sort_key(s: CredentialState) -> float:
            if s.burn_rate is None:
                return -1.0
            if s.burn_rate == float("inf"):
                return 999999.0
            return s.burn_rate

        return sorted(states, key=sort_key, reverse=True)

    def get(self, name: str) -> Optional[CredentialState]:
        """Return the cached state for a single profile, or None if not found."""
        return self._cache.get(name)

    def get_best(self) -> Optional[CredentialState]:
        """Return the healthy credential with the lowest burn rate, or None."""
        healthy = [
            s
            for s in self._cache.values()
            if s.health == "healthy" and s.burn_rate is not None
        ]
        if not healthy:
            return None
        return min(
            healthy,
            key=lambda s: s.burn_rate if s.burn_rate != float("inf") else 999999.0,
        )

    def ingest_probe_result(self, name: str, data: dict) -> CredentialState:
        """Store probe data submitted by the frontend widget and update cache.

        Accepts the raw JSON dict from claude-usage (keys: five_hour_pct, seven_day_pct, etc.)
        and returns the updated CredentialState.
        """
        existing = self._cache.get(name, CredentialState(name=name))
        usage_5hr = data.get("five_hour_pct")
        five_hr_resets = data.get("five_hour_resets")
        burn_rate = None
        burn_class = "unknown"
        if usage_5hr is not None and five_hr_resets:
            burn_rate = compute_burn_rate(float(usage_5hr), five_hr_resets)
            if burn_rate is not None:
                burn_class = classify_burn(burn_rate)
        now = time.monotonic()
        state = CredentialState(
            name=name,
            usage_5hr_pct=usage_5hr,
            usage_daily_pct=data.get("seven_day_pct"),
            five_hour_resets=five_hr_resets,
            seven_day_resets=data.get("seven_day_resets"),
            burn_rate=burn_rate,
            burn_class=burn_class,
            health=existing.health,
            account_id=existing.account_id,
            last_probe_time=now,
            last_probe_wall=time.time(),
            data_timestamp=time.time(),
            last_health_check=existing.last_health_check,
            error=None,
        )
        self._cache[name] = state
        return state

    async def force_health_check(self, name: str) -> CredentialState:
        """Run a health check on a single profile and update the cache."""
        healthy = await health_check_profile(name)
        if name not in self._cache:
            self._cache[name] = CredentialState(name=name)
        self._cache[name].health = "healthy" if healthy else "stale"
        self._cache[name].last_health_check = time.monotonic()
        return self._cache[name]

    async def create_profile(self, name: str) -> dict:
        """Create the directory structure for a new profile.

        Returns {"success": True, "name": name} or {"success": False, "error": ...}.
        """
        success = await create_profile_dir(name)
        if not success:
            return {"success": False, "error": "Failed to create profile directory"}
        self._cache[name] = CredentialState(name=name, health="unknown")

        # Try to read existing account_id if credentials are already present
        account_id = await read_account_id_file(name)
        if account_id is None:
            account_id = await get_account_id(name)
            if account_id:
                await write_account_id_file(name, account_id)
        if account_id:
            self._cache[name].account_id = account_id

        return {"success": True, "name": name}

    async def delete_profile(self, name: str) -> bool:
        """Remove a profile directory and evict it from the cache.

        Returns True if the deletion succeeded.
        """
        success = await delete_profile_dir(name)
        if success:
            self._cache.pop(name, None)
        return success
