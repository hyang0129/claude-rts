#!/usr/bin/env bash
# Background poller: logs per-container CPU/mem every INTERVAL seconds to LOG.
# Designed to survive when the aiohttp server is stuck — uses only docker CLI.
set -u

LOG="${CPU_POLLER_LOG:-/tmp/container-cpu.log}"
INTERVAL="${CPU_POLLER_INTERVAL:-5}"
MAX_BYTES="${CPU_POLLER_MAX_BYTES:-52428800}"  # 50 MB, then rotate once

while true; do
  if [ -f "$LOG" ]; then
    size=$(stat -c%s "$LOG" 2>/dev/null || echo 0)
    if [ "$size" -gt "$MAX_BYTES" ]; then
      mv -f "$LOG" "$LOG.1"
    fi
  fi

  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemPerc}},{{.MemUsage}}' 2>/dev/null \
    | while IFS= read -r line; do
        printf '%s,%s\n' "$ts" "$line" >> "$LOG"
      done

  sleep "$INTERVAL"
done
