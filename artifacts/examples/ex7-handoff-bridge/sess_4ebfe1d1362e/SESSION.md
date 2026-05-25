# Session sess_4ebfe1d1362e

**Scenario:** ex7-handoff-bridge
**Created:** 2026-05-24T12:41:25.773056+00:00

## Your task

(The loop half reads this file on every turn. The initial task description
has been written below by the orchestrator when the session was created.
Additional per-session instructions — constraints, identity, voice — can
be added by the scenario author.)

## Task description

Book an Edinburgh pub for an event, going through the structured half for policy approval.

Initial requirements:
  - party size: 12
  - date: 2026-04-25 (a Saturday)
  - time: 19:30
  - area: near Haymarket, Edinburgh

PROCESS (the only valid procedure per round):
  1. Call venue_search(near='Haymarket', party_size=<current>, budget_max_gbp=2000)
  2. From results, pick the FIRST venue whose seats_available_evening >= party_size.
  3. Call handoff_to_structured with:
       reason='loop identified a candidate venue; passing to structured for confirmation'
       context='party of <N> near Haymarket on 2026-04-25 19:30; chosen venue <name>'
       data={
         'action': 'confirm_booking',
         'venue_id': <id from venue_search results[].id>,
         'date': '2026-04-25',
         'time': '19:30',
         'party_size': '<N>',
         'deposit': '£0',
       }
  4. STOP. Do not call any tool after handoff_to_structured. The structured half decides what comes next.

HARD RULES:
  - Do NOT call complete_task — only the structured half can complete a booking.
  - Do NOT call venue_search more than once per round.
  - Do NOT change date or time.
  - The structured half rejects parties > 8 and deposits > £300.

RETRY BEHAVIOR — when PRIOR CONTEXT shows a previous rejection:
  - If the rejection reason mentions 'party' / 'too_large' / cap: re-run venue_search with party_size=6 and pick a new venue.
  - If the rejection reason mentions 'deposit' / 'too_high': pick a cheaper venue from PRIOR CONTEXT's existing results.
  - Always finish the round with another handoff_to_structured call.

## Constraints

- Be honest when you do not know something.
- Prefer reading memory over guessing.
- When the task is ambiguous, ask for clarification rather than inventing an answer.
