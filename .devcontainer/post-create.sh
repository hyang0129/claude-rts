#!/usr/bin/env bash
set -euo pipefail

# Apply git identity captured by initialize.cmd
GITUSER_TMP="$(dirname "$0")/.gituser.tmp"
if [ -f "$GITUSER_TMP" ]; then
  GIT_NAME=$(sed -n '1p' "$GITUSER_TMP")
  GIT_EMAIL=$(sed -n '2p' "$GITUSER_TMP")
  [ -n "$GIT_NAME" ]  && git config --global user.name  "$GIT_NAME"
  [ -n "$GIT_EMAIL" ] && git config --global user.email "$GIT_EMAIL"
  rm -f "$GITUSER_TMP"
fi

pip install -e ".[test,e2e]"
python -m playwright install chromium
