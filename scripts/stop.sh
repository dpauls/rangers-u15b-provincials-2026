#!/bin/bash
# Stop all tracker containers and clean up
set -e
cd "$(dirname "$0")/.."

echo "Stopping all containers..."
podman kill --all 2>/dev/null || true
podman rm --all 2>/dev/null || true

# Clean up any stale git lock
rm -f .git/index.lock

echo "Done. Verify with: podman ps"
