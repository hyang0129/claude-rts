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
    parser = argparse.ArgumentParser(
        description="supreme-claudemander terminal canvas",
        # Allow 'qa next' subcommand without breaking bare 'python -m claude_rts'
    )
    parser.add_argument("--version", action="version", version=f"supreme-claudemander {_get_version()}")

    subparsers = parser.add_subparsers(dest="subcommand")

    # ── 'qa' subcommand ─────────────────────────────────────────────────────
    qa_parser = subparsers.add_parser("qa", help="QA scenario runner commands")
    qa_subparsers = qa_parser.add_subparsers(dest="qa_action")

    qa_run_parser = qa_subparsers.add_parser(
        "run",
        help=(
            "Drive a named QA scenario to the gate state and capture a screenshot. "
            "Headless by default; set HEADED=1 to watch via noVNC. "
            "Use 'qa verdict <id> <verdict>' afterward to post the verdict to GitHub."
        ),
    )
    qa_run_parser.add_argument("scenario_id", help="Scenario ID to run (see: qa list)")

    qa_subparsers.add_parser(
        "list",
        help="List all available QA scenario IDs and their linked debt issue numbers.",
    )

    qa_verdict_parser = qa_subparsers.add_parser(
        "verdict",
        help=(
            "Post a verdict comment to the linked GitHub debt issue. "
            "Run 'qa run <id>' first to drive the app to the gate state and capture a screenshot, "
            "then call this command after assessing the screenshot."
        ),
    )
    qa_verdict_parser.add_argument("scenario_id", help="Scenario ID (see: qa list)")
    qa_verdict_parser.add_argument(
        "verdict",
        choices=["pass", "fail", "inconclusive", "blocked"],
        help="Verdict to record",
    )
    qa_verdict_parser.add_argument(
        "--notes",
        default="",
        metavar="TEXT",
        help="Optional free-form notes appended to the verdict comment",
    )

    # ── Server arguments (only apply when no subcommand is given) ───────────
    parser.add_argument("--port", type=int, default=3000, help="Server port (default: 3000)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
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
    parser.add_argument(
        "--migrate-canvases",
        action="store_true",
        help=(
            "Run the one-shot canvas JSON migration (epic #236 child 5) and exit. "
            "Writes a {name}.json.pre-236-backup sidecar before rewriting each "
            "old-schema file. Refuses to re-run when a sidecar already exists."
        ),
    )
    args = parser.parse_args()

    # ── Dispatch qa subcommand before any server setup ───────────────────────
    if args.subcommand == "qa":
        if args.qa_action == "run":
            from .qa_runner import run_scenario

            run_scenario(args.scenario_id)
            return
        elif args.qa_action == "list":
            from .qa_runner import list_scenarios

            list_scenarios()
            return
        elif args.qa_action == "verdict":
            from .qa_runner import post_verdict

            post_verdict(args.scenario_id, args.verdict, notes=args.notes)
            return
        else:
            qa_parser.print_help()
            sys.exit(1)

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

    # Configure stderr handler early so --migrate-canvases output uses the
    # same format as the running server. The file handler is added below,
    # AFTER the migrate-canvases short-circuit, because the migration is a
    # one-shot and shouldn't write to the production log.
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level="DEBUG",
    )

    if args.migrate_canvases:
        from .migrations import canvas_236

        config.ensure_dirs(app_config)
        summary = canvas_236.migrate_canvas_dir(app_config.canvases_dir)
        logger.info(
            "canvas migration complete: migrated={} skipped={} errors={}",
            len(summary["migrated"]),
            len(summary["skipped"]),
            len(summary["errors"]),
        )
        for path in summary["migrated"]:
            logger.info("  migrated: {}", path)
        for path, msg in summary["errors"]:
            logger.error("  error: {} — {}", path, msg)
        sys.exit(1 if summary["errors"] else 0)

    # File log handler (sidecar to the stderr handler above) for the running
    # server. Add AFTER the migrate-canvases short-circuit so a migration run
    # doesn't append to the production log file.
    logger.add(
        "supreme-claudemander.log",
        rotation="10 MB",
        retention="3 days",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
    )

    logger.info("supreme-claudemander starting on http://{}:{}", args.host, args.port)

    test_mode = args.test_mode or os.environ.get("CLAUDE_RTS_TEST_MODE", "").lower() in ("1", "true")
    app = create_app(app_config, test_mode=test_mode)
    if test_mode:
        logger.info("Test mode enabled — puppeting API available")

    # Epic #236 child 5 (#241): dev-config canvases are spawn-hint fixtures
    # (no ``card_id`` per entry — they are not server snapshots). The startup
    # canvas-schema check would falsely flag them as pre-epic. Skip the
    # check in dev-config mode; the preset copy is wiped+rebuilt every boot
    # so it cannot drift into a corrupt state.
    if args.dev_config is not None:
        app["_skip_canvas_schema_check"] = True

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
            # Browser URL uses 'localhost' when binding to wildcard 0.0.0.0 (macOS
            # cannot open http://0.0.0.0); otherwise use the explicit host.
            browser_host = "localhost" if args.host == "0.0.0.0" else args.host
            url = f"http://{browser_host}:{args.port}"
            logger.info("Opening browser: {}", url)
            webbrowser.open(url)

        app.on_startup.append(open_browser)

    logger.info("Press Ctrl+C to stop")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
