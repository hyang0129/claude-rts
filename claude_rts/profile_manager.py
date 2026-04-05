"""Credential Manager: probe loop, burn rate math, health checking, and CRUD for Claude profiles."""

import asyncio
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
    list_profiles,
    probe_usage_via_session,
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
    last_probe_time: Optional[float] = None    # time.monotonic() — last backend read attempt
    last_probe_wall: Optional[float] = None    # time.time() — wall clock of last backend read
    data_timestamp: Optional[float] = None     # probe_time from usage.json — when data was written
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
    """Manages cached credential state, background probe/health loops, and profile CRUD."""

    def __init__(self, session_mgr, probe_interval: int = 1800, health_check_interval: int = 900):
        self._session_mgr = session_mgr
        self._cache: dict[str, CredentialState] = {}
        self._probe_interval = probe_interval
        self._health_check_interval = health_check_interval
        self._probe_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        self._first_probe_done = False

    async def start(self) -> None:
        """Start background probe and health check loops."""
        self._probe_task = asyncio.create_task(self._probe_loop())
        self._health_task = asyncio.create_task(self._health_check_loop())
        logger.info(
            "CredentialManager started (probe_interval={}s, health_check_interval={}s)",
            self._probe_interval,
            self._health_check_interval,
        )

    async def stop(self) -> None:
        """Cancel background tasks."""
        if self._probe_task:
            self._probe_task.cancel()
            try:
                await self._probe_task
            except asyncio.CancelledError:
                pass
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
        logger.info("CredentialManager stopped")

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
        """Return the healthy credential with the lowest burn rate.

        Returns None if the cache is not yet populated or all credentials are stale.
        """
        if not self._first_probe_done:
            return None
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

    def is_cache_ready(self) -> bool:
        """Return True once the first probe cycle has completed."""
        return self._first_probe_done

    def ingest_probe_result(self, name: str, data: dict) -> CredentialState:
        """Store probe data submitted by the frontend PTY probe and update cache.

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
            last_probe_time=time.monotonic(),
            last_probe_wall=time.time(),
            data_timestamp=data.get("probe_time"),
            last_health_check=existing.last_health_check,
            error=None,
        )
        self._cache[name] = state
        return state

    async def force_probe(self, name: str) -> CredentialState:
        """Immediately probe a single profile and update the cache."""
        state = await self._probe_one(name)
        self._cache[name] = state
        return state

    async def force_health_check(self, name: str) -> CredentialState:
        """Immediately run a health check on a single profile and update the cache."""
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
        return {"success": True, "name": name}

    async def delete_profile(self, name: str) -> bool:
        """Remove a profile directory and evict it from the cache.

        Returns True if the deletion succeeded.
        """
        success = await delete_profile_dir(name)
        if success:
            self._cache.pop(name, None)
        return success

    # ── Internal probe helpers ──────────────────────────────────────────────

    async def _probe_one(self, name: str) -> CredentialState:
        """Read usage.json for a profile and return an updated CredentialState."""
        existing = self._cache.get(name, CredentialState(name=name))
        now_mono = time.monotonic()
        now_wall = time.time()
        try:
            result = await probe_usage_via_session(name, self._session_mgr)
            if result is None:
                existing.last_probe_time = now_mono
                existing.last_probe_wall = now_wall
                existing.error = "usage.json not found"
                return existing

            usage_5hr = result.get("five_hour_pct")
            five_hr_resets = result.get("five_hour_resets")
            burn_rate = None
            burn_class = "unknown"
            if usage_5hr is not None and five_hr_resets:
                burn_rate = compute_burn_rate(float(usage_5hr), five_hr_resets)
                if burn_rate is not None:
                    burn_class = classify_burn(burn_rate)

            account_id = await read_account_id_file(name)
            if account_id is None:
                account_id = await get_account_id(name)
                if account_id:
                    await write_account_id_file(name, account_id)

            return CredentialState(
                name=name,
                usage_5hr_pct=usage_5hr,
                usage_daily_pct=result.get("seven_day_pct"),
                five_hour_resets=five_hr_resets,
                seven_day_resets=result.get("seven_day_resets"),
                burn_rate=burn_rate,
                burn_class=burn_class,
                health=existing.health,
                account_id=account_id,
                last_probe_time=now_mono,
                last_probe_wall=now_wall,
                data_timestamp=result.get("probe_time"),
                last_health_check=existing.last_health_check,
                error=None,
            )
        except Exception as exc:
            logger.warning("Failed to probe credential '{}': {}", name, exc)
            existing.last_probe_time = now_mono
            existing.last_probe_wall = now_wall
            existing.error = str(exc)
            return existing

    async def _probe_loop(self) -> None:
        """Background loop: probe all profiles every probe_interval seconds."""
        while True:
            try:
                profiles = await list_profiles()
                for name in profiles:
                    state = await self._probe_one(name)
                    self._cache[name] = state
                self._first_probe_done = True
                logger.debug("Credential probe cycle complete: {} profile(s)", len(profiles))
            except Exception as exc:
                logger.error("Credential probe loop error: {}", exc)
                self._first_probe_done = True  # don't block /best forever on errors
            await asyncio.sleep(self._probe_interval)

    async def _health_check_loop(self) -> None:
        """Background loop: health-check all profiles every health_check_interval seconds."""
        # Initial delay — run health checks after the first probe cycle, not before
        await asyncio.sleep(self._health_check_interval)
        while True:
            try:
                profiles = await list_profiles()
                for name in profiles:
                    healthy = await health_check_profile(name)
                    if name not in self._cache:
                        self._cache[name] = CredentialState(name=name)
                    self._cache[name].health = "healthy" if healthy else "stale"
                    self._cache[name].last_health_check = time.monotonic()
                logger.debug("Health check cycle complete: {} profile(s)", len(profiles))
            except Exception as exc:
                logger.error("Health check loop error: {}", exc)
            await asyncio.sleep(self._health_check_interval)
