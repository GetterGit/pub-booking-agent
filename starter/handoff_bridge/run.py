"""Ex7 — handoff bridge runner.

Two modes:
  * default (offline): scripted FakeLLMClient + stdlib mock Rasa
      Two-round round-trip:
        round 1: loop picks haymarket_tap, structured rejects (party=12 > cap=8)
        round 2: loop picks royal_oak (16 seats), structured accepts
  * --real: live Nebius LLM in the loop + real Rasa on localhost:5005
      Same scenario, but the LLM must figure out the retry on its own
      after seeing the rejection reason in PRIOR CONTEXT.
"""

from __future__ import annotations

import asyncio
import json
import sys

from sovereign_agent._internal.llm_client import (
    FakeLLMClient,
    OpenAICompatibleClient,
    ScriptedResponse,
    ToolCall,
)
from sovereign_agent._internal.paths import example_sessions_dir
from sovereign_agent.executor import DefaultExecutor
from sovereign_agent.planner import DefaultPlanner
from sovereign_agent.session.directory import create_session

from starter.edinburgh_research.stateful_loop import StatefulLoopHalf
from starter.edinburgh_research.tools import build_tool_registry
from starter.handoff_bridge.bridge import HandoffBridge
from starter.rasa_half.structured_half import RasaStructuredHalf, spawn_mock_rasa

_EX7_TASK = (
    "Book an Edinburgh pub for an event, going through the structured half "
    "for policy approval.\n\n"
    "Initial requirements:\n"
    "  - party size: 12\n"
    "  - date: 2026-04-25 (a Saturday)\n"
    "  - time: 19:30\n"
    "  - area: near Haymarket, Edinburgh\n\n"
    "PROCESS (the only valid procedure per round):\n"
    "  1. Call venue_search(near='Haymarket', party_size=<current>, budget_max_gbp=2000)\n"
    "  2. From results, pick the FIRST venue whose seats_available_evening >= party_size.\n"
    "  3. Call handoff_to_structured with:\n"
    "       reason='loop identified a candidate venue; passing to structured for confirmation'\n"
    "       context='party of <N> near Haymarket on 2026-04-25 19:30; chosen venue <name>'\n"
    "       data={\n"
    "         'action': 'confirm_booking',\n"
    "         'venue_id': <id from venue_search results[].id>,\n"
    "         'date': '2026-04-25',\n"
    "         'time': '19:30',\n"
    "         'party_size': '<N>',\n"
    "         'deposit': '£0',\n"
    "       }\n"
    "  4. STOP. Do not call any tool after handoff_to_structured. The "
    "structured half decides what comes next.\n\n"
    "HARD RULES:\n"
    "  - Do NOT call complete_task — only the structured half can complete a booking.\n"
    "  - Do NOT call venue_search more than once per round.\n"
    "  - Do NOT change date or time.\n"
    "  - The structured half rejects parties > 8 and deposits > £300.\n\n"
    "RETRY BEHAVIOR — when PRIOR CONTEXT shows a previous rejection:\n"
    "  - If the rejection reason mentions 'party' / 'too_large' / cap: re-run "
    "venue_search with party_size=6 and pick a new venue.\n"
    "  - If the rejection reason mentions 'deposit' / 'too_high': pick a "
    "cheaper venue from PRIOR CONTEXT's existing results.\n"
    "  - Always finish the round with another handoff_to_structured call."
)


def _build_fake_client_two_rounds() -> FakeLLMClient:
    """Round 1: plan → venue_search → handoff_to_structured (haymarket_tap)
    Round 2: plan → venue_search → handoff_to_structured (royal_oak)"""
    plan_r1 = json.dumps(
        [
            {
                "id": "sg_1",
                "description": "find venue near haymarket for 12",
                "success_criterion": "candidate identified",
                "estimated_tool_calls": 2,
                "depends_on": [],
                "assigned_half": "loop",
            }
        ]
    )
    # round 2 — loop gets rejection reason, retries with different area
    plan_r2 = json.dumps(
        [
            {
                "id": "sg_1",
                "description": "retry with larger venue after rejection",
                "success_criterion": "different venue with enough seats",
                "estimated_tool_calls": 2,
                "depends_on": [],
                "assigned_half": "loop",
            }
        ]
    )

    return FakeLLMClient(
        [
            # === ROUND 1 ===
            ScriptedResponse(content=plan_r1),  # planner turn 1
            ScriptedResponse(  # executor turn 1: search
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="venue_search",
                        arguments={"near": "Haymarket", "party_size": 12, "budget_max_gbp": 2000},
                    )
                ]
            ),
            ScriptedResponse(  # executor turn 2: handoff
                tool_calls=[
                    ToolCall(
                        id="c2",
                        name="handoff_to_structured",
                        arguments={
                            "reason": "loop half identified a candidate venue; passing to structured half for confirmation under policy rules",
                            "context": "party of 12 near Haymarket on 2026-04-25 19:30; chosen venue haymarket_tap",
                            "data": {
                                "action": "confirm_booking",
                                "venue_id": "Haymarket Tap",
                                "date": "2026-04-25",
                                "time": "19:30",
                                "party_size": "12",
                                "deposit": "£0",
                            },
                        },
                    )
                ]
            ),
            # === ROUND 2 (after reverse handoff from structured rejecting party=12) ===
            ScriptedResponse(content=plan_r2),  # planner turn 2
            ScriptedResponse(  # executor turn 1: new search with smaller party
                tool_calls=[
                    ToolCall(
                        id="c3",
                        name="venue_search",
                        arguments={"near": "Old Town", "party_size": 6, "budget_max_gbp": 2000},
                    )
                ]
            ),
            ScriptedResponse(  # executor turn 2: handoff royal_oak with party=6
                tool_calls=[
                    ToolCall(
                        id="c4",
                        name="handoff_to_structured",
                        arguments={
                            "reason": "retry after reverse handoff — scaled down to fit policy",
                            "context": "party was originally 12; rejected; re-proposing party of 6 at royal_oak (16 seats)",
                            "data": {
                                "action": "confirm_booking",
                                "venue_id": "The Royal Oak",
                                "date": "2026-04-25",
                                "time": "19:30",
                                "party_size": "6",
                                "deposit": "£0",
                            },
                        },
                    )
                ]
            ),
            # Safety pad — extra benign responses in case the framework makes
            # additional client.chat calls per round (retries, capability
            # probes, parse re-attempts). Documented in
            # docs/real-mode-failures.md §Ex7 — FakeLLMClient ran out of
            # scripted responses. Unused extras are harmless.
            *[ScriptedResponse(content="{}") for _ in range(6)],
        ]
    )


async def run_scenario(real: bool) -> int:
    with example_sessions_dir("ex7-handoff-bridge", persist=real) as sessions_root:
        session = create_session(
            scenario="ex7-handoff-bridge",
            task=_EX7_TASK,
            sessions_dir=sessions_root,
        )
        print(f"Session {session.session_id}")
        print(f"  dir: {session.directory}")

        # Structured half: stdlib mock unless --real (then real Rasa on :5005)
        server = None
        if not real:
            server, _thread, mock_url = spawn_mock_rasa(port=5906)
            rasa_half = RasaStructuredHalf(rasa_url=mock_url)
        else:
            rasa_half = RasaStructuredHalf()

        # Loop half client: scripted offline, live Nebius when --real
        if real:
            from sovereign_agent.config import Config

            cfg = Config.from_env()
            print(f"  LLM: {cfg.llm_base_url} (live)")
            print(f"  planner:  {cfg.llm_planner_model}")
            print(f"  executor: {cfg.llm_executor_model}")
            client = OpenAICompatibleClient(
                base_url=cfg.llm_base_url,
                api_key_env=cfg.llm_api_key_env,
            )
            planner_model = cfg.llm_planner_model
            executor_model = cfg.llm_executor_model
        else:
            print("  LLM: FakeLLMClient (offline, scripted)")
            client = _build_fake_client_two_rounds()
            planner_model = executor_model = "fake"

        tools = build_tool_registry(session)
        # StatefulLoopHalf is required for --real: the planner may decompose
        # the task into multiple subgoals, and each subgoal executes in a
        # fresh executor context. Without `task_constants` + PRIOR CONTEXT
        # injection, the LLM forgets the booking rules between subgoals and
        # spirals (same failure mode we hit in Ex5).
        loop_half = StatefulLoopHalf(
            planner=DefaultPlanner(model=planner_model, client=client),
            executor=DefaultExecutor(model=executor_model, client=client, tools=tools),  # type: ignore[arg-type]
            task_constants=_EX7_TASK,
        )
        bridge = HandoffBridge(
            loop_half=loop_half,
            structured_half=rasa_half,
            max_rounds=3,
        )

        try:
            result = await bridge.run(session, {"task": _EX7_TASK})
        finally:
            if server is not None:
                server.shutdown()

        print(f"\nBridge outcome: {result.outcome}")
        print(f"  rounds: {result.rounds}")
        print(f"  summary: {result.summary}")

        if real:
            print(f"\nArtifacts persist at: {session.directory}")
            print(f"📜 Narrate this run: make narrate SESSION={session.session_id}")

        return 0 if result.outcome == "completed" else 1


def main() -> None:
    real = "--real" in sys.argv
    sys.exit(asyncio.run(run_scenario(real=real)))


if __name__ == "__main__":
    main()
