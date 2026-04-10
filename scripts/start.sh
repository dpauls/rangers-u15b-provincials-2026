#!/bin/bash
# Start the live tracker directly with podman (bypasses podman-compose for cleaner Ctrl+C)
set -e
cd "$(dirname "$0")/.."

# Clean up any previous container
podman rm -f rangers-tracker 2>/dev/null || true

exec podman run --rm -it \
  --name rangers-tracker \
  --init \
  --stop-timeout 3 \
  --env-file .env \
  -e TZ=${TZ:-America/Toronto} \
  -v ~/.gitconfig:/root/.gitconfig:ro \
  -v ~/.git-credentials:/root/.git-credentials:ro \
  -v "$(pwd)/.git:/app/.git" \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/docs:/app/docs" \
  -v "$(pwd)/logs:/app/logs" \
  rangers-u15b-provincials-2026_tracker \
  --interval 60
