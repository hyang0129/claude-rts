"""Tests for hub discovery."""

from unittest.mock import AsyncMock, patch

import pytest

from claude_rts.discovery import discover_hubs


def _mock_process(stdout: bytes, returncode: int = 0):
    """Create a mock asyncio subprocess."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    proc.returncode = returncode
    return proc


@pytest.mark.asyncio
async def test_discover_hubs_parses_docker_output():
    stdout = (
        b"zealous_darwin|d:\\containers\\hub_1\n"
        b"cool_ramanujan|d:\\containers\\hub_3\n"
        b"suspicious_lichterman|d:\\containers\\hub_2\n"
    )
    with patch("claude_rts.discovery.asyncio.create_subprocess_exec", return_value=_mock_process(stdout)):
        hubs = await discover_hubs()

    assert len(hubs) == 3
    # Should be sorted by hub name
    assert hubs[0] == {"hub": "hub_1", "container": "zealous_darwin"}
    assert hubs[1] == {"hub": "hub_2", "container": "suspicious_lichterman"}
    assert hubs[2] == {"hub": "hub_3", "container": "cool_ramanujan"}


@pytest.mark.asyncio
async def test_discover_hubs_handles_forward_slashes():
    stdout = b"my_container|/mnt/d/containers/hub_5\n"
    with patch("claude_rts.discovery.asyncio.create_subprocess_exec", return_value=_mock_process(stdout)):
        hubs = await discover_hubs()

    assert len(hubs) == 1
    assert hubs[0] == {"hub": "hub_5", "container": "my_container"}


@pytest.mark.asyncio
async def test_discover_hubs_returns_empty_on_docker_failure():
    with patch("claude_rts.discovery.asyncio.create_subprocess_exec", return_value=_mock_process(b"", returncode=1)):
        hubs = await discover_hubs()

    assert hubs == []


@pytest.mark.asyncio
async def test_discover_hubs_returns_empty_on_no_containers():
    with patch("claude_rts.discovery.asyncio.create_subprocess_exec", return_value=_mock_process(b"")):
        hubs = await discover_hubs()

    assert hubs == []


@pytest.mark.asyncio
async def test_discover_hubs_skips_malformed_lines():
    stdout = b"good_container|d:\\containers\\hub_1\nbad_line_no_pipe\n|also_bad\nanother_good|d:\\containers\\hub_2\n"
    with patch("claude_rts.discovery.asyncio.create_subprocess_exec", return_value=_mock_process(stdout)):
        hubs = await discover_hubs()

    assert len(hubs) == 2
    assert hubs[0]["hub"] == "hub_1"
    assert hubs[1]["hub"] == "hub_2"


@pytest.mark.asyncio
async def test_discover_hubs_strips_whitespace():
    stdout = b"  spaced_container  |d:\\containers\\hub_1  \n"
    with patch("claude_rts.discovery.asyncio.create_subprocess_exec", return_value=_mock_process(stdout)):
        hubs = await discover_hubs()

    assert len(hubs) == 1
    assert hubs[0]["container"] == "spaced_container"
