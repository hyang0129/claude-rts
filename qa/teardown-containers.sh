#!/usr/bin/env bash
# Remove QA test containers

docker rm -f rts-test-a rts-test-b 2>/dev/null || true
echo "QA containers removed."
