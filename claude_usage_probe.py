"""Check Claude Max/Pro usage limits programmatically on Linux.

Works by running the `/usage` command inside an interactive Claude Code
session via a virtual terminal (pexpect + pyte). This is the only reliable
method — there is no public API endpoint for usage data.

Usage as a library:
    from claude_usage import get_usage, get_usage_multi

    # Single account
    usage = get_usage()                             # default ~/.claude
    usage = get_usage("/home/me/.claude-work")      # specific profile

    # Multiple accounts in parallel
    results = get_usage_multi([
        "/home/me/.claude-profiles/acct-a/.claude",
        "/home/me/.claude-profiles/acct-b/.claude",
    ])
    for dir, usage in results.items():
        print(f"{dir}: 5hr={usage.five_hour_pct}%")

Usage as a CLI:
    claude-usage                                    # default
    claude-usage --claude-dir ~/.claude-work        # specific profile
    claude-usage --claude-dir /path/a --claude-dir /path/b  # parallel
    claude-usage --json                             # JSON output
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Optional

import logging
import logging.handlers

import pexpect
import pyte

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30

_file_logger: logging.Logger | None = None


def _setup_file_logger(claude_dir: str | None) -> None:
    """Set up a rotating file logger inside claude_dir. Called once per get_usage()."""
    global _file_logger
    log_dir = claude_dir if claude_dir else os.path.expanduser("~/.claude")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "claude-usage.log")
    handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    fl = logging.getLogger(f"claude_usage._file.{log_path}")
    fl.setLevel(logging.DEBUG)
    fl.handlers = [handler]
    fl.propagate = False
    _file_logger = fl


def _log(msg: str) -> None:
    logger.debug(msg)
    if _file_logger:
        _file_logger.debug(msg)


@dataclass
class Usage:
    five_hour_pct: Optional[float] = None
    five_hour_resets: Optional[str] = None
    seven_day_pct: Optional[float] = None
    seven_day_resets: Optional[str] = None
    sonnet_week_pct: Optional[float] = None
    sonnet_week_resets: Optional[str] = None
    raw_output: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self, include_raw: bool = False) -> dict:
        d = asdict(self)
        if not include_raw:
            d.pop("raw_output", None)
        return d


def _find_claude() -> str:
    """Find the claude binary via env var, PATH, or well-known locations.

    Resolution order:
    1. ``CLAUDE_BIN`` environment variable (explicit override)
    2. ``claude`` on ``PATH`` (via :func:`shutil.which`)
    3. Well-known install locations (pip/pipx user installs, npm global,
       VS Code extension directories)
    """
    # 1. Environment variable override
    env_bin = os.environ.get("CLAUDE_BIN")
    if env_bin:
        if os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
            return env_bin
        raise FileNotFoundError(f"CLAUDE_BIN is set to {env_bin!r} but it is not an executable file.")

    # 2. PATH lookup
    if shutil.which("claude"):
        return "claude"

    # 3. Well-known locations
    for pattern in [
        os.path.expanduser("~/.local/bin/claude"),
        "/usr/local/bin/claude",
        os.path.expanduser("~/.vscode-server/extensions/anthropic.claude-code-*/resources/native-binary/claude"),
        os.path.expanduser("~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude"),
    ]:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[-1]
    raise FileNotFoundError("claude not found. Install Claude Code, add it to PATH, or set CLAUDE_BIN.")


def _parse_screen(text: str) -> Usage:
    """Extract usage percentages from the /usage screen output."""
    usage = Usage(raw_output=text)
    lines = text.split("\n")

    for i, line in enumerate(lines):
        m = re.search(r"(\d+)%\s*used", line)
        if not m:
            continue
        pct = float(m.group(1))

        # Look backward for context
        ctx = " ".join(lines[max(0, i - 3) : i]).lower()

        # Look forward for reset time
        resets = None
        for j in range(i, min(len(lines), i + 3)):
            rm = re.search(r"[Rr]eset[s ]+(.*?)$", lines[j])
            if rm:
                resets = rm.group(1).strip()
                break

        if "session" in ctx and usage.five_hour_pct is None:
            usage.five_hour_pct = pct
            usage.five_hour_resets = resets
        elif "sonnet" in ctx:
            usage.sonnet_week_pct = pct
            usage.sonnet_week_resets = resets
        elif "week" in ctx and usage.seven_day_pct is None:
            usage.seven_day_pct = pct
            usage.seven_day_resets = resets

    if usage.five_hour_pct is None and usage.seven_day_pct is None and usage.sonnet_week_pct is None:
        logger.warning("No usage data parsed from screen output (%d lines)", len(lines))
        logger.debug("Raw screen output:\n%s", text)

    return usage


def _is_login_screen(t: str) -> bool:
    """Return True if the screen is showing the claude login/auth prompt."""
    return "Select login method" in t or "Claude account with subscription" in t


def get_usage(
    claude_dir: Optional[str] = None,
    claude_bin: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Usage:
    """Get current Claude usage by running /usage in a virtual terminal.

    Args:
        claude_dir: Path to the .claude config directory. Defaults to
                    ~/.claude (the standard location). Pass a different
                    path to check a specific profile/account.
        claude_bin: Explicit path to the ``claude`` binary. When provided,
                    auto-discovery via :func:`_find_claude` is skipped.
        timeout:    Max seconds to wait for claude to start and respond.

    Returns:
        A Usage dataclass with the parsed percentages and reset times.
    """
    _setup_file_logger(claude_dir)

    if claude_bin is None:
        claude_bin = _find_claude()

    _log(f"using claude binary: {claude_bin}")
    _log(f"claude_dir={claude_dir!r}, timeout={timeout}s")

    env = os.environ.copy()
    if claude_dir:
        env["CLAUDE_CONFIG_DIR"] = str(claude_dir)

    screen = pyte.Screen(120, 40)
    stream = pyte.Stream(screen)

    _log("spawning claude process...")
    child = pexpect.spawn(
        claude_bin,
        args=["--dangerously-skip-permissions"],
        encoding="utf-8",
        timeout=timeout,
        dimensions=(40, 120),
        env=env,
    )
    _log(f"spawned pid={child.pid}")

    def feed():
        try:
            data = child.read_nonblocking(size=4096, timeout=1)
            stream.feed(data)
        except (pexpect.TIMEOUT, pexpect.EOF):
            pass

    def text():
        return "\n".join(screen.display[i].rstrip() for i in range(screen.lines))

    try:
        # Wait for claude to be ready (bypass prompt or direct to input)
        deadline = time.time() + timeout
        accepted = False
        at_prompt = False
        last_screen_log = 0

        _log("waiting for claude to be ready...")
        while time.time() < deadline:
            feed()
            t = text()
            elapsed = time.time() - (deadline - timeout)

            # Log screen state every 5s
            if time.time() - last_screen_log >= 5:
                visible = [ln for ln in t.split("\n") if ln.strip()]
                _log(f"t={elapsed:.1f}s screen ({len(visible)} lines):")
                for line in visible[:10]:
                    _log(f"  | {line}")
                last_screen_log = time.time()

            # 1. Bail out early if claude is showing the login/auth screen
            if _is_login_screen(t):
                raise PermissionError(
                    "Not authenticated: claude is prompting for login. Run `claude` interactively to log in first."
                )

            # 2. Handle workspace trust prompt ("Yes, I trust this folder")
            if "Yes, I trust this folder" in t and not accepted:
                _log(f"t={elapsed:.1f}s: trust-folder dialog detected, accepting...")
                child.send("\r")  # first option is already selected: "Yes, I trust this folder"
                accepted = True
                time.sleep(2)
                continue

            # 3. Handle bypass-permissions prompt if it appears (must check BEFORE prompt detection)
            if ("Yes, I accept" in t or "Bypass Permissions" in t) and not accepted:
                _log(f"t={elapsed:.1f}s: bypass-permissions dialog detected, accepting...")
                child.send("\x1b[B")  # down arrow to "Yes, I accept"
                time.sleep(0.3)
                child.send("\r")
                accepted = True
                time.sleep(3)
                continue

            # 4. Check if we're at the welcome splash (reliable signal that claude is ready)
            if "Welcome back" in t or "Tips for getting started" in t:
                _log(f"t={elapsed:.1f}s: welcome splash detected — claude is ready, proceeding")
                at_prompt = True
                break

            time.sleep(0.5)

        if not at_prompt:
            t = text()
            visible = [ln for ln in t.split("\n") if ln.strip()]
            _log(f"TIMEOUT waiting for claude ready state. Final screen ({len(visible)} lines):")
            for line in visible:
                _log(f"  | {line}")
            raise TimeoutError("Timed out waiting for claude prompt")

        feed()
        elapsed = time.time() - (deadline - timeout)
        _log(f"t={elapsed:.1f}s: at prompt, sending /usage...")

        # Send /usage
        for ch in "/usage":
            child.send(ch)
            time.sleep(0.05)
        time.sleep(1)
        feed()
        child.send("\r")

        # Wait for results
        _log("waiting for /usage output...")
        for i in range(timeout):
            time.sleep(1)
            feed()
            t = text()
            visible = [ln for ln in t.split("\n") if ln.strip()]
            _log(f"  wait {i + 1}s: {len(visible)} non-empty lines, has_pct={'%' in t and 'used' in t}")
            if visible:
                for line in visible[:5]:
                    _log(f"    | {line}")
            if _is_login_screen(t):
                raise PermissionError(
                    "Not authenticated: claude is prompting for login. Run `claude` interactively to log in first."
                )
            if "%" in t and "used" in t:
                # Give it a moment to finish rendering
                time.sleep(1)
                feed()
                break
        else:
            t = text()
            visible = [ln for ln in t.split("\n") if ln.strip()]
            _log(f"TIMEOUT waiting for /usage output. Final screen ({len(visible)} lines):")
            for line in visible:
                _log(f"  | {line}")

        result = _parse_screen(text())
        _log(f"parsed: {result}")

        # Exit
        child.send("\x1b")
        time.sleep(0.3)
        child.sendline("/exit")
        time.sleep(0.5)

        return result

    finally:
        try:
            child.close(force=True)
        except Exception:
            pass


def get_usage_multi(
    claude_dirs: list[str],
    claude_bin: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_workers: int | None = None,
) -> dict[str, Usage | None]:
    """Get usage for multiple accounts in parallel.

    Each account is probed concurrently via its own pexpect/pyte session.
    Individual failures return None without blocking other probes.

    Args:
        claude_dirs:  List of .claude config directory paths.
        claude_bin:   Explicit path to the ``claude`` binary. Passed through
                      to each :func:`get_usage` call, bypassing auto-discovery.
        timeout:      Max seconds per probe (passed to get_usage).
        max_workers:  Thread pool size. Defaults to min(len(claude_dirs), 4).

    Returns:
        Dict mapping each config dir to its Usage (or None on failure).
    """
    if not claude_dirs:
        return {}

    if max_workers is None:
        max_workers = min(len(claude_dirs), 4)

    results: dict[str, Usage | None] = {}

    def _probe(claude_dir: str) -> tuple[str, Usage | None]:
        try:
            return claude_dir, get_usage(claude_dir=claude_dir, claude_bin=claude_bin, timeout=timeout)
        except Exception as e:
            logger.warning("Probe failed for %s: %s: %s", claude_dir, type(e).__name__, e)
            return claude_dir, None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_probe, d): d for d in claude_dirs}
        for future in as_completed(futures):
            claude_dir, usage = future.result()
            results[claude_dir] = usage

    return results


def _print_usage(usage: Usage, label: str | None = None) -> bool:
    """Pretty-print a single Usage result. Returns True if any data shown."""
    if label:
        print(f"\n--- {label} ---")
    shown = False
    if usage.five_hour_pct is not None:
        print(f"Session (5h):     {usage.five_hour_pct:>5.0f}% used", end="")
        if usage.five_hour_resets:
            print(f"  (resets {usage.five_hour_resets})", end="")
        print()
        shown = True
    if usage.seven_day_pct is not None:
        print(f"Week (all):       {usage.seven_day_pct:>5.0f}% used", end="")
        if usage.seven_day_resets:
            print(f"  (resets {usage.seven_day_resets})", end="")
        print()
        shown = True
    if usage.sonnet_week_pct is not None:
        print(f"Week (Sonnet):    {usage.sonnet_week_pct:>5.0f}% used", end="")
        if usage.sonnet_week_resets:
            print(f"  (resets {usage.sonnet_week_resets})", end="")
        print()
        shown = True
    return shown


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Check Claude Max/Pro usage limits")
    parser.add_argument(
        "--claude-dir",
        action="append",
        help="Path to .claude config directory (repeatable for multi-account)",
    )
    parser.add_argument(
        "--claude-bin",
        help="Explicit path to the claude binary (skips auto-discovery)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Output as JSON",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging (shows raw screen output on failure)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(name)s %(levelname)s: %(message)s",
    )

    dirs = args.claude_dir  # None or list of strings
    bin_path = args.claude_bin

    try:
        if dirs and len(dirs) > 1:
            # Multi-account parallel mode
            results = get_usage_multi(dirs, claude_bin=bin_path, timeout=args.timeout)
            if args.as_json:
                print(
                    json.dumps(
                        {d: u.to_dict() if u else None for d, u in results.items()},
                        indent=2,
                    )
                )
            else:
                any_data = False
                for d, usage in results.items():
                    if usage is None:
                        print(f"\n--- {d} ---")
                        print("  Error: failed to retrieve usage (use -v for details)")
                    else:
                        if _print_usage(usage, label=d):
                            any_data = True
                if not any_data:
                    print("No usage data found for any account.")
                    sys.exit(1)
        else:
            # Single-account mode (default or single --claude-dir)
            claude_dir = dirs[0] if dirs else None
            usage = get_usage(claude_dir=claude_dir, claude_bin=bin_path, timeout=args.timeout)

            if args.as_json:
                print(json.dumps(usage.to_dict(), indent=2))
            else:
                if not _print_usage(usage):
                    print("No usage data found. Is Claude Code installed and logged in?")
                    if args.verbose and usage.raw_output:
                        print(f"\nRaw screen output:\n{usage.raw_output}")
                    elif not args.verbose:
                        print("Run with -v for debug output.")
                    sys.exit(1)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except TimeoutError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except PermissionError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
