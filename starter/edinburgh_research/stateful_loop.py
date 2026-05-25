"""StatefulLoopHalf — a LoopHalf that threads tool outputs forward.

The framework's default LoopHalf calls executor.execute(sg) for each
subgoal in isolation; each executor invocation gets a fresh message
history seeded only with the subgoal's description + success_criterion.
That means sg_3 ("calculate cost") cannot see sg_1's venue_search
output, so it panics and hands off.

StatefulLoopHalf mirrors LoopHalf's API but accumulates tool results
across subgoals and injects a "PRIOR CONTEXT" block into each subgoal's
description before calling executor.execute. The planner can decompose
the task into any number of subgoals — earlier outputs always reach
later executors.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace

from sovereign_agent.executor import DefaultExecutor, ExecutorResult
from sovereign_agent.halves import HalfResult
from sovereign_agent.planner import DefaultPlanner
from sovereign_agent.session.directory import Session
from sovereign_agent.session.state import now_utc

from starter.edinburgh_research.integrity import _TOOL_CALL_LOG, ToolCallRecord

log = logging.getLogger(__name__)


_MAX_CONTEXT_VALUE_LEN = 1500


class StatefulLoopHalf:
    """Drop-in replacement for sovereign_agent.halves.loop.LoopHalf.

    Identical control flow, plus: before each subgoal runs, all prior
    tool outputs (from _TOOL_CALL_LOG) are injected into the subgoal's
    description so the executor LLM can chain on them.
    """

    name = "loop"

    def __init__(
        self,
        *,
        planner: DefaultPlanner,
        executor: DefaultExecutor,
        task_constants: str | None = None,
    ) -> None:
        self.planner = planner
        self.executor = executor
        self.task_constants = task_constants

    def discover(self) -> dict:
        return {
            "name": self.name,
            "kind": "half",
            "description": "Loop half with tool-output state threaded across subgoals.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "context": {"type": "object"},
                },
                "required": ["task"],
            },
            "returns": {"type": "object"},
            "error_codes": ["SA_VAL_INVALID_PLANNER_OUTPUT"],
            "examples": [],
            "version": "0.1.0",
            "metadata": {"variant": "stateful"},
        }

    async def run(self, session: Session, input_payload: dict) -> HalfResult:
        task = input_payload.get("task") or ""
        context = input_payload.get("context") or {}

        session.append_trace_event(
            {
                "event_type": "planner.called",
                "actor": self.planner.name,
                "timestamp": now_utc().isoformat(),
                "payload": {"task_preview": task[:200]},
            }
        )
        subgoals = await self.planner.plan(task, context, session)
        session.append_trace_event(
            {
                "event_type": "planner.produced_subgoals",
                "actor": self.planner.name,
                "timestamp": now_utc().isoformat(),
                "payload": {"num_subgoals": len(subgoals)},
            }
        )
        session.update_state(
            state="executing",
            planner={"subgoals": [sg.to_dict() for sg in subgoals]},
        )

        executor_results: list[ExecutorResult] = []
        log_cursor = len(_TOOL_CALL_LOG)

        for sg in subgoals:
            if sg.assigned_half != "loop":
                return HalfResult(
                    success=True,
                    output={
                        "subgoal_id": sg.id,
                        "assigned_half": sg.assigned_half,
                        "executor_results": [_execresult_to_dict(r) for r in executor_results],
                    },
                    summary=f"subgoal {sg.id} is assigned to {sg.assigned_half}; handing off",
                    next_action=(
                        "handoff_to_structured"
                        if sg.assigned_half == "structured"
                        else "handoff_to_loop"
                    ),
                    handoff_payload={
                        "subgoal": sg.to_dict(),
                        "prior_results": [_execresult_to_dict(r) for r in executor_results],
                    },
                )

            prior_records = _TOOL_CALL_LOG[:log_cursor]
            sections: list[str] = [sg.description]

            if self.task_constants:
                sections.append(
                    "TASK CONSTANTS — fixed for the entire task. NEVER override, "
                    "NEVER ask for these, NEVER hand off claiming they are missing. "
                    "Use these exact values in every tool call:\n"
                    f"{self.task_constants}"
                )

            if prior_records:
                ctx_block = _format_prior_context(prior_records)
                sections.append(
                    "PRIOR CONTEXT — tool results gathered in earlier subgoals "
                    "of this same task. Reuse these values directly; do NOT "
                    "re-query, do NOT ask for them, do NOT hand off claiming "
                    "they are missing:\n"
                    f"{ctx_block}"
                )

            if len(sections) > 1:
                augmented_sg = replace(sg, description="\n\n".join(sections))
                session.append_trace_event(
                    {
                        "event_type": "stateful_loop.context_injected",
                        "actor": self.name,
                        "timestamp": now_utc().isoformat(),
                        "payload": {
                            "subgoal_id": sg.id,
                            "prior_tool_calls": len(prior_records),
                            "task_constants_injected": bool(self.task_constants),
                        },
                    }
                )
            else:
                augmented_sg = sg

            result = await self.executor.execute(augmented_sg, session)
            executor_results.append(result)
            log_cursor = len(_TOOL_CALL_LOG)

            if result.handoff_requested:
                return HalfResult(
                    success=True,
                    output={
                        "subgoal_id": sg.id,
                        "executor_results": [_execresult_to_dict(r) for r in executor_results],
                    },
                    summary=f"executor requested handoff to structured from {sg.id}",
                    next_action="handoff_to_structured",
                    handoff_payload=result.handoff_payload or {},
                )
            if not result.success:
                return HalfResult(
                    success=False,
                    output={
                        "subgoal_id": sg.id,
                        "executor_results": [_execresult_to_dict(r) for r in executor_results],
                    },
                    summary=f"executor failed on {sg.id}: {result.final_answer}",
                    next_action="escalate",
                )

        final_answer = executor_results[-1].final_answer if executor_results else ""
        return HalfResult(
            success=True,
            output={
                "final_answer": final_answer,
                "executor_results": [_execresult_to_dict(r) for r in executor_results],
            },
            summary=(
                f"stateful loop half completed {len(executor_results)} subgoal(s); "
                f"final answer: {final_answer[:120]}"
            ),
            next_action="complete",
        )


def _format_prior_context(records: list[ToolCallRecord]) -> str:
    lines: list[str] = []
    for i, rec in enumerate(records, 1):
        args = _truncate_json(rec.arguments)
        output = _truncate_json(rec.output)
        lines.append(f"  {i}. {rec.tool_name}({args}) -> {output}")
    return "\n".join(lines)


def _truncate_json(obj: object) -> str:
    try:
        s = json.dumps(obj, default=str, ensure_ascii=False)
    except Exception:
        s = str(obj)
    if len(s) > _MAX_CONTEXT_VALUE_LEN:
        return s[:_MAX_CONTEXT_VALUE_LEN] + "...(truncated)"
    return s


def _execresult_to_dict(r: ExecutorResult) -> dict:
    return {
        "subgoal_id": r.subgoal_id,
        "success": r.success,
        "final_answer": r.final_answer,
        "turns_used": r.turns_used,
        "tool_calls_made": r.tool_calls_made,
        "handoff_requested": r.handoff_requested,
    }


__all__ = ["StatefulLoopHalf"]
