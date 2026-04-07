"""Tests for CLI entry point."""

from unittest.mock import patch, MagicMock

from claude_rts.__main__ import main


def test_default_port():
    """Verify default port is 3000 when no args given."""
    with (
        patch("sys.argv", ["supreme-claudemander"]),
        patch("claude_rts.__main__.web") as mock_web,
        patch("claude_rts.__main__.create_app") as mock_create,
        patch("claude_rts.__main__.config") as mock_config,
    ):
        mock_config.load.return_value = MagicMock()
        mock_create.return_value = MagicMock()
        try:
            main()
        except SystemExit:
            pass
        mock_web.run_app.assert_called_once()
        call_kwargs = mock_web.run_app.call_args
        assert call_kwargs.kwargs["port"] == 3000
        assert call_kwargs.kwargs["host"] == "localhost"


def test_custom_port():
    """Verify --port flag is respected."""
    with (
        patch("sys.argv", ["supreme-claudemander", "--port", "4000"]),
        patch("claude_rts.__main__.web") as mock_web,
        patch("claude_rts.__main__.create_app") as mock_create,
        patch("claude_rts.__main__.config") as mock_config,
    ):
        mock_config.load.return_value = MagicMock()
        mock_create.return_value = MagicMock()
        try:
            main()
        except SystemExit:
            pass
        call_kwargs = mock_web.run_app.call_args
        assert call_kwargs.kwargs["port"] == 4000


def test_no_browser_flag():
    """Verify --no-browser skips browser open."""
    with (
        patch("sys.argv", ["supreme-claudemander", "--no-browser"]),
        patch("claude_rts.__main__.web") as _mock_web,
        patch("claude_rts.__main__.create_app") as mock_create,
        patch("claude_rts.__main__.config") as mock_config,
    ):
        mock_config.load.return_value = MagicMock()
        mock_app = MagicMock()
        mock_app.on_startup = []
        mock_create.return_value = mock_app
        try:
            main()
        except SystemExit:
            pass
        # on_startup should be empty (no browser callback added)
        assert len(mock_app.on_startup) == 0


def test_browser_opens_by_default():
    """Verify browser open callback is added by default."""
    with (
        patch("sys.argv", ["supreme-claudemander"]),
        patch("claude_rts.__main__.web") as _mock_web,
        patch("claude_rts.__main__.create_app") as mock_create,
        patch("claude_rts.__main__.config") as mock_config,
    ):
        mock_config.load.return_value = MagicMock()
        mock_app = MagicMock()
        mock_app.on_startup = []
        mock_create.return_value = mock_app
        try:
            main()
        except SystemExit:
            pass
        # on_startup should have the browser callback
        assert len(mock_app.on_startup) == 1
