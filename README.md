# Kanata Rangers U15B Provincials 2026

Live-updating tournament tracker for the Kanata Rangers at the OWHA U15B Provincial Championships (April 10-12, 2026, Chesswood Arenas, Toronto).

**Live site**: https://dpauls.github.io/rangers-u15b-provincials-2026/

## What It Does

- Polls the OWHA tournament API for score updates every 90 seconds during games
- Runs scenario analysis (all possible remaining outcomes) with tiebreaker resolution
- Generates AI commentary via Claude API
- Pushes updates to GitHub Pages so parents can follow on their phones

## Quick Start (Docker)

```bash
# 1. Clone the repo
git clone https://github.com/dpauls/rangers-u15b-provincials-2026.git
cd rangers-u15b-provincials-2026

# 2. Create .env file with your API key
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY

# 3. Make sure git credentials are configured
#    (the container needs to push to GitHub)
git config --global credential.helper store
# Do a test push to cache credentials:
# git commit --allow-empty -m "test" && git push && git reset HEAD~1

# 4. Run the daemon
docker compose up -d tracker

# 5. Check logs
docker compose logs -f tracker
```

## Test with Mock Data

Simulates a full tournament using 2025 provincial data mapped onto 2026 teams:

```bash
# Run one mock cycle (processes next step in the sequence)
docker compose run mock-test

# Or run without Docker:
python3 src/daemon.py --mock-dir test/mock_responses --once --skip-push
```

## Manual Update (without daemon)

```bash
# Edit data/tournament.json with new scores, then:
python3 src/generate.py --skip-narrative
# Or with narrative:
python3 src/generate.py

# Push to GitHub Pages:
./scripts/update.sh
```

## Architecture

```
RAMP API  -->  daemon.py  -->  analyze.py  -->  narrative.py  -->  state.json  -->  GitHub Pages
(poll)         (detect)        (scenarios)      (Claude AI)       (git push)       (parents view)
```

- `src/daemon.py` - Main polling loop with schedule-aware intervals
- `src/scraper.py` - RAMP JSON API client with retry logic
- `src/analyze.py` - Scenario enumeration engine with tiebreaker resolution
- `src/narrative.py` - Claude API for generating tournament commentary
- `src/generate.py` - Produces `docs/data/state.json` from tournament data
- `docs/index.html` - Static page that renders JSON client-side
- `docs/scenarios.html` - Detailed scenario table with color coding

## Project Structure

```
data/tournament.json     - Tournament config + game data (source of truth)
docs/index.html          - Main page (static, renders from JSON)
docs/scenarios.html      - Scenario detail page
docs/data/state.json     - Generated data file (updated by daemon)
src/                     - Python backend
test/mock_responses/     - Mock API responses for testing
test/fixtures/           - Cached real API data for development
```
