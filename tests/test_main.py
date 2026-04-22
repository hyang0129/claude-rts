"""Tests for CLI entry point."""

import pytest
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
        assert call_kwargs.kwargs["host"] == "127.0.0.1"


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


def test_custom_host():
    """Verify --host 0.0.0.0 is passed through to web.run_app."""
    with (
        patch("sys.argv", ["supreme-claudemander", "--host", "0.0.0.0", "--no-browser"]),
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
        assert call_kwargs.kwargs["host"] == "0.0.0.0"


def test_custom_host_tailscale_ip():
    """Verify --host accepts a Tailscale-range IP and passes it through verbatim."""
    with (
        patch("sys.argv", ["supreme-claudemander", "--host", "100.64.0.1", "--no-browser"]),
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
        assert call_kwargs.kwargs["host"] == "100.64.0.1"


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


def test_electron_flag_launches_electron():
    """Verify --electron adds electron launch callback and cleanup."""
    with (
        patch("sys.argv", ["supreme-claudemander", "--electron"]),
        patch("claude_rts.__main__.web") as _mock_web,
        patch("claude_rts.__main__.create_app") as mock_create,
        patch("claude_rts.__main__.config") as mock_config,
        patch("claude_rts.__main__._check_electron_installed"),
    ):
        mock_config.load.return_value = MagicMock()
        mock_app = MagicMock()
        mock_app.on_startup = []
        mock_app.on_cleanup = []
        mock_create.return_value = mock_app
        try:
            main()
        except SystemExit:
            pass
        # on_startup should have electron launch (not browser)
        assert len(mock_app.on_startup) == 1
        assert mock_app.on_startup[0].__name__ == "launch_electron"
        # cleanup handler registered
        assert len(mock_app.on_cleanup) == 1


def test_electron_flag_skips_browser():
    """Verify --electron does not also open a browser."""
    with (
        patch("sys.argv", ["supreme-claudemander", "--electron"]),
        patch("claude_rts.__main__.web") as _mock_web,
        patch("claude_rts.__main__.create_app") as mock_create,
        patch("claude_rts.__main__.config") as mock_config,
        patch("claude_rts.__main__._check_electron_installed"),
        patch("claude_rts.__main__.webbrowser") as mock_wb,
    ):
        mock_config.load.return_value = MagicMock()
        mock_app = MagicMock()
        mock_app.on_startup = []
        mock_app.on_cleanup = []
        mock_create.return_value = mock_app
        try:
            main()
        except SystemExit:
            pass
        # webbrowser.open should never be referenced in startup
        mock_wb.open.assert_not_called()


def test_config_dir_flag_passes_resolved_path():
    """Verify --config-dir passes a resolved Path to config.load."""
    with (
        patch("sys.argv", ["supreme-claudemander", "--config-dir", "/tmp/test-config", "--no-browser"]),
        patch("claude_rts.__main__.web") as _mock_web,
        patch("claude_rts.__main__.create_app") as mock_create,
        patch("claude_rts.__main__.config") as mock_config,
    ):
        import pathlib

        mock_config.load.return_value = MagicMock()
        mock_create.return_value = MagicMock()
        try:
            main()
        except SystemExit:
            pass
        mock_config.load.assert_called_once_with(pathlib.Path("/tmp/test-config").resolve())


def test_config_dir_not_provided_uses_default():
    """Verify config.load() is called with no args when --config-dir is absent."""
    with (
        patch("sys.argv", ["supreme-claudemander", "--no-browser"]),
        patch("claude_rts.__main__.web") as _mock_web,
        patch("claude_rts.__main__.create_app") as mock_create,
        patch("claude_rts.__main__.config") as mock_config,
    ):
        mock_config.load.return_value = MagicMock()
        mock_create.return_value = MagicMock()
        try:
            main()
        except SystemExit:
            pass
        mock_config.load.assert_called_once_with()


def test_migrate_canvases_flag_runs_and_exits():
    """Epic #236 child 5 (#241): --migrate-canvases runs the migration and exits."""
    with (
        patch("sys.argv", ["supreme-claudemander", "--migrate-canvases"]),
        patch("claude_rts.__main__.web") as mock_web,
        patch("claude_rts.__main__.create_app") as mock_create,
        patch("claude_rts.__main__.config") as mock_config,
        patch("claude_rts.migrations.canvas_236.migrate_canvas_dir") as mock_migrate,
    ):
        mock_config.load.return_value = MagicMock()
        mock_create.return_value = MagicMock()
        mock_migrate.return_value = {"migrated": [], "skipped": [], "errors": []}
        try:
            main()
        except SystemExit as exc:
            assert exc.code == 0  # no errors → exit 0
        mock_migrate.assert_called_once()
        # Server must NOT have been started in migrate mode.
        mock_web.run_app.assert_not_called()


def test_migrate_canvases_flag_exits_nonzero_on_error():
    """--migrate-canvases exits with non-zero status when any file errored."""
    with (
        patch("sys.argv", ["supreme-claudemander", "--migrate-canvases"]),
        patch("claude_rts.__main__.web") as _mock_web,
        patch("claude_rts.__main__.create_app") as _mock_create,
        patch("claude_rts.__main__.config") as mock_config,
        patch("claude_rts.migrations.canvas_236.migrate_canvas_dir") as mock_migrate,
    ):
        mock_config.load.return_value = MagicMock()
        mock_migrate.return_value = {
            "migrated": [],
            "skipped": [],
            "errors": [("/path/to/main.json", "boom")],
        }
        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 1
