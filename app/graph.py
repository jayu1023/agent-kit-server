"""The LangGraph agent: plan -> act -> observe -> reflect.

**Why four nodes and not one ReAct node.** A single node that thinks, calls a
tool and loops is less code and strictly worse here, for three reasons:

1. Each phase emits a *different* `agent_kit` event. `plan` emits a plan tree,
   `act` emits a `pending` tool call, `observe` emits the terminal one, and
   `reflect` emits the thinking bubble. Collapsing them means one node that
   knows about every widget.
2. The human-in-the-loop interrupt has to land on a boundary where the state is
   complete and resumable. `act` is that boundary: the tool and its arguments
   are decided, nothing has executed. Interrupting mid-ReAct means checkpointing
   a half-made decision.
3. `reflect` is where the loop guard lives. A step limit enforced inside the
   same node that does the work is a limit you can forget to check on one of
   the paths out.

**Guards, and where each one lives** -- deliberately in the graph, not bolted on:

| guard | where | what it stops |
|---|---|---|
| max steps | `reflect` | an agent that never terminates |
| tool timeout | `guards.run_tool` | one hung tool holding a request open |
| bounded retry, permanent/transient split | `guards.run_tool` | retrying a bad argument forever |
| approval before writes | `act`, via `interrupt()` | a side effect nobody sanctioned |
| tool allowlist + arg schema validation | `planner._validate` | a hallucinated tool or argument executing |
| no cascading failure | `reflect` | one failed step aborting a usable answer |
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from .config import settings
from .events import (
    Citation,
    PlanStepState,
    ToolCall,
    ToolCallState,
    citations_event,
    plan_event,
    thinking_event,
    tool_call_event,
)
from .guards import run_tool
from .planner import make_plan, resolve_args
from .state import AgentState, set_step_state
from .tools import TOOLS_BY_NAME, is_write_tool
from .tracing import set_attributes, span


def _emit(payload: dict[str, Any]) -> None:
    """Push one `agent_kit` event to the SSE stream.

    `get_stream_writer()` is a no-op outside a streaming run, so the same nodes
    work under `ainvoke` in tests without a branch for it.
    """
    get_stream_writer()(payload)


def _run_id(config: RunnableConfig) -> str:
    return ((config or {}).get("configurable") or {}).get("thread_id", "run")


def _call_id(config: RunnableConfig, step_id: str, tool: str) -> str:
    """Stable id for one tool call, unique across runs.

    agent_kit keys its widgets on `ToolCall.id`, so pending -> running ->
    success must carry the *same* id or the Flutter list renders three cards
    instead of advancing one.

    The run id is in there because `step_id` alone is only unique *within* a
    run: two runs both start at `s1`, and a client showing more than one run in
    the same view would collide their cards. Found by the Dart wire test, which
    decodes two concatenated runs from one fixture.
    """
    return f"{_run_id(config)}:{step_id}-{tool}"


# --------------------------------------------------------------------------- #
# nodes
# --------------------------------------------------------------------------- #
def plan(state: AgentState) -> dict[str, Any]:
    question = state["question"]
    with span("agent.plan", **{"agent.question_chars": len(question)}) as plan_span:
        steps, actions, planner_used = make_plan(question)
        set_attributes(
            plan_span,
            **{"agent.planner": planner_used,
               "agent.steps": len(steps),
               "agent.tools": ",".join(a["tool"] for a in actions)},
        )

    _emit(thinking_event(f"Planning with the {planner_used} planner…"))
    _emit(plan_event(steps))

    return {
        "plan": [s.wire() for s in steps],
        "actions": actions,
        "cursor": 0,
        "steps_taken": 0,
    }


def act(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Resolve the current step's arguments, ask for approval if it writes.

    Nothing here executes a tool. That separation is what makes the interrupt
    safe: on resume, the node re-runs from the top, and re-running `act` has no
    side effects.
    """
    cursor = state["cursor"]
    action = state["actions"][cursor]
    step_id, tool_name = action["step_id"], action["tool"]
    plan_wire = set_step_state(state["plan"], step_id, PlanStepState.running.value)
    _emit(plan_event([_as_step(s) for s in plan_wire]))

    # Managed by hand rather than with `with`, because interrupt() unwinds the
    # stack from the middle of this function and a context manager would have
    # its __exit__ skipped. `closed` guards against the double-close that
    # produced "Setting attribute on ended span" warnings on the resume pass.
    act_span_cm = span(
        "agent.act",
        **{"agent.step_id": step_id, "agent.tool": tool_name,
           "agent.requires_approval": is_write_tool(tool_name)},
    )
    act_span = act_span_cm.__enter__()
    closed = False

    def close_act(**attributes: Any) -> None:
        nonlocal closed
        if closed:
            return
        set_attributes(act_span, **attributes)
        act_span_cm.__exit__(None, None, None)
        closed = True

    try:
        args = resolve_args(action, state["question"], list(state.get("observations", [])))
    except Exception as exc:
        # A resolver failure is this step's failure, not the run's. reflect()
        # decides whether the answer is still worth producing.
        close_act(**{"agent.outcome": "arg_resolution_failed"})
        _emit(
            tool_call_event(
                ToolCall(
                    id=_call_id(config, step_id, tool_name),
                    name=tool_name,
                    state=ToolCallState.failed,
                    error=f"could not resolve arguments: {exc}",
                )
            )
        )
        return {
            "plan": set_step_state(plan_wire, step_id, PlanStepState.failed.value),
            "observations": [
                {
                    "step_id": step_id,
                    "tool": tool_name,
                    "ok": False,
                    "result": None,
                    "error": f"argument resolution failed: {exc}",
                    "duration_ms": 0,
                    "attempts": 0,
                }
            ],
            "resolved_args": None,
        }

    if is_write_tool(tool_name):
        # IDEMPOTENCY: after an interrupt, LangGraph re-runs this node from the
        # top on resume -- everything above `interrupt()` executes twice. So the
        # only thing emitted here is a tool_call whose id is stable
        # (`_call_id`), which agent_kit renders as the *same* pending card both
        # times. A thinking bubble was emitted here originally and showed up
        # twice in the client, because ThinkingStep has no id to dedupe on.
        _emit(
            tool_call_event(
                ToolCall(
                    id=_call_id(config, step_id, tool_name),
                    name=tool_name,
                    state=ToolCallState.pending,
                    args=args,
                )
            )
        )
        # Execution stops here. The checkpointer persists state; the process may
        # even restart. `POST /runs/{id}/approve` resumes with Command(resume=...).
        close_act(**{"agent.outcome": "awaiting_approval"})
        decision = interrupt(
            {
                "kind": "approval_required",
                "step_id": step_id,
                "tool": tool_name,
                "args": args,
                "reason": "This tool writes to disk.",
            }
        )
        # The run resumes here in a NEW process span: interrupt() unwinds the
        # stack, so the span opened before it cannot survive. This is why
        # approval shows as two traces, not one -- which is honest, because it
        # genuinely is two executions of this node.
        approved = bool(decision.get("approved")) if isinstance(decision, dict) else bool(decision)
        with span(
            "agent.approval",
            **{"agent.step_id": step_id, "agent.tool": tool_name, "agent.approved": approved},
        ):
            pass
        if not approved:
            note = (decision or {}).get("note", "") if isinstance(decision, dict) else ""
            _emit(
                tool_call_event(
                    ToolCall(
                        id=_call_id(config, step_id, tool_name),
                        name=tool_name,
                        state=ToolCallState.cancelled,
                        args=args,
                        error=note or "rejected by reviewer",
                    )
                )
            )
            close_act(**{"agent.outcome": "rejected"})
            return {
                "plan": set_step_state(plan_wire, step_id, PlanStepState.skipped.value),
                "resolved_args": None,
                "rejection_note": note or "rejected by reviewer",
                "observations": [
                    {
                        "step_id": step_id,
                        "tool": tool_name,
                        "ok": False,
                        "result": None,
                        "error": "rejected by reviewer",
                        "duration_ms": 0,
                        "attempts": 0,
                    }
                ],
            }

    _emit(
        tool_call_event(
            ToolCall(
                id=_call_id(config, step_id, tool_name),
                name=tool_name,
                state=ToolCallState.running,
                args=args,
            )
        )
    )
    close_act(**{"agent.outcome": "ready"})
    return {"plan": plan_wire, "resolved_args": args}


async def observe(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Execute the resolved tool call under the guards and record what happened."""
    cursor = state["cursor"]
    action = state["actions"][cursor]
    step_id, tool_name = action["step_id"], action["tool"]
    args = state.get("resolved_args")

    if args is None:
        # act() already failed or was rejected; nothing to execute.
        return {"steps_taken": state.get("steps_taken", 0) + 1}

    cfg = settings()
    with span(
        f"tool.{tool_name}",
        **{"tool.name": tool_name, "tool.timeout_s": cfg.tool_timeout_s,
           "tool.max_retries": cfg.tool_retries},
    ) as tool_span:
        outcome = await run_tool(
            TOOLS_BY_NAME[tool_name],
            args,
            timeout_s=cfg.tool_timeout_s,
            retries=cfg.tool_retries,
            backoff_s=cfg.retry_backoff_s,
        )
        # `attempts` is the attribute worth having: it is the only place a
        # silent retry becomes visible after the fact.
        set_attributes(
            tool_span,
            **{"tool.ok": outcome.ok, "tool.attempts": outcome.attempts,
               "tool.duration_ms": outcome.duration_ms, "tool.error": outcome.error},
        )

    _emit(
        tool_call_event(
            ToolCall(
                id=_call_id(config, step_id, tool_name),
                name=tool_name,
                state=ToolCallState.success if outcome.ok else ToolCallState.failed,
                args=args,
                result=outcome.result,
                error=outcome.error,
                duration_ms=outcome.duration_ms,
            )
        )
    )

    citations = _citations_from(tool_name, outcome.result) if outcome.ok else []
    if citations:
        _emit(citations_event(citations))

    # Emit the plan transition, do not only store it. Returning the new plan in
    # state updates the checkpoint but sends nothing to the client, which left
    # the last step rendered as `running` forever in the Flutter timeline --
    # caught by the Dart wire test asserting every step reaches a terminal state.
    plan_wire = set_step_state(
        state["plan"],
        step_id,
        (PlanStepState.done if outcome.ok else PlanStepState.failed).value,
    )
    _emit(plan_event([_as_step(s) for s in plan_wire]))

    return {
        "plan": plan_wire,
        "observations": [
            {
                "step_id": step_id,
                "tool": tool_name,
                "ok": outcome.ok,
                "result": outcome.result,
                "error": outcome.error,
                "duration_ms": outcome.duration_ms,
                "attempts": outcome.attempts,
            }
        ],
        "citations": [c.wire() for c in citations],
        "steps_taken": state.get("steps_taken", 0) + 1,
        "resolved_args": None,
    }


def reflect(state: AgentState) -> dict[str, Any]:
    """Advance the cursor and decide whether to keep going.

    This is the only node that may set `halt_reason`, so there is exactly one
    place where "why did the agent stop" is decided.
    """
    cursor = state["cursor"] + 1
    taken = state.get("steps_taken", 0)
    total = len(state.get("actions", []))
    observations = list(state.get("observations", []))

    with span(
        "agent.reflect",
        **{"agent.steps_taken": taken, "agent.total_steps": total,
           "agent.failed_steps": sum(1 for o in observations if not o["ok"]),
           "agent.budget": settings().max_steps},
    ):
        pass

    if taken >= settings().max_steps:
        _emit(
            thinking_event(
                f"Step budget reached ({taken}/{settings().max_steps}). "
                "Answering from what has been gathered."
            )
        )
        return {"cursor": cursor, "halt_reason": "max_steps"}

    if cursor >= total:
        return {"cursor": cursor, "halt_reason": "completed"}

    failed = sum(1 for o in observations if not o["ok"])
    next_label = next(
        (s["label"] for s in state["plan"] if s["id"] == state["actions"][cursor]["step_id"]),
        "the next step",
    )
    note = f" {failed} step(s) failed so far; continuing." if failed else ""
    _emit(thinking_event(f"Step {cursor} of {total} done.{note} Next: {next_label.lower()}."))
    return {"cursor": cursor}


def answer(state: AgentState) -> dict[str, Any]:
    """Compose the final text.

    Deliberately extractive rather than an LLM call: this node's job in a
    reference implementation is to prove the tool results and citations made it
    through the graph intact. Swapping in a generation call is a one-function
    change and does not touch the event protocol.
    """
    observations = list(state.get("observations", []))
    successes = [o for o in observations if o["ok"]]
    failures = [o for o in observations if not o["ok"]]
    answer_span_cm = span(
        "agent.answer",
        **{"agent.successes": len(successes), "agent.failures": len(failures),
           "agent.halt_reason": state.get("halt_reason", "completed")},
    )
    answer_span_cm.__enter__()

    lines: list[str] = []
    for obs in successes:
        if obs["tool"] == "web_search":
            hits = obs["result"] if isinstance(obs["result"], list) else []
            for hit in hits:
                lines.append(f"- [{hit.get('source_id')}] {hit.get('snippet', '').strip()}")
        else:
            lines.append(f"- `{obs['tool']}` → {str(obs['result']).strip()}")

    if not lines:
        body = "No step produced a usable result, so there is nothing to answer from."
    else:
        body = "\n".join(lines)

    halt = state.get("halt_reason", "completed")
    footer = []
    if halt == "max_steps":
        footer.append("_Stopped early: step budget reached._")
    if failures:
        footer.append(
            "_Failed steps: "
            + ", ".join(f"{o['tool']} ({o['error']})" for o in failures)
            + "._"
        )
    if state.get("rejection_note"):
        footer.append(f"_A write action was rejected: {state['rejection_note']}._")

    text = "\n\n".join([body, *footer]) if footer else body
    _emit(thinking_event("Composing the final answer.", is_streaming=False))
    answer_span_cm.__exit__(None, None, None)
    return {"answer": text, "halt_reason": halt}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _as_step(wire: dict) -> Any:
    from .events import PlanStep

    return PlanStep(**wire)


def _citations_from(tool_name: str, result: Any) -> list[Citation]:
    """Only `web_search` produces citations. A SQL result has no source document
    and inventing a url for it would make the citation chips lie."""
    if tool_name != "web_search" or not isinstance(result, list):
        return []
    out: list[Citation] = []
    for hit in result:
        if not isinstance(hit, dict) or not hit.get("url"):
            continue  # Citation.url is non-nullable in Dart
        out.append(
            Citation(
                source_id=str(hit.get("source_id") or hit["url"]),
                title=str(hit.get("title") or hit["url"]),
                url=str(hit["url"]),
                snippet=hit.get("snippet"),
            )
        )
    return out


def route_after_reflect(state: AgentState) -> Literal["act", "answer"]:
    if state.get("halt_reason"):
        return "answer"
    return "act"


def build_graph(checkpointer: Any | None = None):
    builder = StateGraph(AgentState)
    builder.add_node("plan", plan)
    builder.add_node("act", act)
    builder.add_node("observe", observe)
    builder.add_node("reflect", reflect)
    builder.add_node("answer", answer)

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "act")
    builder.add_edge("act", "observe")
    builder.add_edge("observe", "reflect")
    builder.add_conditional_edges("reflect", route_after_reflect, ["act", "answer"])
    builder.add_edge("answer", END)

    # A checkpointer is not optional for the human-in-the-loop path: interrupt()
    # has nowhere to persist state without one.
    return builder.compile(checkpointer=checkpointer or MemorySaver())
