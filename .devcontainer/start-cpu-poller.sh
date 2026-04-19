#!/usr/bin/env bash
# Starts cpu-poller.sh as a background daemon, killing any prior instance.
# Safe to re-run on every container start.
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
POLLER="$SCRIPT_DIR/cpu-poller.sh"
PIDFILE="/tmp/container-cpu.pid"

if [ -f "$PIDFILE" ]; then
  old=$(cat "$PIDFILE" 2>/dev/null || echo "")
  if [ -n "$old" ] && [ -d "/proc/$old" ]; then
    kill "$old" 2>/dev/null || true
    sleep 1
    [ -d "/proc/$old" ] && kill -9 "$old" 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
fi

chmod +x "$POLLER" 2>/dev/null || true
nohup "$POLLER" >/dev/null 2>&1 &
echo $! > "$PIDFILE"
disown 2>/dev/null || true
