#!/bin/bash
set -e
cd "$(dirname "$0")/.."
python3 src/generate.py "$@"
git add docs/ data/
git commit -m "Update: $(date '+%b %d %H:%M')"
git push
echo "Site updated. Live at https://dpauls.github.io/rangers-u15b-provincials-2026/"
