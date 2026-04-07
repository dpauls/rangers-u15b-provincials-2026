#!/usr/bin/env python3
"""
Claude API integration for generating tournament narrative and event commentary.

Uses Sonnet for fast, cheap commentary. Falls back gracefully if API is unavailable.
"""
import json
import logging
import os

log = logging.getLogger(__name__)

MODEL = 'claude-sonnet-4-20250514'
MAX_TOKENS = 600


def _get_client():
    """Get Anthropic client, or None if unavailable."""
    try:
        import anthropic
        key = os.environ.get('ANTHROPIC_API_KEY')
        if not key:
            log.warning('ANTHROPIC_API_KEY not set, narrative disabled')
            return None
        return anthropic.Anthropic(api_key=key)
    except ImportError:
        log.warning('anthropic package not installed, narrative disabled')
        return None


def _call(prompt, max_tokens=MAX_TOKENS):
    """Call Claude API with a prompt. Returns text or None on failure."""
    client = _get_client()
    if not client:
        return None
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            messages=[{'role': 'user', 'content': prompt}],
        )
        return resp.content[0].text
    except Exception as e:
        log.error(f'Claude API error: {e}')
        return None


def _rank_str(r):
    """Format ranking for prompt: '#18' or '' if no ranking."""
    rank = r.get('ranking')
    return f' (ranked #{rank} provincially)' if rank else ''


def generate_overall_narrative(standings, scenarios, our_team_name, our_pool,
                                qf_pool_standings, completed_games, upcoming_games):
    """Generate 2-3 paragraph tournament overview for the main page."""
    standings_str = '\n'.join(
        f"  {r['name']} [provincial rank #{r.get('ranking', '?')}]: "
        f"{r['w']}W-{r['l']}L-{r['t']}T, {r['pts']}pts (GF={r['gf']} GA={r['ga']})"
        + (' ← US' if r.get('is_us') else '')
        for r in standings
    )

    completed_str = '\n'.join(
        f"  {g['home_name']} {g['home_score']}-{g['away_score']} {g['away_name']}"
        for g in (completed_games or [])
    ) or '  No games completed yet.'

    upcoming_str = '\n'.join(
        f"  {g['home_name']} vs {g['away_name']} ({g['date']})"
        for g in (upcoming_games or [])[:4]
    ) or '  No upcoming games.'

    qf_str = '\n'.join(
        f"  {r['name']}: {r['w']}W-{r['l']}L-{r['t']}T, {r['pts']}pts"
        for r in (qf_pool_standings or [])
    ) or '  No data yet.'

    if scenarios and scenarios.get('total', 0) > 1:
        sc_summary = (f"Out of {scenarios['total']} possible combinations of remaining game outcomes, "
                      f"{our_team_name} wins Pool {our_pool} in {scenarios['our_count']} of them.")
    elif scenarios and scenarios.get('total') == 1:
        sc_summary = f"All pool games are complete."
    else:
        sc_summary = "No scenario data available."

    prompt = f"""You are the tournament analyst for the {our_team_name} at the OWHA U15B Provincial Championships.

Pool {our_pool} standings:
{standings_str}

Recent results:
{completed_str}

Coming up:
{upcoming_str}

Scenario analysis: {sc_summary}

Quarterfinal opponent pool (Pool {qf_pool_standings[0]['name'].split()[0] if qf_pool_standings else '?'} watch):
{qf_str}

The provincial rankings indicate pre-tournament strength. Lower rank = stronger team.
A lower-ranked team beating a higher-ranked team is an upset worth noting.

IMPORTANT about scenario numbers: The scenario analysis counts how many possible
combinations of remaining game outcomes (win/loss/tie) result in us winning the pool.
These are NOT probabilities or predictions -- they treat all outcomes as equally likely,
which they aren't (ties are rarer, higher-ranked teams win more often). So NEVER say
"29% chance" or "60% probability". Instead say things like "we win the pool in 49 out
of 81 possible outcome combinations" or "the majority of scenarios have us advancing"
or "only a narrow set of outcomes would knock us out". Use the numbers to illustrate
how many paths exist, not to predict likelihood.

Write 2-3 short paragraphs for hockey parents reading on their phones at the rink. Cover:
1. Where {our_team_name} stands right now in Pool {our_pool}
2. What the next game means and what result we need -- reference the opponent's ranking
3. Who to root for in other pool games and why -- note when a result would be an upset based on rankings

When discussing matchups, mention rankings naturally (e.g., "our #18 Rangers face #3 Kincardine" or
"an upset by #25 Ennismore over #3 Kincardine would help us").
Be conversational and specific. No jargon. No filler.
Keep it under 200 words total."""

    return _call(prompt)


def generate_game_final_comment(game, standings, prev_scenarios, curr_scenarios,
                                 our_team, our_team_name, our_pool, teams=None):
    """Generate 1-2 sentence commentary when a game finishes."""
    is_our_game = (game['home'] == our_team or game['away'] == our_team)

    standings_str = ', '.join(
        f"{r['name'].split('#')[0].strip()} (#{r.get('ranking','?')}) {r['pts']}pts"
        for r in standings
    )

    prev_count = prev_scenarios.get('our_count', '?') if prev_scenarios else '?'
    prev_total = prev_scenarios.get('total', '?') if prev_scenarios else '?'
    curr_count = curr_scenarios.get('our_count', '?') if curr_scenarios else '?'
    curr_total = curr_scenarios.get('total', '?') if curr_scenarios else '?'

    home_name = game.get('home_name', game['home'])
    away_name = game.get('away_name', game['away'])

    # Get rankings for the two teams
    home_rank = teams.get(game['home'], {}).get('ranking', '?') if teams else '?'
    away_rank = teams.get(game['away'], {}).get('ranking', '?') if teams else '?'

    # Determine if this was an upset (lower-ranked team won)
    upset_note = ''
    winner_rank = home_rank if game['home_score'] > game['away_score'] else away_rank
    loser_rank = away_rank if game['home_score'] > game['away_score'] else home_rank
    if isinstance(winner_rank, int) and isinstance(loser_rank, int):
        if winner_rank > loser_rank:
            upset_note = f'\nThis was an UPSET: the #{winner_rank} team beat the #{loser_rank} team!'

    clinch_note = ''
    if curr_scenarios and curr_count == curr_total:
        clinch_note = f'\n{our_team_name} has CLINCHED first place in Pool {our_pool}!'
    elif curr_scenarios and curr_count == 0:
        clinch_note = f'\n{our_team_name} has been ELIMINATED from Pool {our_pool}.'

    prompt = f"""A game just finished at the OWHA U15B Provincial Championships.

Result: {home_name} (ranked #{home_rank}) {game['home_score']} - {game['away_score']} {away_name} (ranked #{away_rank}) (Pool {our_pool})
{'This was OUR game.' if is_our_game else 'We were not playing in this game.'}
{upset_note}

Updated standings: {standings_str}
Scenario impact: Out of {curr_total} remaining outcome combinations, {our_team_name} wins the pool in {curr_count} (was {prev_count} of {prev_total}).
{clinch_note}

If this was an upset based on rankings, mention it. Lower rank number = stronger team.
IMPORTANT: Scenario counts are NOT probabilities. Don't say "X% chance". Instead say
things like "we win the pool in X out of Y outcome combinations" or "most paths have us advancing".
Write 1-2 sentences explaining what this result means for {our_team_name}. Be specific. Parents are reading on phones."""

    return _call(prompt, max_tokens=200)


def generate_in_game_comment(game, our_team, our_team_name, scenarios_if_holds,
                              scenarios_if_reversed, total_scenarios):
    """Generate 1 sentence about a game in progress that Kanata is not playing in."""
    home_name = game.get('home_name', game['home'])
    away_name = game.get('away_name', game['away'])
    hs = game.get('home_score', 0)
    as_ = game.get('away_score', 0)

    if hs > as_:
        leading = home_name
        trailing = away_name
    elif as_ > hs:
        leading = away_name
        trailing = home_name
    else:
        leading = 'tied'
        trailing = None

    prompt = f"""A pool game is in progress at the OWHA U15B Provincials. {our_team_name} is NOT playing in this game.

Current score: {home_name} {hs} - {as_} {away_name}
If this score holds: {our_team_name} advances in {scenarios_if_holds} of {total_scenarios} remaining scenarios.
If the other team comes back to win: {our_team_name} advances in {scenarios_if_reversed} of {total_scenarios} scenarios.

Write exactly 1 fun, brief sentence about who we're rooting for and why. Reference the scenario numbers."""

    return _call(prompt, max_tokens=100)


def generate_correction_comment(game, old_score, new_score, our_team_name):
    """Generate a note about a post-game score correction."""
    home_name = game.get('home_name', game['home'])
    away_name = game.get('away_name', game['away'])

    return (f"Score correction: {home_name} vs {away_name} changed from "
            f"{old_score[0]}-{old_score[1]} to {new_score[0]}-{new_score[1]}. "
            f"Analysis has been updated.")


# ── CLI test ────────────────────────────────────────────────────

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    # Quick test with fake data
    test_standings = [
        {'name': 'Kanata Rangers', 'is_us': True, 'w': 2, 'l': 0, 't': 0, 'pts': 4, 'gf': 5, 'ga': 1},
        {'name': 'Kincardine Kinucks', 'is_us': False, 'w': 1, 'l': 0, 't': 1, 'pts': 3, 'gf': 4, 'ga': 3},
        {'name': 'Windsor Wildcats', 'is_us': False, 'w': 0, 'l': 1, 't': 1, 'pts': 1, 'gf': 3, 'ga': 4},
        {'name': 'Ennismore Eagles', 'is_us': False, 'w': 0, 'l': 2, 't': 0, 'pts': 0, 'gf': 1, 'ga': 5},
    ]
    test_scenarios = {'our_count': 65, 'total': 81, 'our_pct': 80.2}
    test_qf = [
        {'name': 'Durham West Lightning', 'w': 1, 'l': 0, 't': 0, 'pts': 2, 'gf': 3, 'ga': 1},
        {'name': 'London Devilettes', 'w': 0, 'l': 0, 't': 1, 'pts': 1, 'gf': 2, 'ga': 2},
    ]

    print('=== Overall Narrative ===')
    result = generate_overall_narrative(
        test_standings, test_scenarios, 'Kanata Rangers', 'C',
        test_qf,
        [{'home_name': 'Kanata Rangers', 'home_score': 3, 'away_score': 1, 'away_name': 'Ennismore Eagles'}],
        [{'home_name': 'Windsor Wildcats', 'away_name': 'Kanata Rangers', 'date': 'Fri 3:45 PM'}],
    )
    print(result or '(no API key)')
