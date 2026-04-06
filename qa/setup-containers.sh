#!/usr/bin/env bash
# QA test containers for supreme-claudemander
# Container A: rts-test-a — tmux installed (persistence expected)
# Container B: rts-test-b — no tmux (fallback, warning expected)
# These containers do NOT carry devcontainer labels, so they won't appear in hub discovery.

set -e

echo "=== Stopping and removing old test containers (if any) ==="
docker rm -f rts-test-a rts-test-b 2>/dev/null || true

echo "=== Starting rts-test-a (with tmux) ==="
docker run -d --name rts-test-a ubuntu:22.04 \
  bash -c "apt-get update -qq && apt-get install -y -qq tmux && tail -f /dev/null"

echo "=== Starting rts-test-b (without tmux) ==="
docker run -d --name rts-test-b ubuntu:22.04 \
  bash -c "tail -f /dev/null"

echo ""
echo "Waiting for rts-test-a apt install to finish..."
timeout 60 bash -c 'until docker exec rts-test-a which tmux 2>/dev/null; do sleep 2; done' \
  && echo "  tmux ready in rts-test-a" \
  || echo "  WARNING: tmux install may still be in progress"

echo ""
echo "=== Containers ready ==="
docker ps --filter name=rts-test-a --filter name=rts-test-b --format "table {{.Names}}\t{{.Status}}"
echo ""
echo "Next: copy qa/config.qa.json -> ~/.supreme-claudemander/config.json"
echo "      install the m6-qa canvas via the API or copy qa/canvas.m6-qa.json -> ~/.supreme-claudemander/canvases/m6-qa.json"
echo "      python -m claude_rts --port 3001 --no-browser"
