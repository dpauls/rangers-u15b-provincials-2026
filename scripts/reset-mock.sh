#!/bin/bash
# Reset mock test state to the beginning
set -e
cd "$(dirname "$0")/.."

echo "Resetting mock test state..."
rm -f test/mock_responses/.mock_step
git checkout -- data/tournament.json
echo "Done. Next 'podman-compose run mock-test' will start from step 0."
