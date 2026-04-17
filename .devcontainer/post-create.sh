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

# Fix Docker socket permissions (socket from Windows host is root:root, not root:docker)
if [ -e /var/run/docker.sock ]; then
  sudo chmod 666 /var/run/docker.sock
fi

# Fix claude-profiles volume permissions (created by Docker as root:root)
if [ -d /profiles ]; then
  sudo chmod 777 /profiles
fi

pip install -e ".[test,e2e]"
python -m playwright install chromium
