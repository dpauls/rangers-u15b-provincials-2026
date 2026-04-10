#!/bin/bash
# Re-roll: clear AI-generated content so the daemon regenerates it on next startup.
# Use when you don't like the current narrative, coach's corner, or Don Cherry.
set -e
cd "$(dirname "$0")/.."

echo "Clearing AI-generated content..."

# Clear from shadow file if it exists
for f in data/.tournament_live.json data/tournament.json; do
    if [ -f "$f" ]; then
        python3 -c "
import json
data = json.load(open('$f'))
for key in ['_narrative', '_coaches_corner', '_tb_health']:
    data.pop(key, None)
json.dump(data, open('$f', 'w'), indent=2)
"
        echo "  Cleared $f"
    fi
done

echo "Done. Restart the daemon to generate fresh content."
