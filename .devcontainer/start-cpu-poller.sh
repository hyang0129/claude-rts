#!/usr/bin/env bash
# Starts cpu-poller.sh as a background daemon, killing any prior instance.
# Safe to re-run on every container start.
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
POLLER="$SCRIPT_DIR/cpu-poller.sh"
PIDFILE="/tmp/container-cpu.pid"
LOG_DIR="/workspaces/claude-rts/.cpu-logs"

mkdir -p "$LOG_DIR"

# Kill prior poller instance
if [ -f "$PIDFILE" ]; then
  old=$(cat "$PIDFILE" 2>/dev/null || echo "")
  if [ -n "$old" ] && [ -d "/proc/$old" ]; then
    kill "$old" 2>/dev/null || true
    sleep 1
    [ -d "/proc/$old" ] && kill -9 "$old" 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
fi

# Prune oldest logs so that after this new one is created we have at most 5
mapfile -t old_logs < <(ls -t "$LOG_DIR"/container-cpu-*.log 2>/dev/null | tail -n +5)
[ "${#old_logs[@]}" -gt 0 ] && rm -f "${old_logs[@]}"

LOG="$LOG_DIR/container-cpu-$(date -u +%Y%m%d-%H%M).log"

chmod +x "$POLLER" 2>/dev/null || true
CPU_POLLER_LOG="$LOG" CPU_POLLER_INTERVAL=1 nohup "$POLLER" >/dev/null 2>&1 &
echo $! > "$PIDFILE"
disown 2>/dev/null || true
