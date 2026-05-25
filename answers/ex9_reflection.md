# Ex9 — Reflection

## Q1 — Planner handoff decision

### Your answer

In my Ex7 session the round-1 planner decomposed the booking task into two subgoals and explicitly typed the second one as `assigned_half: "structured"`. In the subgoal 2:

> "description": "Hand off to structured half for booking confirmation with selected venue", "depends_on": ["sg_1"], "assigned_half": "structured"

The signal that caused the decision was not emergent reasoning — it was the task spec. The `_EX7_TASK` constant in `starter/handoff_bridge/run.py` declares a two-step PROCESS block: "Step 1 — find candidate venues via the loop half; Step 2 — call handoff_to_structured for booking validation." The planner mirrored that `step→half` mapping verbatim into assigned_half and depends_on. 

### Citation

- `artifacts/examples/ex7-handoff-bridge/sess_4ebfe1d1362e/logs/tickets/tk_b50c58c5/raw_output.json`
- `artifacts/examples/ex7-handoff-bridge/sess_4ebfe1d1362e/logs/tickets/tk_b50c58c5/manifest.json`
- `artifacts/examples/ex7-handoff-bridge/sess_4ebfe1d1362e/logs/trace.jsonl`
- `starter/handoff_bridge/run.py`

---

## Q2 — Dataflow integrity catch

### Your answer

My Ex5 run produced a fact-complete flyer. Every visible value traces back to exactly one tool call: `Venue: Haymarket Tap` comes from `venue_search(near=Haymarket, party_size=6)` returning 1 result, `Weather: cloudy, 12°C` from `get_weather(city=edinburgh, date=2026-04-25)`, and `Total Cost: £556 / Deposit: £111` from `calculate_cost(venue_id=haymarket_tap, party_size=6, ...)`. Each fact has a one-line provenance entry in `_TOOL_CALL_LOG`. A human reviewer reading the rendered flyer sees a plausible venue, a plausible deposit in the right order of magnitude, and a plausible weather phrase — they cannot independently verify that £111 is the number calculate_cost actually returned versus a number the LLM hallucinated near the right range.

The specific scenario my `verify_dataflow` would catch and a human would not: run `make ex5-real` to populate the tool-call log, then before the integrity check fires, mutate one digit in the in-memory flyer string — £111 becomes £150. `verify_dataflow` walks the flyer's data-testid values and asserts each appears in some `_TOOL_CALL_LOG[i].output`. 150 matches zero tool outputs (only `calculate_cost(haymarket_tap, 6) → 111` exists in the log), so the check fails with `fact_not_in_tool_log: deposit_required_gbp=150` and refuses to mark the session complete. The same mechanism catches any single-fact corruption — price swap, venue rename, date drift, weather mismatch — because every flyer fact is forced through the `_TOOL_CALL_LOG` cross-reference rather than re-derived from prose. A reviewer reading `flyer.html` in a few seconds cannot do that join by eye; the verifier does it deterministically against the trace.

### Citation

- `artifacts/examples/ex5-edinburgh-research/sess_c2f83c251524/workspace/flyer.html`
- `artifacts/examples/ex5-edinburgh-research/sess_c2f83c251524/logs/trace.jsonl`
- `starter/edinburgh_research/integrity.py`

---

## Q3 — Removing one framework primitive

### Your answer

#### Failure mode

The loop half's LLM hallucinates a fact in the booking. Concretely: in my Ex5 run `venue_search(near=Haymarket, party_size=6)` returned `1 result(s)` (Haymarket Tap) and `calculate_cost(haymarket_tap, 6)` returned total £556, deposit £111. Imagine the LLM, anchoring on a number from its pretraining or confabulating mid-ReAct, calls generate_flyer with total_gbp: 450 instead of 556. The tool happily writes whatever it's given to `workspace/flyer.html`. The executor then calls `complete_task`. Every tool returned `success: True`. The session transitions to state: success — visible at `tk_d4404e47/state.json`. A human reading the flyer sees a plausible venue and a plausible price; the customer is quoted £450 and arrives expecting that bill. The agent has "succeeded" but the booking is wrong. This is the failure class that scales worst with traffic: silent, indistinguishable from a healthy session by any single-process signal.

#### Primitive: ticket state machine

Every ticket has a strict lifecycle — `created → running → success | failed` — visible in `state.json` files. Sessions can only transition to complete if all required tickets have reached success. Wrap the dataflow integrity check as its own ticket: verify_dataflow ingests `_TOOL_CALL_LOG` and the rendered flyer, then transitions its ticket to success only when every flyer fact has a tool-output provenance. Hallucinated value `total_gbp=450` matches zero entries in `_TOOL_CALL_LOG` (only 556 is recorded) → integrity-check ticket transitions to failed. The session state machine refuses the executing → complete transition because a required ticket is failed, and the session itself becomes state: failed with a structured reason. The hallucination converts from silent flyer corruption into a queryable session outcome: a fleet aggregator counting `session.state == failed` AND `failed_ticket.operation == "integrity.verify_dataflow"` returns a direct hallucination-rate metric instead of grepping flyer prose. One failure (LLM fact hallucination), one primitive (ticket state machine).

### Citation

- `starter/edinburgh_research/integrity.py`
- `artifacts/examples/ex5-edinburgh-research/sess_c2f83c251524/logs/tickets/tk_d4404e47/state.json`
- `artifacts/examples/ex5-edinburgh-research/sess_c2f83c251524/logs/tickets/tk_d4404e47/manifest.json`
- `artifacts/examples/ex5-edinburgh-research/sess_c2f83c251524/logs/trace.jsonl`
- `artifacts/examples/ex5-edinburgh-research/sess_c2f83c251524/workspace/flyer.html`
