#!/usr/bin/env python3
"""Fail if any uncapped 'docker run' call exists in claude_rts/ or tests/.

Looks for list-form docker run patterns like ["docker", "run", ...] or
'docker', 'run' that don't carry --cpus within the same call block.
The docker_run() wrapper in tests/conftest.py is excluded because it
defines the caps themselves.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

# Matches a line containing the literal tokens "docker" and "run" in sequence
# (exec-form list syntax). Catches `["docker", "run", ...]` style.
DOCKER_RUN_LINE = re.compile(r"""["']docker["'][^"']*["']run["']""")


def check_file(path: Path) -> list[str]:
    violations = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        # Skip blank lines and pure comments
        if not stripped or stripped.startswith("#"):
            continue
        # Skip the docker_run wrapper definition and its internal list
        # construction — that function IS the safe wrapper with caps.
        if "docker_run" in line:
            continue
        if not DOCKER_RUN_LINE.search(line):
            continue
        # Found a docker run pattern — check for --cpus within ±15 lines
        start = max(0, lineno - 5)
        end = min(len(lines), lineno + 15)
        context = "\n".join(lines[start:end])
        if "--cpus" not in context:
            rel = path.relative_to(ROOT)
            violations.append(f"{rel}:{lineno}: {stripped!r}")
    return violations


def main() -> int:
    targets = [*ROOT.glob("claude_rts/**/*.py"), *ROOT.glob("tests/**/*.py")]
    all_violations: list[str] = []
    for path in sorted(targets):
        if path.name == "check_docker_caps.py":
            continue
        all_violations.extend(check_file(path))

    if all_violations:
        print("FAIL: uncapped docker run calls found in claude_rts/ or tests/:")
        for v in all_violations:
            print(f"  {v}")
        return 1

    print("OK: all docker run calls carry resource caps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
