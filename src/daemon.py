#!/usr/bin/env python3
"""
Main daemon: polls RAMP API, detects changes, runs analysis, generates site, pushes to GitHub.

Usage:
    python3 src/daemon.py                          # Live mode (polls RAMP API)
    python3 src/daemon.py --mock-dir test/mock_responses  # Test with mock data
    python3 src/daemon.py --once                   # Single poll cycle, then exit
"""
import argparse
import json
import logging
import subprocess
import sys
import time
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from scraper import (
    fetch_games, fetch_standings, fetch_bracket_games,
    games_to_tournament_format, standings_to_pool_map,
    detect_changes, MockDataSource,
    SEASON_2026, DIVISION_U15B_2026, GT_ROUND_ROBIN,
)
from analyze import load_tournament, enumerate_scenarios, compute_standings, compute_h2h
from generate import generate
from narrative import (
    generate_overall_narrative, generate_overall_narrative_with_context,
    generate_game_final_comment, generate_in_game_comment,
    generate_correction_comment, evaluate_narrative,
)

log = logging.getLogger(__name__)

# Paths
PROJECT_ROOT = Path(__file__).parent.parent
DATA_PATH = PROJECT_ROOT / 'data' / 'tournament.json'
STATE_PATH = PROJECT_ROOT / 'docs' / 'data' / 'state.json'


def _set_data_path(path):
    global DATA_PATH
    DATA_PATH = path

# Team number -> short ID mapping (built from tournament.json)
TEAM_MAP = {}

# Polling intervals (seconds)
POLL_IDLE = 900       # 15 min
POLL_PREGAME = 180    # 3 min
POLL_INGAME = 90      # 90 sec
POLL_POSTGAME = 120   # 2 min
POLL_BETWEEN = 300    # 5 min

# How many minutes before game start to enter pre-game mode
PREGAME_WINDOW = 20


def load_team_map():
    """Build team number -> short ID mapping from tournament.json."""
    global TEAM_MAP
    data = load_tournament(str(DATA_PATH))
    TEAM_MAP = {str(data['teams'][tid]['number']): tid for tid in data['teams']}
    return data


_last_final_time = None  # Track when last game went final for post-game window


def get_poll_interval(tournament_data):
    """Determine poll interval based on game schedule and current state.

    States: IDLE → PRE-GAME → IN-GAME → POST-GAME → BETWEEN → IDLE
    """
    global _last_final_time
    now = datetime.now()
    games = tournament_data.get('pool_games', [])

    # Check for in-progress games (highest priority)
    for g in games:
        if g.get('status') == 'in_progress':
            log.debug(f'Poll: IN-GAME ({POLL_INGAME}s)')
            return POLL_INGAME

    # Post-game window: 10 minutes after last final
    if _last_final_time and (now - _last_final_time).total_seconds() < 600:
        log.debug(f'Poll: POST-GAME ({POLL_POSTGAME}s)')
        return POLL_POSTGAME

    # Check proximity to next scheduled game
    next_game_minutes = None
    for g in games:
        if g.get('status') != 'scheduled':
            continue
        try:
            game_time = datetime.fromisoformat(g['date'])
            minutes_until = (game_time - now).total_seconds() / 60
            if minutes_until > 0:
                if next_game_minutes is None or minutes_until < next_game_minutes:
                    next_game_minutes = minutes_until
        except (ValueError, TypeError):
            continue

    if next_game_minutes is not None:
        if next_game_minutes <= PREGAME_WINDOW:
            log.debug(f'Poll: PRE-GAME ({POLL_PREGAME}s, {next_game_minutes:.0f}min to game)')
            return POLL_PREGAME
        if next_game_minutes <= 60:
            log.debug(f'Poll: BETWEEN ({POLL_BETWEEN}s, {next_game_minutes:.0f}min to game)')
            return POLL_BETWEEN

    log.debug(f'Poll: IDLE ({POLL_IDLE}s)')
    return POLL_IDLE


def mark_game_final():
    """Call when a game goes final to start the post-game polling window."""
    global _last_final_time
    _last_final_time = datetime.now()


def update_tournament_data(tournament_data, api_games, pool_id):
    """Update tournament.json pool_games with data from API response.

    Returns list of detected changes.
    """
    # Convert API games to our format, filtering to tracked teams
    new_games = games_to_tournament_format(api_games, TEAM_MAP)

    # Build lookup of current games
    current_games = [g for g in tournament_data['pool_games'] if g.get('pool') == pool_id]

    # Detect changes
    changes = detect_changes(current_games, new_games)

    # Apply updates to tournament_data
    game_map = {g['game_id']: g for g in new_games}
    for i, g in enumerate(tournament_data['pool_games']):
        if g.get('pool') == pool_id and g['game_id'] in game_map:
            updated = game_map[g['game_id']]
            tournament_data['pool_games'][i]['home_score'] = updated['home_score']
            tournament_data['pool_games'][i]['away_score'] = updated['away_score']
            tournament_data['pool_games'][i]['status'] = updated['status']

    return changes


def create_event(event_type, headline, detail=None):
    """Create an event log entry."""
    return {
        'time': datetime.now().strftime('%a %b %d, %I:%M %p'),
        'type': event_type,
        'headline': headline,
        'detail': detail,
    }


def process_changes(changes, tournament_data, prev_scenarios, skip_narrative=False):
    """Process detected changes: generate events and narrative."""
    our_team = tournament_data['tournament']['our_team']
    our_team_name = tournament_data['teams'][our_team]['name']
    our_pool = tournament_data['teams'][our_team]['pool']
    teams = tournament_data['teams']
    events = []

    # Run analysis with updated data
    curr_analysis = enumerate_scenarios(our_pool, tournament_data)
    curr_scenarios = {
        'our_count': curr_analysis['counts'].get(our_team, 0),
        'total': curr_analysis['total'],
    }

    # Build standings for narrative
    from generate import build_standings
    standings = build_standings(our_pool, tournament_data, curr_analysis)

    for change in changes:
        g = change['curr']
        home_name = teams.get(g['home'], {}).get('name', g['home'])
        away_name = teams.get(g['away'], {}).get('name', g['away'])
        game_info = {**g, 'home_name': home_name, 'away_name': away_name}

        if change['type'] == 'game_started':
            hs = g.get('home_score', 0) or 0
            as_ = g.get('away_score', 0) or 0
            headline = f"Game #{g['game_id']} underway: {home_name} {hs} - {as_} {away_name}"
            events.append(create_event('goal', headline))

        elif change['type'] == 'score_change':
            hs = g.get('home_score', 0)
            as_ = g.get('away_score', 0)
            headline = f"Score update: {home_name} {hs} - {as_} {away_name} (in progress)"

            detail = None
            if not skip_narrative and g['home'] != our_team and g['away'] != our_team:
                # Generate rooting interest for non-Kanata game
                # Run what-if for current score holding vs reversal
                from analyze import what_if_projection
                if_holds = what_if_projection(our_pool, tournament_data,
                    [{'game_id': g['game_id'], 'home_score': hs, 'away_score': as_}])
                holds_count = if_holds['counts'].get(our_team, 0)

                # Rough reversal: flip who's winning
                if hs != as_:
                    rev_hs, rev_as = (as_, hs)  # simple swap
                    if_rev = what_if_projection(our_pool, tournament_data,
                        [{'game_id': g['game_id'], 'home_score': rev_hs, 'away_score': rev_as}])
                    rev_count = if_rev['counts'].get(our_team, 0)
                else:
                    rev_count = holds_count

                detail = generate_in_game_comment(
                    game_info, our_team, our_team_name,
                    holds_count, rev_count, if_holds['total'])

            events.append(create_event('goal', headline, detail))

        elif change['type'] == 'game_final':
            mark_game_final()
            headline = (f"FINAL: {home_name} {g['home_score']} - "
                       f"{g['away_score']} {away_name}")

            detail = None
            if not skip_narrative:
                detail = generate_game_final_comment(
                    game_info, standings, prev_scenarios, curr_scenarios,
                    our_team, our_team_name, our_pool, teams)

            events.append(create_event('final', headline, detail))

        elif change['type'] == 'correction':
            prev_g = change['prev']
            old_score = (prev_g['home_score'], prev_g['away_score'])
            new_score = (g['home_score'], g['away_score'])
            headline = generate_correction_comment(game_info, old_score, new_score, our_team_name)
            events.append(create_event('info', headline, 'Standings and scenarios have been recalculated.'))

    return events, curr_scenarios


def git_push():
    """Commit and push changes to GitHub."""
    try:
        subprocess.run(
            ['git', 'add', 'docs/data/state.json', 'data/tournament.json'],
            cwd=str(PROJECT_ROOT), check=True, capture_output=True)

        # Check if there are actually changes to commit
        result = subprocess.run(
            ['git', 'diff', '--cached', '--quiet'],
            cwd=str(PROJECT_ROOT), capture_output=True)
        if result.returncode == 0:
            log.debug('No changes to commit')
            return True

        timestamp = datetime.now().strftime('%b %d %I:%M %p')
        subprocess.run(
            ['git', 'commit', '-m', f'Update: {timestamp}'],
            cwd=str(PROJECT_ROOT), check=True, capture_output=True)
        subprocess.run(
            ['git', 'push'],
            cwd=str(PROJECT_ROOT), check=True, capture_output=True, timeout=30)
        log.info('Pushed to GitHub')
        return True
    except subprocess.CalledProcessError as e:
        log.error(f'Git error: {e.stderr.decode() if e.stderr else e}')
        return False
    except subprocess.TimeoutExpired:
        log.error('Git push timed out')
        return False


def run_cycle(tournament_data, mock_source=None, skip_narrative=False, skip_push=False):
    """Run one poll-analyze-generate-push cycle.

    Returns (changed: bool, tournament_data, prev_scenarios).
    """
    our_team = tournament_data['tournament']['our_team']
    our_pool = tournament_data['teams'][our_team]['pool']

    # Fetch latest data
    if mock_source:
        api_games = mock_source.fetch_games()
    else:
        api_games = fetch_games()

    if not api_games:
        log.warning('No game data from API, skipping cycle')
        return False, tournament_data, None

    # Save previous scenarios for comparison
    prev_analysis = enumerate_scenarios(our_pool, tournament_data)
    prev_scenarios = {
        'our_count': prev_analysis['counts'].get(our_team, 0),
        'total': prev_analysis['total'],
    }

    # Detect and apply changes
    changes = update_tournament_data(tournament_data, api_games, 'C')
    # Also update Pool F
    pool_f_changes = update_tournament_data(tournament_data, api_games, 'F')

    all_changes = changes + pool_f_changes

    if not all_changes:
        log.debug('No changes detected')
        return False, tournament_data, prev_scenarios

    log.info(f'Detected {len(all_changes)} change(s)')

    # Process changes (generate events, narrative)
    events, curr_scenarios = process_changes(
        changes, tournament_data, prev_scenarios, skip_narrative)

    # Add events to log
    tournament_data.setdefault('event_log', []).extend(events)

    # Save updated tournament data
    DATA_PATH.write_text(json.dumps(tournament_data, indent=2))

    # Decide whether to regenerate the narrative
    narrative = None
    if not skip_narrative:
        prev_narrative = tournament_data.get('_narrative')
        should_regen = evaluate_narrative(
            prev_narrative, changes, prev_scenarios, curr_scenarios,
            our_team, tournament_data['teams'][our_team]['name'])

        if should_regen:
            from generate import build_standings, build_scenario_data, build_games_list
            our_analysis = enumerate_scenarios(our_pool, tournament_data)
            standings = build_standings(our_pool, tournament_data, our_analysis)
            scenario_data = build_scenario_data(our_analysis, our_team)
            qf_standings = build_standings('F', tournament_data,
                                           enumerate_scenarios('F', tournament_data))
            completed = build_games_list(our_pool, tournament_data, 'final')
            upcoming = build_games_list(our_pool, tournament_data, 'scheduled')

            # Summarize recent changes for context
            recent_change_descs = [e['headline'] for e in events]

            narrative = generate_overall_narrative_with_context(
                prev_narrative, standings, scenario_data,
                tournament_data['teams'][our_team]['name'], our_pool,
                qf_standings, completed, upcoming, recent_change_descs)

    # Store narrative in tournament data for generate.py to pick up
    if narrative:
        tournament_data['_narrative'] = narrative
        DATA_PATH.write_text(json.dumps(tournament_data, indent=2))

    # Generate state.json
    generate(str(DATA_PATH), skip_narrative=True)

    # Inject narrative into state.json if we have one
    if narrative or tournament_data.get('_narrative'):
        state = json.loads(STATE_PATH.read_text())
        state['narrative'] = narrative or tournament_data.get('_narrative')
        STATE_PATH.write_text(json.dumps(state, indent=2))

    # Push to GitHub
    if not skip_push:
        git_push()

    return True, tournament_data, curr_scenarios


def main():
    parser = argparse.ArgumentParser(description='Kanata Rangers tournament tracker daemon')
    parser.add_argument('--mock-dir', help='Use mock data from directory instead of live API')
    parser.add_argument('--mock-reset', action='store_true', help='Reset mock data to step 0')
    parser.add_argument('--once', action='store_true', help='Run one cycle and exit')
    parser.add_argument('--skip-narrative', action='store_true', help='Skip Claude API calls')
    parser.add_argument('--skip-push', action='store_true', help='Skip git push')
    parser.add_argument('--interval', type=int, help='Override poll interval (seconds)')
    parser.add_argument('--data', default=None, help='Path to tournament.json')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )

    _set_data_path(Path(args.data) if args.data else DATA_PATH)
    tournament_data = load_team_map()

    mock_source = None
    if args.mock_dir:
        mock_source = MockDataSource(args.mock_dir)
        if args.mock_reset:
            mock_source.reset()
            # Clear event log and reset all game scores to clean state
            tournament_data['event_log'] = []
            tournament_data.pop('_narrative', None)
            for g in tournament_data.get('pool_games', []):
                g['home_score'] = None
                g['away_score'] = None
                g['status'] = 'scheduled'
            DATA_PATH.write_text(json.dumps(tournament_data, indent=2))
            tournament_data = load_team_map()
            log.info('Mock data reset to step 0 (scores cleared, events cleared)')

    prev_scenarios = None
    log.info(f'Daemon starting. Mock: {args.mock_dir or "OFF"}, Narrative: {not args.skip_narrative}, Push: {not args.skip_push}')

    # Always generate and push current state on startup
    log.info('Initial generate and push...')
    generate(str(DATA_PATH), skip_narrative=True)

    # Generate welcome narrative if no events yet and narrative enabled
    if not args.skip_narrative and not tournament_data.get('event_log'):
        log.info('Generating welcome narrative...')
        our_team = tournament_data['tournament']['our_team']
        our_pool = tournament_data['teams'][our_team]['pool']
        our_analysis = enumerate_scenarios(our_pool, tournament_data)
        from generate import build_standings, build_scenario_data, build_games_list
        standings = build_standings(our_pool, tournament_data, our_analysis)
        scenario_data = build_scenario_data(our_analysis, our_team)
        qf_standings = build_standings('F', tournament_data, enumerate_scenarios('F', tournament_data))
        upcoming = build_games_list(our_pool, tournament_data, 'scheduled')
        welcome = generate_overall_narrative(
            standings, scenario_data, tournament_data['teams'][our_team]['name'],
            our_pool, qf_standings, [], upcoming)
        if welcome:
            state = json.loads(STATE_PATH.read_text())
            state['narrative'] = welcome
            STATE_PATH.write_text(json.dumps(state, indent=2))

    if not args.skip_push:
        git_push()

    while True:
        try:
            changed, tournament_data, prev_scenarios = run_cycle(
                tournament_data, mock_source, args.skip_narrative, args.skip_push)

            if changed:
                log.info('Cycle complete: changes detected and published')
            else:
                log.debug('Cycle complete: no changes')

        except KeyboardInterrupt:
            log.info('Shutting down')
            break
        except Exception as e:
            log.error(f'Cycle error: {e}', exc_info=True)

        if args.once:
            break

        interval = args.interval or get_poll_interval(tournament_data)
        log.info(f'Next poll in {interval}s')
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            log.info('Shutting down')
            break


if __name__ == '__main__':
    main()
