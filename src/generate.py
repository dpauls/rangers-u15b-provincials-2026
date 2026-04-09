#!/usr/bin/env python3
"""
Site generator: loads tournament data, runs analysis, outputs docs/data/state.json.
The static index.html fetches this JSON and renders everything client-side.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from analyze import (
    load_tournament,
    compute_standings,
    compute_h2h,
    enumerate_scenarios,
    determine_pool_winner,
    get_remaining_games,
)


def get_team_status(team_id, analysis, total):
    count = analysis['counts'].get(team_id, 0)
    if total == 0:
        return 'TBD'
    if count == total:
        return 'CLINCHED'
    if count == 0:
        return 'ELIMINATED'
    return 'ALIVE'


def build_standings(pool_id, data, analysis):
    teams = data['teams']
    games = data['pool_games']
    st = compute_standings(pool_id, teams, games)
    h2h = compute_h2h(pool_id, games)
    total = analysis['total'] if analysis else 1
    deterministic = total - analysis.get('unresolved_count', 0) if analysis else 1

    sorted_teams = sorted(st, key=lambda t: (st[t]['PTS'], st[t]['W'], st[t]['GF'] - st[t]['GA']), reverse=True)

    # Find groups of teams tied on points (>0) and resolve tiebreakers for display
    from analyze import resolve_tie
    tie_notes = {}  # tid -> note explaining why they're ranked here
    i = 0
    while i < len(sorted_teams):
        pts = st[sorted_teams[i]]['PTS']
        # Collect all teams at this point level
        group = []
        while i < len(sorted_teams) and st[sorted_teams[i]]['PTS'] == pts:
            group.append(sorted_teams[i])
            i += 1

        if len(group) <= 1 or pts == 0:
            continue  # No tie to explain, or all at 0 points

        # Run tiebreaker to get explanation
        adv, elim, lines, gd_dep = resolve_tie(group, st, h2h, len(group), indent='')
        if adv is None:
            # Unresolved
            for tid in group:
                tie_notes[tid] = 'Tied — needs rules v+ to resolve'
        elif lines:
            # Extract a concise explanation from the resolution lines
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                # Lines like "Rule i (wins): KAN (2W) > WIN (1W)"
                # or "Rule iii (GD): WIN (+7) > KAN,KIN (+1)"
                if line.startswith('Rule '):
                    for tid in group:
                        tie_notes[tid] = line
                    break

    rows = []
    for tid in sorted_teams:
        s = st[tid]
        gp = s['W'] + s['L'] + s['T']
        gd = s['GF'] - s['GA']
        count = analysis['counts'].get(tid, 0) if analysis else 0
        rows.append({
            'id': tid,
            'name': teams[tid]['name'],
            'ranking': teams[tid].get('ranking'),
            'is_us': teams[tid].get('is_us', False),
            'gp': gp, 'w': s['W'], 'l': s['L'], 't': s['T'],
            'pts': s['PTS'], 'gf': s['GF'], 'ga': s['GA'], 'gd': gd,
            'status': get_team_status(tid, analysis, total) if analysis else '',
            'adv_count': count,
            'adv_pct': round(count / deterministic * 100, 1) if deterministic > 0 else 0,
            'tie_note': tie_notes.get(tid),
        })
    return rows


def build_scenario_data(analysis, our_team):
    if not analysis or analysis['total'] <= 1:
        if analysis and analysis['total'] == 1:
            sc = analysis['scenarios'][0]
            winner = sc['result']['advancing'][0] if sc['result']['advancing'] else None
            return {
                'total': 1,
                'our_count': analysis['counts'].get(our_team, 0),
                'remaining_games': 0,
                'gd_dependent': 0,
                'unresolved': 0,
                'winner': winner,
                'scenarios': [],
            }
        return None

    total = analysis['total']
    our_count = analysis['counts'].get(our_team, 0)
    unresolved_count = analysis.get('unresolved_count', 0)

    # Build compact scenario list for JSON
    scenarios = []
    for sc in analysis['scenarios']:
        res = sc['result']
        is_unresolved = sc.get('unresolved', False)
        tb_info = []
        for tb in res['tb_details']:
            tb_info.append({
                'teams': tb['teams'],
                'pts': tb['pts'],
                'lines': tb['lines'],
                'gd_dep': tb['gd_dep'],
                'unresolved': tb.get('unresolved', False),
            })

        # Classify resolution type
        if is_unresolved:
            res_type = 'unresolved'
        elif not tb_info:
            res_type = 'clean'
        elif sc['gd_dependent']:
            res_type = 'score_dependent'
        else:
            res_type = 'tiebreaker'

        advancing = res['advancing'] or []
        eliminated = res['eliminated'] or []
        unresolved_teams = res.get('unresolved_teams', [])

        # For the winner's points, handle unresolved case
        if advancing:
            winner_pts = sc['standings'][advancing[0]]['PTS']
        elif unresolved_teams:
            winner_pts = sc['standings'][unresolved_teams[0]]['PTS']
        else:
            winner_pts = 0

        scenarios.append({
            'labels': sc['labels'],
            'advancing': advancing,
            'eliminated': eliminated,
            'unresolved_teams': unresolved_teams,
            'pts': winner_pts,
            'gd_dep': sc['gd_dependent'],
            'res_type': res_type,
            'tiebreakers': tb_info,
        })

    remaining = analysis['remaining_games']
    remaining_info = [{'home': g['home'], 'away': g['away'], 'game_id': g['game_id'], 'date': g['date']} for g in remaining]

    # Three categories for the bar:
    # GREEN = we win via rules i-ii only (deterministic, score-independent)
    # YELLOW = score-dependent (rules iii+) or truly unresolved
    # RED = another team wins via rules i-ii only
    green = 0  # deterministic IN
    yellow = 0  # unknown (score-dependent + unresolved)
    red = 0    # deterministic OUT
    for sc in scenarios:
        if sc['res_type'] == 'unresolved' or sc['res_type'] == 'score_dependent':
            yellow += 1
        elif sc['advancing'] and our_team in sc['advancing']:
            green += 1
        else:
            red += 1

    return {
        'total': total,
        'green': green,
        'yellow': yellow,
        'red': red,
        'our_count': our_count,  # all wins (incl score-dependent) for standings column
        'our_pct': round(green / (green + red) * 100, 1) if (green + red) > 0 else 0,
        'unresolved': unresolved_count,
        'gd_dependent': analysis['gd_dependent_count'],
        'remaining_games': len(remaining),
        'remaining_info': remaining_info,
        'counts': analysis['counts'],
        'scenarios': scenarios,
    }


def build_games_list(pool_id, data, status_filter=None):
    games = []
    for g in data['pool_games']:
        if g['pool'] != pool_id:
            continue
        if status_filter and g['status'] != status_filter:
            continue
        teams = data['teams']
        games.append({
            'game_id': g['game_id'],
            'date': g['date'],
            'home': g['home'],
            'away': g['away'],
            'home_name': teams[g['home']]['name'],
            'away_name': teams[g['away']]['name'],
            'home_score': g.get('home_score'),
            'away_score': g.get('away_score'),
            'status': g['status'],
        })
    games.sort(key=lambda g: g['date'], reverse=(status_filter == 'final'))
    return games


def build_bracket(data):
    bracket = []
    for b in data.get('bracket', []):
        entry = dict(b)
        if entry['home'] and entry['home'] in data['teams']:
            entry['home_name'] = data['teams'][entry['home']]['name']
        if entry['away'] and entry['away'] in data['teams']:
            entry['away_name'] = data['teams'][entry['away']]['name']
        bracket.append(entry)
    return bracket


def build_tiebreaker_resolution(pool_id, data, analysis):
    """Build human-readable tiebreaker resolution when all games are complete.

    Returns a list of resolution descriptions, or None if games remain.
    """
    remaining = get_remaining_games(pool_id, data['pool_games'])
    if remaining:
        return None  # Games still to play

    # With 0 remaining games, there's exactly 1 scenario
    if not analysis['scenarios']:
        return None

    sc = analysis['scenarios'][0]
    res = sc['result']
    if not res['tb_details']:
        return None  # No tiebreaker needed

    resolutions = []
    for tb in res['tb_details']:
        lines = [line.strip() for line in tb['lines'] if line.strip()]
        resolutions.append({
            'teams': tb['teams'],
            'pts': tb['pts'],
            'resolved': not tb.get('unresolved', False),
            'lines': lines,
            'summary': ' → '.join(lines[-2:]) if lines else '',
        })
    return resolutions


def generate(data_path='data/tournament.json', skip_narrative=False):
    data = load_tournament(data_path)
    our_team = data['tournament']['our_team']
    our_pool = data['teams'][our_team]['pool']
    qf_pool = 'F'

    our_analysis = enumerate_scenarios(our_pool, data)
    qf_analysis = enumerate_scenarios(qf_pool, data)

    state = {
        'generated_at': datetime.now().astimezone().isoformat(),
        'tournament': data['tournament'],
        'our_team': our_team,
        'our_team_name': data['teams'][our_team]['name'],
        'our_pool': our_pool,
        'qf_pool': qf_pool,
        'teams': data['teams'],

        'standings': build_standings(our_pool, data, our_analysis),
        'scenarios': build_scenario_data(our_analysis, our_team),
        'upcoming': build_games_list(our_pool, data, 'scheduled'),
        'completed': build_games_list(our_pool, data, 'final'),
        'live_games': build_games_list(our_pool, data, 'in_progress'),

        'qf_standings': build_standings(qf_pool, data, qf_analysis),
        'qf_scenarios': build_scenario_data(qf_analysis, None),
        'qf_upcoming': build_games_list(qf_pool, data, 'scheduled'),

        'bracket': build_bracket(data),
        'tiebreaker_resolution': build_tiebreaker_resolution(our_pool, data, our_analysis),
        'event_log': data.get('event_log', []),
        'narrative': None,
        'scouting': data.get('scouting'),
    }

    output_dir = Path(__file__).parent.parent / 'docs' / 'data'
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / 'state.json'
    output_path.write_text(json.dumps(state, indent=2))
    print(f'Generated {output_path} ({output_path.stat().st_size} bytes)')
    return state


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default='data/tournament.json')
    parser.add_argument('--skip-narrative', action='store_true')
    args = parser.parse_args()
    generate(args.data, args.skip_narrative)
