"""CLI entry point: start server and open browser/electron."""

import argparse
import os
import pathlib
import subprocess
import sys
import webbrowser
from importlib.metadata import version as _pkg_version

from aiohttp import web
from loguru import logger

from . import config
from .server import create_app

# Resolve electron/ directory: env var override → repo-relative fallback
_env_electron_dir = os.environ.get("SUPREME_CLAUDEMANDER_ELECTRON_DIR")
if _env_electron_dir:
    _ELECTRON_DIR = pathlib.Path(_env_electron_dir).resolve()
else:
    _ELECTRON_DIR = pathlib.Path(__file__).resolve().parent.parent / "electron"


def _get_version() -> str:
    """Return the installed package version, falling back to 'unknown'."""
    try:
        return _pkg_version("supreme-claudemander")
    except Exception:
        return "unknown"


def _check_electron_installed():
    """Exit with instructions if electron/ directory or node_modules is missing."""
    if not _ELECTRON_DIR.exists():
        print(
            "Electron shell is not available in pip-installed packages.\n"
            "Option 1: set SUPREME_CLAUDEMANDER_ELECTRON_DIR to the 'electron/' directory in your local clone.\n"
            "Option 2: clone the repository and run 'npm install' in the electron/ directory.\n"
            "See: https://github.com/hyang0129/supreme-claudemander",
            file=sys.stderr,
        )
        sys.exit(1)
    node_modules = _ELECTRON_DIR / "node_modules"
    if node_modules.exists():
        return
    print(
        f"Electron dependencies not installed.\nRun the following first:\n\n  cd {_ELECTRON_DIR}\n  npm install\n",
        file=sys.stderr,
    )
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="supreme-claudemander terminal canvas")
    parser.add_argument("--version", action="version", version=f"supreme-claudemander {_get_version()}")
    parser.add_argument("--port", type=int, default=3000, help="Server port (default: 3000)")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    parser.add_argument("--electron", action="store_true", help="Launch in Electron shell instead of browser")
    parser.add_argument("--test-mode", action="store_true", help="Enable test puppeting API")
    parser.add_argument("--config-dir", help="Override config directory (default: ~/.supreme-claudemander)")
    parser.add_argument(
        "--dev-config",
        nargs="?",
        const="default",
        default=None,
        metavar="PRESET",
        help="Wipe and rebuild an isolated dev config dir; optionally specify a preset name (default: 'default')",
    )
    args = parser.parse_args()

    import os

    # Build AppConfig early, before anything reads config from disk
    if args.dev_config is not None:
        from .dev_config import setup_dev_config

        dev_dir = setup_dev_config(preset=args.dev_config)
        app_config = config.load(dev_dir)
        logger.info("Dev config active: {} (preset={})", dev_dir, args.dev_config)
    elif args.config_dir:
        app_config = config.load(pathlib.Path(args.config_dir).resolve())
    else:
        app_config = config.load()

    # Configure loguru: remove default handler, add our own
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level="DEBUG",
    )
    logger.add(
        "supreme-claudemander.log",
        rotation="10 MB",
        retention="3 days",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
    )

    logger.info("supreme-claudemander starting on http://localhost:{}", args.port)

    test_mode = args.test_mode or os.environ.get("CLAUDE_RTS_TEST_MODE", "").lower() in ("1", "true")
    app = create_app(app_config, test_mode=test_mode)
    if test_mode:
        logger.info("Test mode enabled — puppeting API available")

    # --- Frontend launch strategy ---
    electron_proc = None

    if args.electron:
        _check_electron_installed()
        electron_exe = _ELECTRON_DIR / "node_modules" / "electron" / "dist" / "electron.exe"
        if not electron_exe.exists():
            electron_exe = _ELECTRON_DIR / "node_modules" / "electron" / "dist" / "electron"

        async def launch_electron(app):
            nonlocal electron_proc
            logger.info("Launching Electron shell (port {})", args.port)
            # Strip ELECTRON_RUN_AS_NODE — VS Code / Claude Code set it,
            # which forces electron.exe to act as plain Node.js.
            env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
            electron_proc = subprocess.Popen(
                [str(electron_exe), ".", "--port", str(args.port)],
                cwd=str(_ELECTRON_DIR),
                env=env,
            )

        app.on_startup.append(launch_electron)

        async def shutdown_electron(app):
            if electron_proc and electron_proc.poll() is None:
                logger.info("Terminating Electron shell")
                electron_proc.terminate()

        app.on_cleanup.append(shutdown_electron)

    elif not args.no_browser:

        async def open_browser(app):
            url = f"http://localhost:{args.port}"
            logger.info("Opening browser: {}", url)
            webbrowser.open(url)

        app.on_startup.append(open_browser)

    logger.info("Press Ctrl+C to stop")
    web.run_app(app, host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
