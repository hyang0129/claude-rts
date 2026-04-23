#!/usr/bin/env bash
# Background poller: logs per-container CPU/mem every INTERVAL seconds to LOG.
# Designed to survive when the aiohttp server is stuck — uses only docker CLI.
set -u

LOG="${CPU_POLLER_LOG:-/workspaces/claude-rts/.cpu-logs/container-cpu-$(date -u +%Y%m%d-%H%M).log}"
INTERVAL="${CPU_POLLER_INTERVAL:-1}"
RETAIN_SECONDS=900  # 15 minutes

while true; do
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemPerc}},{{.MemUsage}}' 2>/dev/null \
    | while IFS= read -r line; do
        printf '%s,%s\n' "$ts" "$line" >> "$LOG"
      done

  # Trim entries older than 15 minutes (ISO 8601 sorts lexicographically)
  if [ -f "$LOG" ]; then
    cutoff=$(date -u -d "${RETAIN_SECONDS} seconds ago" +%Y-%m-%dT%H:%M:%SZ)
    awk -v cutoff="$cutoff" -F',' '$1 >= cutoff' "$LOG" > "${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"
  fi

  sleep "$INTERVAL"
done
