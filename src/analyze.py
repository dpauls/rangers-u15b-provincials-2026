#!/usr/bin/env python3
"""
OWHA U15B Provincials - Pool Play Scenario Analysis Engine

Reads tournament data from JSON, computes standings and H2H from game results,
enumerates all possible outcomes of remaining games, and determines pool winners
with full tiebreaker resolution.

Tiebreaker rules (OWHA Handbook Section C.6):
  i.   Number of wins
  ii.  Record against other tied teams
  iii. Goal differential
  iv.  Fewest goals allowed
  v.   Most periods won (not implemented - data rarely available)
  vi.  Fewest penalty minutes
  vii. First goal scored in series (not implemented)
  viii.Coin flip (not implemented)
"""
import itertools
import json
from copy import deepcopy
from pathlib import Path


# ── Data loading ────────────────────────────────────────────────

def load_tournament(path='data/tournament.json'):
    with open(path) as f:
        return json.load(f)


def compute_standings(pool_id, teams, games):
    """Compute standings from completed/in-progress games for a pool."""
    pool_teams = {tid: teams[tid] for tid in teams if teams[tid]['pool'] == pool_id}
    st = {}
    for tid in pool_teams:
        st[tid] = {'W': 0, 'L': 0, 'T': 0, 'PTS': 0, 'GF': 0, 'GA': 0, 'PIM': 0}

    for g in games:
        if g['pool'] != pool_id:
            continue
        if g['status'] not in ('final', 'in_progress'):
            continue
        if g['home_score'] is None or g['away_score'] is None:
            continue
        h, a = g['home'], g['away']
        hs, as_ = g['home_score'], g['away_score']
        st[h]['GF'] += hs
        st[h]['GA'] += as_
        st[a]['GF'] += as_
        st[a]['GA'] += hs
        if g['status'] == 'final':
            if hs > as_:
                st[h]['W'] += 1; st[h]['PTS'] += 2; st[a]['L'] += 1
            elif as_ > hs:
                st[a]['W'] += 1; st[a]['PTS'] += 2; st[h]['L'] += 1
            else:
                st[h]['T'] += 1; st[h]['PTS'] += 1
                st[a]['T'] += 1; st[a]['PTS'] += 1

    return st


def compute_h2h(pool_id, games):
    """Build H2H lookup from completed games. Key: tuple(sorted([a,b])), value: winner or 'T'."""
    h2h = {}
    for g in games:
        if g['pool'] != pool_id or g['status'] != 'final':
            continue
        if g['home_score'] is None or g['away_score'] is None:
            continue
        key = _key(g['home'], g['away'])
        hs, as_ = g['home_score'], g['away_score']
        if hs > as_:
            h2h[key] = g['home']
        elif as_ > hs:
            h2h[key] = g['away']
        else:
            h2h[key] = 'T'
    return h2h


def get_remaining_games(pool_id, games):
    """Return list of games in the pool that haven't been completed."""
    return [g for g in games if g['pool'] == pool_id and g['status'] == 'scheduled']


# ── Scenario simulation ────────────────────────────────────────

def _key(a, b):
    return tuple(sorted([a, b]))


def apply_result(st, h2h, home, away, outcome):
    """Apply a simulated game result. outcome: 0=home win, 1=tie, 2=away win."""
    if outcome == 0:
        st[home]['W'] += 1; st[home]['PTS'] += 2
        st[home]['GF'] += 1; st[away]['GA'] += 1; st[away]['L'] += 1
        h2h[_key(home, away)] = home
    elif outcome == 1:
        st[home]['T'] += 1; st[home]['PTS'] += 1
        st[away]['T'] += 1; st[away]['PTS'] += 1
        h2h[_key(home, away)] = 'T'
    else:
        st[away]['W'] += 1; st[away]['PTS'] += 2
        st[away]['GF'] += 1; st[home]['GA'] += 1; st[home]['L'] += 1
        h2h[_key(home, away)] = away


# ── Helpers ─────────────────────────────────────────────────────

def _group_by(items, key_fn):
    groups, cur_k, cur = [], None, []
    for item in items:
        k = key_fn(item)
        if k != cur_k:
            if cur:
                groups.append(cur)
            cur_k, cur = k, [item]
        else:
            cur.append(item)
    if cur:
        groups.append(cur)
    return groups


def _mini_pts(team, group, h2h):
    pts = 0
    for opp in group:
        if opp == team:
            continue
        r = h2h.get(_key(team, opp))
        if r == team:
            pts += 2
        elif r == 'T':
            pts += 1
    return pts


def _h2h_record_str(team, group, h2h):
    parts = []
    for opp in group:
        if opp == team:
            continue
        r = h2h.get(_key(team, opp))
        if r == team:
            parts.append(f'beat {opp}')
        elif r == 'T':
            parts.append(f'tied {opp}')
        else:
            parts.append(f'lost to {opp}')
    return ', '.join(parts)


# ── Tiebreaker machinery ───────────────────────────────────────

def resolve_tie(group, st, h2h, spots, indent=''):
    """
    Returns (advancing, eliminated, lines, gd_dependent).
    lines: list of description strings for verbose output.
    """
    if len(group) <= spots:
        return list(group), [], [], False

    lines = []
    gd_dep = False
    I = indent

    # ── Rule i: Number of wins ──
    sg = sorted(group, key=lambda t: st[t]['W'], reverse=True)
    win_gs = _group_by(sg, lambda t: st[t]['W'])
    if len(win_gs) > 1:
        detail = ' > '.join(
            ','.join(t for t in wg) + f' ({st[wg[0]]["W"]}W)'
            for wg in win_gs
        )
        lines.append(f'{I}Rule i (wins): {detail}')
        adv, elim = [], []
        for wg in win_gs:
            rem = spots - len(adv)
            if rem <= 0:
                elim.extend(wg)
                lines.append(f'{I}  {",".join(wg)} eliminated (fewer wins)')
            elif len(wg) <= rem:
                adv.extend(wg)
                lines.append(f'{I}  {",".join(wg)} advance ({st[wg[0]]["W"]}W)')
            else:
                lines.append(f'{I}  Still tied ({st[wg[0]]["W"]}W): {",".join(wg)} — {rem} spot{"s" if rem>1 else ""} left')
                sub_a, sub_e, sub_lines, sub_gd = resolve_tie(wg, st, h2h, rem, indent + '    ')
                adv.extend(sub_a); elim.extend(sub_e); lines.extend(sub_lines)
                gd_dep = gd_dep or sub_gd
        return adv, elim, lines, gd_dep

    # ── Rule ii: H2H mini-table ──
    sg = sorted(group, key=lambda t: _mini_pts(t, group, h2h), reverse=True)
    h2h_gs = _group_by(sg, lambda t: _mini_pts(t, group, h2h))
    if len(h2h_gs) > 1:
        lines.append(f'{I}Rule ii (h2h among {",".join(group)}):')
        for t in sg:
            mp = _mini_pts(t, group, h2h)
            rec = _h2h_record_str(t, group, h2h)
            lines.append(f'{I}  {t}: {mp}pts ({rec})')
        adv, elim = [], []
        for hg in h2h_gs:
            rem = spots - len(adv)
            if rem <= 0:
                elim.extend(hg)
            elif len(hg) <= rem:
                adv.extend(hg)
            else:
                lines.append(f'{I}  Still tied on h2h: {",".join(hg)} — {rem} spot{"s" if rem>1 else ""} left')
                # Restart full tiebreaker chain for sub-group
                sub_a, sub_e, sub_lines, sub_gd = resolve_tie(hg, st, h2h, rem, indent + '    ')
                adv.extend(sub_a); elim.extend(sub_e); lines.extend(sub_lines)
                gd_dep = gd_dep or sub_gd
        lines.append(f'{I}  → {",".join(adv)} advance / {",".join(elim)} eliminated')
        return adv, elim, lines, gd_dep

    # ── All same h2h → Rule iii ──
    lines.append(f'{I}Rule ii (h2h): all tied at {_mini_pts(group[0], group, h2h)}pts')
    return resolve_tie_gd(group, st, h2h, spots, indent)


def resolve_tie_gd(group, st, h2h, spots, indent=''):
    """Rules iii-iv+. Returns (adv, elim, lines, gd_dependent)."""
    if len(group) <= spots:
        return list(group), [], [], False

    lines = []
    I = indent

    # ── Rule iii: Goal differential ──
    sg = sorted(group, key=lambda t: st[t]['GF'] - st[t]['GA'], reverse=True)
    gd_gs = _group_by(sg, lambda t: st[t]['GF'] - st[t]['GA'])
    if len(gd_gs) > 1:
        detail = ' > '.join(
            ','.join(t for t in gg) + f' ({st[gg[0]]["GF"]-st[gg[0]]["GA"]:+d})'
            for gg in gd_gs
        )
        lines.append(f'{I}Rule iii (GD) [score-dependent]: {detail}')
        adv, elim = [], []
        for gg in gd_gs:
            rem = spots - len(adv)
            if rem <= 0:
                elim.extend(gg)
            elif len(gg) <= rem:
                adv.extend(gg)
            else:
                sub_a, sub_e, sub_lines, _ = resolve_tie_ga(gg, st, spots - len(adv), indent + '    ')
                adv.extend(sub_a); elim.extend(sub_e); lines.extend(sub_lines)
        lines.append(f'{I}  → {",".join(adv)} advance / {",".join(elim)} eliminated')
        return adv, elim, lines, True

    lines.append(f'{I}Rule iii (GD): all at {st[group[0]]["GF"]-st[group[0]]["GA"]:+d} — still tied')
    return resolve_tie_ga(group, st, spots, indent)


def resolve_tie_ga(group, st, spots, indent=''):
    """Rule iv. Returns (adv, elim, lines, gd_dependent)."""
    if len(group) <= spots:
        return list(group), [], [], False

    lines = []
    I = indent

    sg = sorted(group, key=lambda t: st[t]['GA'])
    ga_gs = _group_by(sg, lambda t: st[t]['GA'])
    if len(ga_gs) > 1:
        detail = ' > '.join(
            ','.join(t for t in gg) + f' ({st[gg[0]]["GA"]}GA)'
            for gg in ga_gs
        )
        lines.append(f'{I}Rule iv (GA) [score-dependent]: {detail}')
        adv, elim = [], []
        for gg in ga_gs:
            rem = spots - len(adv)
            if rem <= 0:
                elim.extend(gg)
            elif len(gg) <= rem:
                adv.extend(gg)
            else:
                adv.extend(gg[:rem]); elim.extend(gg[rem:])
        lines.append(f'{I}  → {",".join(adv)} advance / {",".join(elim)} eliminated')
        return adv, elim, lines, True

    # Rule vi: Fewest penalty minutes (if available)
    if any(st[t].get('PIM', 0) > 0 for t in group):
        sg = sorted(group, key=lambda t: st[t].get('PIM', 0))
        pim_gs = _group_by(sg, lambda t: st[t].get('PIM', 0))
        if len(pim_gs) > 1:
            detail = ' > '.join(
                ','.join(t for t in pg) + f' ({st[pg[0]].get("PIM", 0)}PIM)'
                for pg in pim_gs
            )
            lines.append(f'{I}Rule vi (PIM): {detail}')
            adv, elim = [], []
            for pg in pim_gs:
                rem = spots - len(adv)
                if rem <= 0:
                    elim.extend(pg)
                elif len(pg) <= rem:
                    adv.extend(pg)
                else:
                    adv.extend(pg[:rem]); elim.extend(pg[rem:])
            lines.append(f'{I}  → {",".join(adv)} advance / {",".join(elim)} eliminated')
            return adv, elim, lines, True

    lines.append(f'{I}UNRESOLVED — tiebreakers i-iv(+vi) exhausted')
    return list(group[:spots]), list(group[spots:]), lines, True


# ── Top-level pool resolver ─────────────────────────────────────

def determine_pool_winner(pool_teams, st, h2h, advance_count=1):
    """
    Returns dict:
      'advancing': list of advancing team IDs
      'eliminated': list of eliminated team IDs
      'tb_details': list of tiebreaker info dicts
      'tb_teams': set of teams involved in tiebreakers
    """
    by_pts = sorted(pool_teams, key=lambda t: st[t]['PTS'], reverse=True)
    gs = _group_by(by_pts, lambda t: st[t]['PTS'])

    result = []
    tb_details = []
    tb_teams = set()
    tb_num = 0

    for g in gs:
        rem = advance_count - len(result)
        if rem <= 0:
            break
        if len(g) <= rem:
            result.extend(g)
        else:
            tb_num += 1
            adv, elim, lines, gd_dep = resolve_tie(g, st, h2h, rem, indent='      ')
            result.extend(adv)
            tb_teams.update(g)
            tb_details.append({
                'num': tb_num,
                'teams': list(g),
                'pts': st[g[0]]['PTS'],
                'spots': rem,
                'adv': adv,
                'elim': elim,
                'lines': lines,
                'gd_dep': gd_dep,
            })

    eliminated = [t for t in pool_teams if t not in result]
    return {
        'advancing': result,
        'eliminated': eliminated,
        'tb_details': tb_details,
        'tb_teams': tb_teams,
    }


# ── Scenario enumeration ───────────────────────────────────────

def enumerate_scenarios(pool_id, tournament_data):
    """
    Enumerate all possible outcomes of remaining games in a pool.
    Returns dict with:
      'total': total scenarios
      'counts': {team_id: number of scenarios where team advances}
      'scenarios': list of scenario dicts
      'gd_dependent_count': number of scenarios where GD/GA decides
    """
    teams = tournament_data['teams']
    games = tournament_data['pool_games']
    pool_teams = [tid for tid in teams if teams[tid]['pool'] == pool_id]
    advance_count = tournament_data['pools'][pool_id]['advance_count']

    base_st = compute_standings(pool_id, teams, games)
    base_h2h = compute_h2h(pool_id, games)
    remaining = get_remaining_games(pool_id, games)

    total = 3 ** len(remaining)
    counts = {t: 0 for t in pool_teams}
    scenarios = []
    gd_count = 0

    for outcomes in itertools.product([0, 1, 2], repeat=len(remaining)):
        st = deepcopy(base_st)
        h2h = dict(base_h2h)
        for i, o in enumerate(outcomes):
            apply_result(st, h2h, remaining[i]['home'], remaining[i]['away'], o)

        res = determine_pool_winner(pool_teams, st, h2h, advance_count)
        for t in res['advancing']:
            counts[t] += 1
        gd_dep = any(tb['gd_dep'] for tb in res['tb_details'])
        if gd_dep:
            gd_count += 1

        outcome_labels = []
        for i, o in enumerate(outcomes):
            g = remaining[i]
            if o == 0:
                outcome_labels.append(f'{g["home"]}win')
            elif o == 1:
                outcome_labels.append('tie')
            else:
                outcome_labels.append(f'{g["away"]}win')

        scenarios.append({
            'outcomes': outcomes,
            'labels': outcome_labels,
            'standings': st,
            'result': res,
            'gd_dependent': gd_dep,
        })

    return {
        'total': total,
        'counts': counts,
        'scenarios': scenarios,
        'gd_dependent_count': gd_count,
        'remaining_games': remaining,
        'pool_teams': pool_teams,
    }


def what_if_projection(pool_id, tournament_data, assumed_scores):
    """
    Run scenario analysis assuming certain in-progress games finish with given scores.
    assumed_scores: list of {'game_id': id, 'home_score': x, 'away_score': y}
    Returns same format as enumerate_scenarios.
    """
    data = deepcopy(tournament_data)
    for assumed in assumed_scores:
        for g in data['pool_games']:
            if g['game_id'] == assumed['game_id']:
                g['home_score'] = assumed['home_score']
                g['away_score'] = assumed['away_score']
                g['status'] = 'final'
    return enumerate_scenarios(pool_id, data)


# ── Text output (for CLI testing) ──────────────────────────────

def print_analysis(pool_id, tournament_data):
    """Print full scenario analysis to stdout (for testing)."""
    teams = tournament_data['teams']
    analysis = enumerate_scenarios(pool_id, tournament_data)
    remaining = analysis['remaining_games']
    total = analysis['total']

    if not remaining:
        # All games complete, just show the final result
        st = compute_standings(pool_id, teams, tournament_data['pool_games'])
        h2h = compute_h2h(pool_id, tournament_data['pool_games'])
        pool_teams = analysis['pool_teams']
        advance_count = tournament_data['pools'][pool_id]['advance_count']
        res = determine_pool_winner(pool_teams, st, h2h, advance_count)
        print(f'Pool {pool_id} — ALL GAMES COMPLETE')
        print(f'Winner: {", ".join(res["advancing"])}')
        for tb in res['tb_details']:
            for line in tb['lines']:
                print(line)
        return analysis

    print(f'Pool {pool_id} — {len(remaining)} games remaining, {total} scenarios')
    game_strs = ' | '.join(g['home'] + 'v' + g['away'] for g in remaining)
    print(f'Games: {game_strs}')
    print()

    for i, sc in enumerate(analysis['scenarios']):
        res = sc['result']
        flag = ' *' if sc['gd_dependent'] else ''
        adv_str = ', '.join(f'{t}({sc["standings"][t]["PTS"]}pts)' for t in res['advancing'])
        games_str = '  '.join(f'{l:>7}' for l in sc['labels'])
        print(f'#{i+1:3d}  {games_str}  →  {adv_str}{flag}')

        for tb in res['tb_details']:
            tied_names = ', '.join(tb['teams'])
            print(f'   TB: {len(tb["teams"])}-way tie at {tb["pts"]}pts for {tb["spots"]} spot: {tied_names}')
            for line in tb['lines']:
                print(line)
            print()

    print('=' * 60)
    print('SUMMARY')
    print('=' * 60)
    print(f'{"Team":>5} {"Name":20s} {"Adv":>5} {"Out":>5} {"Adv%":>7}')
    print('-' * 45)
    for t in sorted(analysis['pool_teams'], key=lambda t: analysis['counts'][t], reverse=True):
        a = analysis['counts'][t]
        name = teams[t]['name']
        status = ''
        if a == total:
            status = '  ← CLINCHED'
        elif a == 0:
            status = '  ← ELIMINATED'
        print(f'{t:>5} {name:20s} {a:5d} {total-a:5d} {a/total*100:6.1f}%{status}')

    if analysis['gd_dependent_count'] > 0:
        print(f'\n* = GD/GA was the deciding tiebreaker ({analysis["gd_dependent_count"]} scenarios)')
        print('  (uses 1-0 for wins, 0-0 for ties; actual scores could change these)')

    return analysis


# ── Main ────────────────────────────────────────────────────────

if __name__ == '__main__':
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else 'data/tournament.json'
    data = load_tournament(path)
    our_team = data['tournament']['our_team']

    for pool_id in data['pools']:
        pool_teams = [t for t in data['teams'] if data['teams'][t]['pool'] == pool_id]
        is_our_pool = our_team in pool_teams
        label = f' (OUR POOL)' if is_our_pool else ''
        print(f'\n{"="*60}')
        print(f'POOL {pool_id}{label}')
        print(f'{"="*60}\n')
        print_analysis(pool_id, data)
        print()
