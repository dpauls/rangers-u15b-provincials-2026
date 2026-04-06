#!/usr/bin/env python3
"""
RAMP JSON API client for OWHA Provincials data.

Fetches standings and game data from the RAMP platform's REST API.
No HTML scraping -- uses the same JSON endpoints the website calls via AJAX.
"""
import json
import logging
import ssl
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

# RAMP API configuration
BASE_URL = 'https://owhaprovincials.msa4.rampinteractive.com'
LEAGUE_ID = 2910
SEASON_2026 = 13788
SEASON_2025 = 11769  # for testing
DIVISION_U15B_2026 = 16870
DIVISION_U15B_2025 = 16862  # for testing

# Game type IDs
GT_ROUND_ROBIN = 5383
GT_FINALS = 5384
GT_BRONZE = 5428

# SSL context (cert is expired)
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

USER_AGENT = 'KanataRangersTracker/1.0'


# The RAMP site has 2 backend servers behind DNS round-robin.
# One (Microsoft-HTTPAPI/2.0) is misconfigured and always returns 404.
# The other (Microsoft-IIS/10.0) works correctly.
# We retry with backoff to handle hitting the broken server.
# With ~50% per-attempt success, 10 attempts gives 99.9% reliability.
MAX_RETRIES = 10
RETRY_DELAYS = [0.5, 0.5, 0.5, 1, 1, 1, 1, 2, 2, 2]  # backoff schedule


def _fetch_json(url):
    """Fetch JSON from a URL, with retry logic for the flaky RAMP load balancer.

    Retries up to MAX_RETRIES times on 404 (broken backend server).
    Logs which server was hit for monitoring.
    Total worst-case time: ~14 seconds (well within poll interval).
    """
    import time

    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        try:
            resp = urllib.request.urlopen(req, context=_ssl_ctx, timeout=15)
            data = json.loads(resp.read().decode('utf-8'))
            server = resp.headers.get('Server', 'unknown')
            if attempt > 0:
                log.info(f'Succeeded on attempt {attempt + 1} (server: {server})')
            else:
                log.debug(f'Fetched {url} (server: {server})')
            return data
        except urllib.error.HTTPError as e:
            server = e.headers.get('Server', 'unknown') if hasattr(e, 'headers') else 'unknown'
            is_broken_backend = (e.code == 404 and 'HTTPAPI' in server)
            is_real_404 = (e.code == 404 and 'IIS' in server)

            if is_real_404:
                # 404 from the working server means the resource genuinely doesn't exist
                log.warning(f'Real 404 from IIS: {url}')
                return None

            if is_broken_backend and attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAYS[attempt]
                log.debug(f'Hit broken backend (attempt {attempt + 1}/{MAX_RETRIES}), retry in {delay}s')
                time.sleep(delay)
                continue

            if e.code == 404:
                log.warning(f'404 after {attempt + 1} attempts (server: {server}): {url}')
                return None

            log.error(f'HTTP {e.code} for {url}')
            raise
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAYS[attempt]
                log.debug(f'Error on attempt {attempt + 1}: {e}, retry in {delay}s')
                time.sleep(delay)
                continue
            log.error(f'Failed after {MAX_RETRIES} attempts: {url}: {e}')
            raise


def fetch_games(season_id=SEASON_2026, division_id=DIVISION_U15B_2026, game_type=GT_ROUND_ROBIN):
    """Fetch all games for a division.

    Returns list of game dicts with keys including:
        gameNumber, HomeTeamName, AwayTeamName, homeScore, awayScore,
        completed, liveScores, sDateString, sDate, ArenaName,
        homePIM, awayPIM, homeTID, awayTID, GID
    """
    url = f'{BASE_URL}/api/leaguegame/get/{LEAGUE_ID}/{season_id}/0/{division_id}/{game_type}/0/0'
    data = _fetch_json(url)
    if data is None:
        return []
    return data


def fetch_standings(season_id=SEASON_2026, division_id=DIVISION_U15B_2026):
    """Fetch pool-structured standings for a division.

    Returns list of dicts. Pool headers have SID=0 and SubDivName="POOL X".
    Team rows have all stats: TeamName, GamesPlayed, Wins, Losses, Ties,
    Points, GF, GA, PIM, PlusMinus, etc.
    """
    url = f'{BASE_URL}/api/leaguegame/getstandings3wsdcached/{LEAGUE_ID}/{season_id}/0/0/{division_id}/0'
    data = _fetch_json(url)
    if data is None:
        return []
    return data


def fetch_bracket_games(season_id=SEASON_2026, division_id=DIVISION_U15B_2026):
    """Fetch bracket games (finals + bronze)."""
    finals = fetch_games(season_id, division_id, GT_FINALS) or []
    bronze = fetch_games(season_id, division_id, GT_BRONZE) or []
    return finals + bronze


def parse_team_id_from_name(team_name):
    """Extract team number from name like 'Kanata Rangers #1755' or 'Kanata Rangers #1755 (3)'.

    Returns the number as a string, e.g., '1755'.
    """
    import re
    m = re.search(r'#(\d+)', team_name or '')
    return m.group(1) if m else None


def parse_score_from_name(team_name):
    """Extract in-game score appended to team name like 'Kanata Rangers #1755 (3)'.

    Returns int score or None.
    """
    import re
    m = re.search(r'\((\d+)\)\s*$', team_name or '')
    return int(m.group(1)) if m else None


def games_to_tournament_format(games, team_id_map):
    """Convert RAMP API game data to our tournament.json format.

    Args:
        games: list of game dicts from fetch_games()
        team_id_map: dict mapping OWHA team number (str) to our short ID, e.g. {'1755': 'KAN'}

    Returns list of game dicts in our format.
    """
    result = []
    for g in games:
        home_num = parse_team_id_from_name(g.get('HomeTeamName'))
        away_num = parse_team_id_from_name(g.get('AwayTeamName'))

        home_id = team_id_map.get(home_num)
        away_id = team_id_map.get(away_num)

        if not home_id or not away_id:
            continue  # skip games not involving our tracked teams

        # Determine status
        if g.get('completed'):
            status = 'final'
        elif g.get('homeScore') is not None and g.get('awayScore') is not None:
            status = 'in_progress'
        else:
            status = 'scheduled'

        result.append({
            'game_id': g.get('gameNumber'),
            'gid': g.get('GID'),
            'date': g.get('sDate'),
            'date_str': g.get('sDateString'),
            'arena': g.get('ArenaName'),
            'home': home_id,
            'away': away_id,
            'home_score': g.get('homeScore'),
            'away_score': g.get('awayScore'),
            'home_pim': g.get('homePIM'),
            'away_pim': g.get('awayPIM'),
            'status': status,
            'completed': g.get('completed', False),
            'live_scores': g.get('liveScores', False),
        })

    result.sort(key=lambda g: g.get('date') or '')
    return result


def standings_to_pool_map(standings):
    """Convert RAMP standings to a pool -> teams structure.

    Returns dict like:
        {'C': [{'name': 'Kanata Rangers', 'number': '1755', ...}, ...]}
    """
    pools = {}
    current_pool = None
    for entry in standings:
        if entry.get('SID') == 0:
            current_pool = entry.get('SubDivName', '').replace('POOL ', '')
            pools[current_pool] = []
        elif current_pool:
            num = parse_team_id_from_name(entry.get('TeamName'))
            pools[current_pool].append({
                'name': entry.get('TeamName', '').split('#')[0].strip(),
                'number': num,
                'gp': entry.get('GamesPlayed', 0),
                'w': entry.get('Wins', 0),
                'l': entry.get('Losses', 0),
                't': entry.get('Ties', 0),
                'pts': entry.get('Points', 0),
                'gf': entry.get('GF', 0),
                'ga': entry.get('GA', 0),
                'pim': entry.get('PIM', 0),
                'plus_minus': entry.get('PlusMinus', 0),
            })
    return pools


def detect_changes(prev_games, curr_games):
    """Compare two game lists and detect changes.

    Returns list of change dicts:
        {'type': 'score_change'|'game_final'|'game_started'|'correction',
         'game_id': ..., 'prev': ..., 'curr': ...}
    """
    prev_map = {g['game_id']: g for g in prev_games}
    changes = []

    for g in curr_games:
        gid = g['game_id']
        prev = prev_map.get(gid)

        if not prev:
            continue

        # Game started (was scheduled, now has scores)
        if prev['status'] == 'scheduled' and g['status'] in ('in_progress', 'final'):
            changes.append({
                'type': 'game_started',
                'game_id': gid,
                'prev': prev,
                'curr': g,
            })

        # Score change during game
        elif prev['status'] == 'in_progress' and g['status'] == 'in_progress':
            if prev['home_score'] != g['home_score'] or prev['away_score'] != g['away_score']:
                changes.append({
                    'type': 'score_change',
                    'game_id': gid,
                    'prev': prev,
                    'curr': g,
                })

        # Game completed
        elif prev['status'] != 'final' and g['status'] == 'final':
            changes.append({
                'type': 'game_final',
                'game_id': gid,
                'prev': prev,
                'curr': g,
            })

        # Post-hoc correction (was final, score changed)
        elif prev['status'] == 'final' and g['status'] == 'final':
            if prev['home_score'] != g['home_score'] or prev['away_score'] != g['away_score']:
                changes.append({
                    'type': 'correction',
                    'game_id': gid,
                    'prev': prev,
                    'curr': g,
                })

    return changes


class MockDataSource:
    """Reads API responses from local JSON files for testing."""

    def __init__(self, mock_dir):
        self.mock_dir = Path(mock_dir)
        self.step = 0
        self._files = sorted(self.mock_dir.glob('step_*.json'))
        log.info(f'MockDataSource: {len(self._files)} steps in {mock_dir}')

    def fetch_games(self):
        if self.step >= len(self._files):
            log.info('MockDataSource: no more steps, returning last')
            return json.loads(self._files[-1].read_text())
        data = json.loads(self._files[self.step].read_text())
        log.info(f'MockDataSource: step {self.step} -> {self._files[self.step].name}')
        self.step += 1
        return data


# ── CLI for testing ─────────────────────────────────────────────

if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO)

    cmd = sys.argv[1] if len(sys.argv) > 1 else 'games'
    season = SEASON_2025 if '--2025' in sys.argv else SEASON_2026
    division = DIVISION_U15B_2025 if '--2025' in sys.argv else DIVISION_U15B_2026

    if cmd == 'games':
        games = fetch_games(season, division)
        print(f'{len(games)} games')
        for g in games[:5]:
            print(f'  #{g["gameNumber"]}: {g["HomeTeamName"]} {g["homeScore"]}-{g["awayScore"]} {g["AwayTeamName"]} completed={g["completed"]}')

    elif cmd == 'standings':
        standings = fetch_standings(season, division)
        pools = standings_to_pool_map(standings)
        for pool_name, teams in pools.items():
            print(f'\nPool {pool_name}:')
            for t in teams:
                print(f'  {t["name"]} #{t["number"]} GP={t["gp"]} W={t["w"]} L={t["l"]} T={t["t"]} PTS={t["pts"]} GF={t["gf"]} GA={t["ga"]}')

    elif cmd == 'bracket':
        games = fetch_bracket_games(season, division)
        print(f'{len(games)} bracket games')
        for g in games:
            print(f'  #{g["gameNumber"]}: {g["HomeTeamName"]} {g["homeScore"]}-{g["awayScore"]} {g["AwayTeamName"]}')
