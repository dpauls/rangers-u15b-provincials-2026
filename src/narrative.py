#!/usr/bin/env python3
"""
Claude API integration for generating tournament narrative and event commentary.

Uses Sonnet for fast, cheap commentary. Falls back gracefully if API is unavailable.
"""
import json
import logging
import os
from pathlib import Path

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


LLM_LOG_PATH = Path(__file__).parent.parent / 'logs' / 'llm_interactions.log'


def _call(prompt, max_tokens=MAX_TOKENS, label='unknown'):
    """Call Claude API with a prompt. Returns text or None on failure.

    All interactions are logged to logs/llm_interactions.log for debugging.
    """
    # Log the prompt
    _log_interaction(label, 'PROMPT', prompt)

    client = _get_client()
    if not client:
        _log_interaction(label, 'SKIPPED', 'No API client available')
        return None
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            messages=[{'role': 'user', 'content': prompt}],
        )
        result = resp.content[0].text
        _log_interaction(label, 'RESPONSE', result)
        return result
    except Exception as e:
        _log_interaction(label, 'ERROR', str(e))
        log.error(f'Claude API error: {e}')
        return None


def _log_interaction(label, phase, content):
    """Log an LLM interaction to the log file."""
    try:
        LLM_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        separator = '=' * 80
        with open(LLM_LOG_PATH, 'a') as f:
            f.write(f'\n{separator}\n')
            f.write(f'[{timestamp}] {label} — {phase}\n')
            f.write(f'{separator}\n')
            f.write(content)
            f.write(f'\n{separator}\n\n')
    except Exception:
        pass  # Don't let logging failures break the pipeline


def _rank_str(r):
    """Format ranking for prompt: '#18' or '' if no ranking."""
    rank = r.get('ranking')
    return f' (ranked #{rank} provincially)' if rank else ''


def _format_date(iso_date):
    """Convert ISO date to readable format like 'Fri Apr 10, 3:45 PM'."""
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(iso_date)
        return dt.strftime('%a %b %d, %I:%M %p').replace(' 0', ' ')
    except (ValueError, TypeError):
        return str(iso_date)


def generate_overall_narrative(standings, scenarios, our_team_name, our_pool,
                                qf_pool_standings, completed_games, upcoming_games,
                                live_games=None):
    """Generate 2-3 paragraph tournament overview for the main page."""
    standings_str = '\n'.join(
        f"  {r['name']} [provincial rank #{r.get('ranking', '?')}]: "
        f"{r['w']}W-{r['l']}L-{r['t']}T, {r['pts']}pts (GF={r['gf']} GA={r['ga']})"
        + (' ← US' if r.get('is_us') else '')
        for r in standings
    )

    completed_str = '\n'.join(
        f"  FINAL: {g['home_name']} {g['home_score']}-{g['away_score']} {g['away_name']}"
        for g in (completed_games or [])
    ) or '  No games completed yet.'

    live_str = '\n'.join(
        f"  IN PROGRESS: {g['home_name']} {g.get('home_score',0)}-{g.get('away_score',0)} {g['away_name']}"
        for g in (live_games or [])
    ) or ''

    upcoming_str = '\n'.join(
        f"  SCHEDULED: {g['home_name']} vs {g['away_name']} — {_format_date(g['date'])}"
        for g in (upcoming_games or [])[:4]
    ) or '  No upcoming games.'

    # Find our next scheduled game explicitly
    our_next = None
    for g in (upcoming_games or []):
        if g.get('home') == 'KAN' or g.get('away') == 'KAN' or \
           'Kanata' in g.get('home_name', '') or 'Kanata' in g.get('away_name', ''):
            our_next = g
            break
    our_next_str = ''
    if our_next:
        opp = our_next['away_name'] if 'Kanata' in our_next.get('home_name', '') else our_next['home_name']
        our_next_str = f'\nOUR NEXT GAME: {our_team_name} vs {opp} — {_format_date(our_next["date"])}'

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

=== FACTS (use these exactly, do NOT invent schedule details) ===

Pool {our_pool} standings:
{standings_str}

Games in progress right now:
{live_str or '  None'}

Completed games:
{completed_str}

Upcoming scheduled games:
{upcoming_str}
{our_next_str}

Scenario analysis: {sc_summary}

Quarterfinal opponent pool watch:
{qf_str}

=== END FACTS ===

Provincial rankings: #1 is the strongest team in the province, higher numbers are weaker.
For reference: #3 Kincardine is the strongest in our pool, then #18 Kanata (us),
#25 Ennismore, and #41 Windsor is the weakest. A higher-ranked (bigger number) team
beating a lower-ranked (smaller number) team is an upset.

IMPORTANT TONE RULES:
- NEVER call any game "winnable" or "easy" based on rankings. Every team at Provincials
  earned their spot and will be fired up. Even if rankings favor us, say things like
  "rankings suggest we match up well, but they'll come out hungry" or "on paper we have
  an edge, but anything can happen in tournament hockey."
- NEVER say "X% chance" or "probability". Scenario counts are NOT predictions.
  Say "we win the pool in X out of Y scenarios" to illustrate paths, not likelihood.

Write a structured update using **bold section headings** for hockey parents on phones.
Use exactly these sections:

**Where We Stand** — 1-2 sentences on our current pool position.
**Our Next Game** — 1-2 sentences about our upcoming game. ONLY reference the game
  listed under "OUR NEXT GAME" in the facts. Use the exact opponent and date/time shown.
  Do NOT invent or guess schedule details.
**Around the Pool** — 1-2 sentences on other games and who to root for.

CRITICAL: Only state facts from the FACTS section above. If a game is in progress, say so.
Do NOT guess or infer any schedule, score, or matchup not explicitly listed.
Be conversational and specific. No jargon. Keep it under 150 words total."""

    return _call(prompt, label='overall_narrative')


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

    return _call(prompt, max_tokens=200, label="game_final_comment")


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

    return _call(prompt, max_tokens=100, label="in_game_comment")


def generate_event_impact(event_type, home_name, away_name, home_score, away_score,
                          is_our_game, our_team_name, scenarios_if_holds=None,
                          total_scenarios=None, tournament_context=None):
    """Generate a 1-sentence impact comment for any game event.

    tournament_context is an optional dict with:
        standings_summary, completed_summary, upcoming_summary,
        scenario_detail (for small scenario counts)
    """
    score_str = f"{home_name} {home_score}-{away_score} {away_name}"

    ctx = ''
    if tournament_context:
        ctx = f"""
=== TOURNAMENT CONTEXT ===
Standings: {tournament_context.get('standings_summary', 'unknown')}
Completed games: {tournament_context.get('completed_summary', 'unknown')}
Remaining games: {tournament_context.get('upcoming_summary', 'none')}
"""
        if tournament_context.get('scenario_detail'):
            ctx += f"Key scenarios: {tournament_context['scenario_detail']}\n"
        ctx += "=== END CONTEXT ===\n"

    if event_type == 'game_started':
        if is_our_game:
            prompt = f"""{ctx}Our team ({our_team_name}) just started a game at OWHA U15B Provincials.
Matchup: {score_str}
Write exactly 1 short sentence. Be specific to the situation — is this a must-win? A chance to clinch? Reference the stakes."""
        else:
            prompt = f"""{ctx}A pool game just started at OWHA U15B Provincials. We ({our_team_name}) are not playing.
Matchup: {score_str}
Write exactly 1 short sentence about why this specific game matters to us based on the tournament context. Who do we need to win?"""
    elif event_type == 'score_change':
        scenario_note = ''
        if scenarios_if_holds is not None and total_scenarios:
            scenario_note = f"\nIf this score holds, we win the pool in {scenarios_if_holds} of {total_scenarios} resolved scenarios."
        if is_our_game:
            prompt = f"""{ctx}Score update in our game ({our_team_name}) at OWHA U15B Provincials.
Current: {score_str}{scenario_note}
Write exactly 1 short sentence. Be specific about the stakes — are we fighting to stay alive? Building a lead? Does goal differential matter?"""
        else:
            prompt = f"""{ctx}Score update in another pool game at OWHA U15B Provincials. We ({our_team_name}) are not playing.
Current: {score_str}{scenario_note}
Write exactly 1 short sentence about the concrete impact on us. Does this result help or hurt us? Does the margin matter for tiebreakers?"""
    else:
        return None

    return _call(prompt, max_tokens=100, label=f"event_impact_{event_type}")


def generate_bench_commentary(our_team_name, our_score, their_score, opp_name,
                               projections, other_game_info, standings_summary,
                               tiebreaker_state):
    """Generate LLM commentary for yellow bench situations.

    Called when the goalie-pull decision is unclear (tie might or might not
    advance us depending on other results and score margins).
    """
    other_str = 'No concurrent game.'
    if other_game_info:
        o = other_game_info
        other_str = (f"Concurrent game: {o['home']} {o['home_score']}-{o['away_score']} {o['away']} "
                    f"({'tied' if o['home_score'] == o['away_score'] else str(o['margin']) + '-goal lead'})")

    prompt = f"""You are the tactical analyst for {our_team_name} at OWHA U15B Provincials.
The coach needs to decide whether to pull the goalie late in a tied game.

CURRENT SITUATION:
- Our game: {our_team_name} {our_score} - {their_score} {opp_name} (TIED, late in the game)
- {other_str}

IF WE WIN: We advance in {projections['win']['advance']} of {projections['win']['total']} scenarios
IF WE TIE: We advance in {projections['tie']['advance']} of {projections['tie']['total']} scenarios
IF WE LOSE: We advance in {projections['loss']['advance']} of {projections['loss']['total']} scenarios

CURRENT STANDINGS: {standings_summary}

TIEBREAKER STATE (if we tie on points with another team):
{tiebreaker_state}

Pulling the goalie on a tie INCREASES chance of winning (extra attacker)
but SIGNIFICANTLY increases chance of giving up an empty-net goal (loss, 0 points).

Write 2-3 concise sentences explaining the key factors for this decision.
Focus on: Does the concurrent game's current score make a tie likely to work?
How much does a win vs tie change our outlook? Is the risk worth it?
Be practical and specific. This is for a coach glancing at a phone on the bench."""

    return _call(prompt, max_tokens=200, label='bench_commentary')


def generate_tiebreaker_health(our_team_name, standings_summary, yellow_count,
                                green_count, red_count, total, tiebreaker_state):
    """Generate LLM analysis of the tiebreaker health for yellow scenarios.

    Called when there are score-dependent or unresolved scenarios.
    Provides a summary of where we stand on the tiebreaker metrics
    that could decide those scenarios.
    """
    if yellow_count == 0:
        return None

    prompt = f"""You are analyzing tiebreaker scenarios for {our_team_name} at OWHA U15B Provincials.

=== SCENARIO BREAKDOWN ===
Total scenarios: {total}
Green (we win, deterministic): {green_count}
Yellow (depends on scores/tiebreakers): {yellow_count}
Red (we lose, deterministic): {red_count}

=== CURRENT STANDINGS ===
{standings_summary}

=== TIEBREAKER STATE (rules iii-iv that decide yellow scenarios) ===
{tiebreaker_state}

The yellow scenarios are decided by goal differential (rule iii) or goals
against (rule iv). These depend on actual game scores, not just W/L/T.

Write 2-3 concise sentences analyzing our tiebreaker health:
1. Are we currently ahead or behind on GD and GA compared to teams we might tie with?
2. How fragile is our position? (e.g., "one lopsided loss could erase our GD advantage")
3. What should we be aware of? (e.g., "every goal matters even in blowouts")

Be specific with numbers. Keep it practical. No filler."""

    return _call(prompt, max_tokens=200, label='tiebreaker_health')


def generate_correction_comment(game, old_score, new_score, our_team_name):
    """Generate a note about a post-game score correction."""
    home_name = game.get('home_name', game['home'])
    away_name = game.get('away_name', game['away'])

    return (f"Score correction: {home_name} vs {away_name} changed from "
            f"{old_score[0]}-{old_score[1]} to {new_score[0]}-{new_score[1]}. "
            f"Analysis has been updated.")


def evaluate_narrative(current_narrative, changes, prev_scenarios, curr_scenarios,
                       our_team, our_team_name):
    """Decide whether to regenerate the main page narrative. Returns True/False.

    Simple rule: only regenerate on game finals and corrections.
    Mid-game updates go to the LIVE banner and event log, not the narrative.
    """
    if not current_narrative:
        return True  # No narrative yet, always generate

    for c in changes:
        if c['type'] == 'game_final':
            log.info('Narrative eval: REGENERATE (game final)')
            return True
        if c['type'] == 'correction':
            log.info('Narrative eval: REGENERATE (score correction)')
            return True

    log.info('Narrative eval: KEEP (no finals or corrections)')
    return False


def generate_overall_narrative_with_context(prev_narrative, standings, scenarios,
                                             our_team_name, our_pool,
                                             qf_pool_standings, completed_games,
                                             upcoming_games, recent_changes=None,
                                             live_games=None):
    """Generate narrative with awareness of what was previously said."""
    standings_str = '\n'.join(
        f"  {r['name']} [provincial rank #{r.get('ranking', '?')}]: "
        f"{r['w']}W-{r['l']}L-{r['t']}T, {r['pts']}pts (GF={r['gf']} GA={r['ga']})"
        + (' ← US' if r.get('is_us') else '')
        for r in standings
    )

    completed_str = '\n'.join(
        f"  FINAL: {g['home_name']} {g['home_score']}-{g['away_score']} {g['away_name']}"
        for g in (completed_games or [])
    ) or '  No games completed yet.'

    live_str = '\n'.join(
        f"  IN PROGRESS: {g['home_name']} {g.get('home_score',0)}-{g.get('away_score',0)} {g['away_name']}"
        for g in (live_games or [])
    ) or ''

    upcoming_str = '\n'.join(
        f"  SCHEDULED: {g['home_name']} vs {g['away_name']} — {_format_date(g['date'])}"
        for g in (upcoming_games or [])[:4]
    ) or '  No upcoming games.'

    # Find our next scheduled game
    our_next_str = ''
    for g in (upcoming_games or []):
        if 'Kanata' in g.get('home_name', '') or 'Kanata' in g.get('away_name', ''):
            opp = g['away_name'] if 'Kanata' in g.get('home_name', '') else g['home_name']
            our_next_str = f'\nOUR NEXT GAME: {our_team_name} vs {opp} — {_format_date(g["date"])}'
            break

    qf_str = '\n'.join(
        f"  {r['name']} [#{r.get('ranking', '?')}]: {r['w']}W-{r['l']}L-{r['t']}T, {r['pts']}pts"
        for r in (qf_pool_standings or [])
    ) or '  No data yet.'

    if scenarios and scenarios.get('total', 0) > 1:
        det = scenarios.get('deterministic', scenarios['total'])
        unres = scenarios.get('unresolved', 0)
        sc_summary = (f"Out of {det} deterministic scenarios, {our_team_name} wins the pool in "
                      f"{scenarios['our_count']}. {unres} additional scenarios depend on score margins.")
    elif scenarios and scenarios.get('total') == 1:
        sc_summary = "All pool games are complete."
    else:
        sc_summary = "No scenario data available."

    changes_str = ''
    if recent_changes:
        changes_str = '\nWhat just happened:\n' + '\n'.join(f'  - {c}' for c in recent_changes)

    context_str = ''
    if prev_narrative:
        context_str = f"""
Here is what you wrote in your PREVIOUS update:
---
{prev_narrative}
---
Build on this. Reference what you said before where appropriate (e.g., "Earlier we said
we needed a win -- well, we got it!" or "The situation has changed since our last update").
Don't repeat yourself. Update the story, don't restart it.
"""

    prompt = f"""You are the tournament analyst for the {our_team_name} at the OWHA U15B Provincial Championships.
{context_str}

=== FACTS (use these exactly, do NOT invent schedule details) ===

Pool {our_pool} standings:
{standings_str}

Games in progress right now:
{live_str or '  None'}

Completed games:
{completed_str}

Upcoming scheduled games:
{upcoming_str}
{our_next_str}

Scenario analysis: {sc_summary}
{changes_str}

Quarterfinal opponent pool watch:
{qf_str}

=== END FACTS ===

Rankings: #1 strongest, higher numbers weaker. Pool C: #3 Kincardine, #18 Kanata (us), #25 Ennismore, #41 Windsor.
TONE: Never say "winnable" or "easy". Respect every opponent. Scenarios are counts, not predictions.

Write a structured update using **bold section headings**:
**Where We Stand** — 1-2 sentences on current position.
**Our Next Game** — 1-2 sentences. ONLY use the game from "OUR NEXT GAME" above.
**Around the Pool** — 1-2 sentences on other games.

CRITICAL: Only state facts from above. Do NOT guess schedule details.
Keep it under 150 words."""

    return _call(prompt, label="narrative_with_context")


def generate_pregame_talking_points(our_team_name, opponent_name, opponent_ranking,
                                     our_recent_results, our_pim, standings,
                                     scenarios_summary, what_at_stake):
    """Generate pre-game talking points for the team meeting."""
    results_str = '\n'.join(f'  - {r}' for r in (our_recent_results or [])) or '  No games yet.'

    prompt = f"""You are helping the coaching staff of the {our_team_name} (ranked #18 provincially)
prepare talking points for a pre-game team meeting at the OWHA U15B Provincials.

=== FACTS ===
Next opponent: {opponent_name} (provincial ranking #{opponent_ranking})
NOTE: Lower ranking number = stronger team. We are #18, they are #{opponent_ranking}.
{"We are ranked HIGHER (stronger) than them." if isinstance(opponent_ranking, int) and opponent_ranking > 18 else "They are ranked HIGHER (stronger) than us." if isinstance(opponent_ranking, int) and opponent_ranking < 18 else ""}

Our results at this tournament (most recent first):
{results_str}

Our penalty minutes so far: {our_pim}
Current standings: {standings}
Scenario analysis: {scenarios_summary}
What's at stake: {what_at_stake}
=== END FACTS ===

IMPORTANT: Pay close attention to the MOST RECENT result. If our last game was a
LOSS, the mindset should be about bouncing back and learning from it -- don't
ignore it or lead with earlier wins. But DO reference earlier positive results
as motivation ("Remember how strong we looked against Ennismore? We can play
like that again."). Think like a real coach: acknowledge what just happened,
then use the full tournament arc to motivate.

Write 3-4 short talking points using **bold headings** followed by 1-2 sentences each.
Format like:
**Mindset** — address the most recent result directly. Bounce back from a loss, or build on a win.
**Discipline** — mention PIMs if they were high. Otherwise a brief note on staying clean.
**The Opponent** — what to expect based on their ranking and results. Never say "easy" or "winnable".
**What We Need** — points, strategy, tiebreaker awareness if relevant.

Do NOT use bullet points or special characters. Just **bold heading** then plain text.
Practical, motivating, age-appropriate for U15 girls."""

    return _call(prompt, max_tokens=300, label="pregame_talking_points")


def generate_don_cherry(context, our_team_name='Kanata Rangers'):
    """Generate a Don Cherry-style commentary snippet."""
    prompt = f"""You are Don Cherry from Coach's Corner on Hockey Night in Canada.
You're commenting on the {our_team_name} U15B girls team at the OWHA Provincial Championships.

Current situation: {context}

Give exactly 1-2 short sentences in Don Cherry's voice. Rules:
- Open with "I'll tell ya..." or "Now listen..." or "You kids out there..."
- Short, punchy -- thinking out loud
- Champion toughness, effort, heart
- If they lost: encouraging, "dust yourself off"
- If they won: fired up, "THAT'S how you play hockey!"
- These are U15 GIRLS -- encouraging about girls growing the game
- Do NOT use the phrase "rankings don't score goals" -- be more original
- Keep it brief and fun. Less is more. Maximum 2 sentences."""

    return _call(prompt, max_tokens=100, label="don_cherry")


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
