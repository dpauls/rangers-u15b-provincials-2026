#!/bin/bash
# Start the live tracker directly with podman (bypasses podman-compose for cleaner Ctrl+C)
set -e
cd "$(dirname "$0")/.."

# Load env vars
source .env

# Clean up any previous container
podman rm -f rangers-tracker 2>/dev/null || true

exec podman run --rm -it \
  --name rangers-tracker \
  --init \
  --stop-timeout 3 \
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  -e TZ="${TZ:-America/Toronto}" \
  -e GIT_AUTHOR_NAME="${GIT_USER_NAME:-Rangers Tracker Bot}" \
  -e GIT_AUTHOR_EMAIL="${GIT_USER_EMAIL:-tracker@example.com}" \
  -e GIT_COMMITTER_NAME="${GIT_USER_NAME:-Rangers Tracker Bot}" \
  -e GIT_COMMITTER_EMAIL="${GIT_USER_EMAIL:-tracker@example.com}" \
  -v ~/.gitconfig:/root/.gitconfig:ro \
  -v ~/.git-credentials:/root/.git-credentials:ro \
  -v "$(pwd)/.git:/app/.git" \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/docs:/app/docs" \
  -v "$(pwd)/logs:/app/logs" \
  rangers-u15b-provincials-2026_tracker \
  --interval 60
