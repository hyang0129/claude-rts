"""Shared test helpers used by multiple test modules.

Plain classes (not fixtures) so that test files can import them directly:

    from tests.conftest import ProbeCard, MockScrollback, MockSession, MockSessionManager
"""

from unittest.mock import patch

import pytest
from claude_rts.cards.service_card import ServiceCard


@pytest.fixture(autouse=True)
def _no_legacy_migration(tmp_path):
    """Prevent migration from real ~/.claude-rts into test temp dirs."""
    with patch("claude_rts.config._LEGACY_CONFIG_DIR", tmp_path / ".claude-rts-nonexistent"):
        yield


@pytest.fixture(autouse=True)
def _clear_probe_cooldowns():
    """Reset the class-level probe cooldown dict before each test."""
    ServiceCard._probe_cooldowns.clear()
    yield
    ServiceCard._probe_cooldowns.clear()


# ── Concrete test subclass ───────────────────────────────────────────────────


class ProbeCard(ServiceCard):
    """Minimal concrete ServiceCard for testing."""

    card_type = "test-probe"

    def probe_command(self) -> str:
        return "echo hello"

    def parse_output(self, output: str) -> dict:
        return {"raw": output, "parsed": True}


# ── Mock session / session-manager helpers ───────────────────────────────────


class MockScrollback:
    def __init__(self, data: bytes = b""):
        self._data = data

    def get_all(self) -> bytes:
        return self._data


class MockSession:
    def __init__(self, data: bytes = b"", alive: bool = False, session_id: str = "mock-session-01"):
        self.session_id = session_id
        self.alive = alive
        self.scrollback = MockScrollback(data)


class MockSessionManager:
    """Minimal session manager that returns pre-built MockSession objects."""

    def __init__(self, session: "MockSession | None" = None):
        self._session = session or MockSession()
        self.destroyed: list[str] = []

    def create_session(self, cmd, hub=None, container=None, dimensions=(24, 80)):
        return self._session

    def destroy_session(self, session_id, kill_tmux=False):
        self.destroyed.append(session_id)
