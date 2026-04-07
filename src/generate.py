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
    total = analysis['total'] if analysis else 1

    rows = []
    for tid in sorted(st, key=lambda t: (st[t]['PTS'], st[t]['W'], st[t]['GF'] - st[t]['GA']), reverse=True):
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
            'adv_pct': round(count / total * 100, 1) if total > 0 else 0,
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

    deterministic = total - unresolved_count
    return {
        'total': total,
        'deterministic': deterministic,
        'our_count': our_count,  # deterministic wins only
        'our_pct': round(our_count / deterministic * 100, 1) if deterministic > 0 else 0,
        'unresolved': unresolved_count,
        'remaining_games': len(remaining),
        'remaining_info': remaining_info,
        'gd_dependent': analysis['gd_dependent_count'],
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
        'event_log': data.get('event_log', []),
        'narrative': None,
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
