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
    generate_pregame_talking_points, generate_don_cherry,
    generate_event_impact,
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


def compute_bench_analysis(tournament_data, our_team, our_pool):
    """Compute goalie-pull analysis for bench.html.

    Runs what-if projections for our current game's three outcomes
    (win/tie/loss), factoring in concurrent game state.
    Returns data for bench.html to render.
    """
    from analyze import what_if_projection, get_remaining_games
    teams = tournament_data['teams']
    games = tournament_data['pool_games']

    # Find our in-progress game
    our_game = None
    other_live = None
    for g in games:
        if g.get('pool') != our_pool or g.get('status') != 'in_progress':
            continue
        if g['home'] == our_team or g['away'] == our_team:
            our_game = g
        else:
            other_live = g

    if not our_game:
        return None

    we_are_home = our_game['home'] == our_team
    our_score = (our_game.get('home_score') or 0) if we_are_home else (our_game.get('away_score') or 0)
    their_score = (our_game.get('away_score') or 0) if we_are_home else (our_game.get('home_score') or 0)
    opp_id = our_game['away'] if we_are_home else our_game['home']
    opp_name = teams.get(opp_id, {}).get('name', opp_id)

    # What-if: assume our game ends as win, tie, or loss
    # Use current score +1 for winner to simulate a realistic final
    results = {}
    for outcome, label in [(0, 'win'), (1, 'tie'), (2, 'loss')]:
        if outcome == 0:  # we win
            hs = max(our_score, their_score) + 1 if we_are_home else their_score
            as_ = their_score if we_are_home else max(our_score, their_score) + 1
            if we_are_home:
                hs = max(our_score + 1, their_score + 1)
                as_ = their_score
            else:
                hs = their_score
                as_ = max(our_score + 1, their_score + 1)
        elif outcome == 1:  # tie
            tied = max(our_score, their_score)
            hs = tied
            as_ = tied
        else:  # we lose
            if we_are_home:
                hs = our_score
                as_ = max(our_score + 1, their_score + 1)
            else:
                hs = max(our_score + 1, their_score + 1)
                as_ = our_score

        assumed = [{'game_id': our_game['game_id'], 'home_score': hs, 'away_score': as_}]

        # Also assume concurrent game holds at current score if in progress
        if other_live and other_live.get('home_score') is not None:
            assumed.append({
                'game_id': other_live['game_id'],
                'home_score': other_live.get('home_score', 0),
                'away_score': other_live.get('away_score', 0),
            })

        try:
            proj = what_if_projection(our_pool, tournament_data, assumed)
            adv_count = proj['counts'].get(our_team, 0)
            det = proj['total'] - proj.get('unresolved_count', 0)
            results[label] = {
                'advance': adv_count,
                'total': det,
                'advance_any': adv_count > 0,  # can we advance at all?
            }
        except Exception as e:
            log.error(f'Bench projection error ({label}): {e}')
            results[label] = {'advance': 0, 'total': 0, 'advance_any': False}

    # Determine the indicator color
    tie_advances = results['tie']['advance_any']
    tie_count = results['tie']['advance']
    tie_total = results['tie']['total']
    win_count = results['win']['advance']

    # Factor in concurrent game
    other_info = None
    if other_live:
        oh = other_live.get('home_score', 0) or 0
        oa = other_live.get('away_score', 0) or 0
        other_home_name = teams.get(other_live['home'], {}).get('name', other_live['home'])
        other_away_name = teams.get(other_live['away'], {}).get('name', other_live['away'])
        margin = abs(oh - oa)
        other_info = {
            'home': other_home_name, 'away': other_away_name,
            'home_score': oh, 'away_score': oa,
            'margin': margin,
        }

    if not tie_advances:
        indicator = 'red'
        reason = 'A tie eliminates us regardless of other results. We must win.'
    elif tie_total > 0 and tie_count == tie_total:
        indicator = 'green'
        reason = 'A tie guarantees we advance. No need to take risks.'
    elif tie_total > 0 and tie_count / tie_total > 0.7:
        indicator = 'green'
        reason = f'A tie advances us in {tie_count} of {tie_total} remaining scenarios. Safe to play it out.'
    else:
        # Yellow: this is the nuanced case. Get LLM commentary.
        indicator = 'yellow'

        # Build rich context for LLM
        from generate import build_standings
        our_analysis = enumerate_scenarios(our_pool, tournament_data)
        standings = build_standings(our_pool, tournament_data, our_analysis)
        standings_summary = ', '.join(
            f"{s['name'].split('#')[0].strip()} {s['pts']}pts ({s['w']}W-{s['l']}L-{s['t']}T, GD{'+' if s['gd']>0 else ''}{s['gd']})"
            for s in standings
        )

        # Tiebreaker state: current GD, GA for teams near us
        tb_lines = []
        for s in standings:
            tb_lines.append(f"  {s['name'].split('#')[0].strip()}: GD={'+' if s['gd']>0 else ''}{s['gd']}, GA={s['ga']}")
        tiebreaker_state = '\n'.join(tb_lines)

        from narrative import generate_bench_commentary
        llm_reason = generate_bench_commentary(
            teams[our_team]['name'], our_score, their_score, opp_name,
            results, other_info, standings_summary, tiebreaker_state)

        if llm_reason:
            reason = llm_reason
        elif tie_total > 0 and tie_count / tie_total > 0.3:
            reason = f'A tie advances us in {tie_count} of {tie_total} scenarios. It depends on other results.'
        else:
            reason = (f'A tie only advances us in {tie_count} of {tie_total} scenarios. '
                     f'A win is much better ({win_count} of {results["win"]["total"]}). Consider pulling late.')

    return {
        'our_game': {
            'home': our_game['home'], 'away': our_game['away'],
            'home_name': teams.get(our_game['home'], {}).get('name', our_game['home']),
            'away_name': teams.get(our_game['away'], {}).get('name', our_game['away']),
            'home_score': our_game.get('home_score', 0),
            'away_score': our_game.get('away_score', 0),
        },
        'other_game': other_info,
        'projections': results,
        'indicator': indicator,
        'reason': reason,
    }


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
            is_our_game = (g['home'] == our_team or g['away'] == our_team)
            headline = f"GAME STARTED: {home_name} vs {away_name} (Game #{g['game_id']})"

            detail = None
            if not skip_narrative:
                detail = generate_event_impact(
                    'game_started', home_name, away_name, hs, as_,
                    is_our_game, our_team_name)
            events.append(create_event('info', headline, detail))

        elif change['type'] == 'score_change':
            hs = g.get('home_score', 0)
            as_ = g.get('away_score', 0)
            is_our_game = (g['home'] == our_team or g['away'] == our_team)
            headline = f"Score update: {home_name} {hs} - {as_} {away_name} (in progress)"

            # Run what-if projection for scenario impact
            from analyze import what_if_projection
            holds_count = None
            holds_total = None
            try:
                if_holds = what_if_projection(our_pool, tournament_data,
                    [{'game_id': g['game_id'], 'home_score': hs, 'away_score': as_}])
                holds_count = if_holds['counts'].get(our_team, 0)
                holds_total = if_holds['total'] - if_holds.get('unresolved_count', 0)
            except Exception:
                pass

            detail = None
            if not skip_narrative:
                detail = generate_event_impact(
                    'score_change', home_name, away_name, hs, as_,
                    is_our_game, our_team_name, holds_count, holds_total)

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
            live = build_games_list(our_pool, tournament_data, 'in_progress')

            # Summarize recent changes for context
            recent_change_descs = [e['headline'] for e in events]

            narrative = generate_overall_narrative_with_context(
                prev_narrative, standings, scenario_data,
                tournament_data['teams'][our_team]['name'], our_pool,
                qf_standings, completed, upcoming, recent_change_descs, live)

    # Store narrative in tournament data for generate.py to pick up
    if narrative:
        tournament_data['_narrative'] = narrative
        DATA_PATH.write_text(json.dumps(tournament_data, indent=2))

    # Generate state.json
    generate(str(DATA_PATH), skip_narrative=True)

    # Generate Coach's Corner content -- only when the "for_game" changes
    coaches_corner = tournament_data.get('_coaches_corner', {})
    has_any_final = any(c['type'] == 'game_final' for c in changes)

    if not skip_narrative:
        our_team_name = tournament_data['teams'][our_team]['name']
        from generate import build_games_list, build_standings

        # Determine what game Coach's Corner should be about:
        # If we have an in-progress game, it's about that game
        # If we have upcoming games, it's about the next one
        # If no games left, no update
        live = build_games_list(our_pool, tournament_data, 'in_progress')
        upcoming = build_games_list(our_pool, tournament_data, 'scheduled')
        our_live = [g for g in live if g['home'] == our_team or g['away'] == our_team]
        our_upcoming = [g for g in upcoming if g['home'] == our_team or g['away'] == our_team]

        # Determine which opponent Coach's Corner should be about
        # Use just the opponent name (no status suffix) so in-progress
        # doesn't trigger a re-generation for the same opponent
        if our_live:
            g0 = our_live[0]
            current_for_game = g0['away_name'] if g0['home'] == our_team else g0['home_name']
        elif our_upcoming:
            g0 = our_upcoming[0]
            current_for_game = g0['away_name'] if g0['home'] == our_team else g0['home_name']
        else:
            current_for_game = None

        prev_for_game = coaches_corner.get('for_game')
        should_regen_corner = (
            current_for_game is not None and
            current_for_game != prev_for_game and
            (has_any_final or not prev_for_game)  # regen on finals or if no content yet
        )

        if should_regen_corner:
            log.info(f'Coach\'s Corner: regenerating (was "{prev_for_game}", now "{current_for_game}")')
            coaches_corner['for_game'] = current_for_game

            # Don Cherry
            recent_results = [e['headline'] for e in tournament_data.get('event_log', []) if e['type'] == 'final']
            cherry_context = f"Tournament state: {', '.join(recent_results[-3:])}. Next: {current_for_game}."
            don_cherry = generate_don_cherry(cherry_context, our_team_name)
            if don_cherry:
                coaches_corner['don_cherry'] = don_cherry

            # Pre-game talking points for the relevant game
            target_game = our_live[0] if our_live else (our_upcoming[0] if our_upcoming else None)
            if target_game:
                opp_id = target_game['away'] if target_game['home'] == our_team else target_game['home']
                opp_name = tournament_data['teams'].get(opp_id, {}).get('name', opp_id)
                opp_ranking = tournament_data['teams'].get(opp_id, {}).get('ranking', '?')
                completed = build_games_list(our_pool, tournament_data, 'final')
                # Build results with most recent first and win/loss indicator
                our_completed = [g for g in completed
                                if g['home'] == our_team or g['away'] == our_team]
                our_results = []
                for i, g in enumerate(our_completed):
                    we_home = g['home'] == our_team
                    our_goals = g['home_score'] if we_home else g['away_score']
                    their_goals = g['away_score'] if we_home else g['home_score']
                    if our_goals > their_goals:
                        result_tag = 'WIN'
                    elif their_goals > our_goals:
                        result_tag = 'LOSS'
                    else:
                        result_tag = 'TIE'
                    recency = '(MOST RECENT)' if i == 0 else ''
                    our_results.append(
                        f"{g['home_name']} {g['home_score']}-{g['away_score']} {g['away_name']} [{result_tag}] {recency}".strip()
                    )

                our_analysis = enumerate_scenarios(our_pool, tournament_data)
                standings = build_standings(our_pool, tournament_data, our_analysis)
                our_standing = next((s for s in standings if s['id'] == our_team), None)
                our_pim = our_standing.get('pim', 0) if our_standing else 0

                our_count = our_analysis['counts'].get(our_team, 0)
                det = our_analysis['total'] - our_analysis.get('unresolved_count', 0)
                stake = f"We win the pool in {our_count} of {det} deterministic scenarios."

                talking_points = generate_pregame_talking_points(
                    our_team_name, opp_name, opp_ranking,
                    our_results, our_pim, stake, stake,
                    'Only pool winners advance to the quarterfinal.')
                if talking_points:
                    coaches_corner['talking_points'] = talking_points

        tournament_data['_coaches_corner'] = coaches_corner
        DATA_PATH.write_text(json.dumps(tournament_data, indent=2))

    # Compute bench analysis for goalie-pull decisions
    bench = compute_bench_analysis(tournament_data, our_team, our_pool)

    # Inject narrative, coaches corner, and bench into state.json
    state = json.loads(STATE_PATH.read_text())
    if narrative or tournament_data.get('_narrative'):
        state['narrative'] = narrative or tournament_data.get('_narrative')
    if coaches_corner:
        state['coaches_corner'] = coaches_corner
    if bench:
        state['bench'] = bench
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
    parser.add_argument('--skip-push', action='store_true', help='Skip git push (default for mock testing)')
    parser.add_argument('--push', action='store_true', help='Force git push (overrides --skip-push)')
    parser.add_argument('--interval', type=int, help='Override poll interval (seconds)')
    parser.add_argument('--data', default=None, help='Path to tournament.json')
    args = parser.parse_args()

    # --push overrides --skip-push
    if args.push:
        args.skip_push = False

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

    # Generate welcome narrative + Coach's Corner if no events yet and narrative enabled
    if not args.skip_narrative and not tournament_data.get('event_log'):
        our_team = tournament_data['tournament']['our_team']
        our_team_name = tournament_data['teams'][our_team]['name']
        our_pool = tournament_data['teams'][our_team]['pool']
        our_analysis = enumerate_scenarios(our_pool, tournament_data)
        from generate import build_standings, build_scenario_data, build_games_list
        standings = build_standings(our_pool, tournament_data, our_analysis)
        scenario_data = build_scenario_data(our_analysis, our_team)
        qf_standings = build_standings('F', tournament_data, enumerate_scenarios('F', tournament_data))
        upcoming = build_games_list(our_pool, tournament_data, 'scheduled')

        state = json.loads(STATE_PATH.read_text())

        # Welcome narrative
        log.info('Generating welcome narrative...')
        welcome = generate_overall_narrative(
            standings, scenario_data, our_team_name,
            our_pool, qf_standings, [], upcoming, [])
        if welcome:
            state['narrative'] = welcome
            tournament_data['_narrative'] = welcome

        # Initial Coach's Corner
        log.info('Generating initial Coach\'s Corner...')
        coaches_corner = {}

        # Don Cherry for the opening
        cherry = generate_don_cherry(
            f"Tournament is about to start. {our_team_name} is ranked #18 in the province. "
            f"First game is against #25 Ennismore Eagles. Pool also has #3 Kincardine (tough!) "
            f"and #41 Windsor. Only the pool winner advances to the quarterfinal.",
            our_team_name)
        if cherry:
            coaches_corner['don_cherry'] = cherry

        # Pre-game talking points for first game
        our_upcoming = [g for g in upcoming if g['home'] == our_team or g['away'] == our_team]
        if our_upcoming:
            next_game = our_upcoming[0]
            opp_id = next_game['away'] if next_game['home'] == our_team else next_game['home']
            opp_name = tournament_data['teams'].get(opp_id, {}).get('name', opp_id)
            opp_ranking = tournament_data['teams'].get(opp_id, {}).get('ranking', '?')
            talking = generate_pregame_talking_points(
                our_team_name, opp_name, opp_ranking,
                [], 0, 'First game of the tournament.',
                'No games played yet.', 'Every game matters -- only the pool winner advances.')
            if talking:
                coaches_corner['talking_points'] = talking

        if coaches_corner:
            state['coaches_corner'] = coaches_corner
            tournament_data['_coaches_corner'] = coaches_corner

        STATE_PATH.write_text(json.dumps(state, indent=2))
        DATA_PATH.write_text(json.dumps(tournament_data, indent=2))

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
