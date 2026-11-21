"""Multi-agent: a supervisor that routes to specialist agents.

    route -> dispatch(specialist) -> collect -> [more specialists] -> synthesize

**What makes this multi-agent rather than one agent with a label.** Three things,
and the third is the one that is usually missing:

1. **Each specialist has a restricted tool set.** The analyst cannot write; the
   scribe cannot query. Capability is a property of who was dispatched, decided
   before anything runs, rather than a property of the current step.
2. **Each specialist has its own step budget.** A shared budget lets one agent
   starve the others -- the classic failure -- so `researcher` gets 2 steps,
   `analyst` 3, `scribe` 1, enforced per specialist in `app/specialists.py`.
3. **The state contract between them is explicit.** A specialist receives the
   question and the *findings so far*, and returns findings. It never sees the
   supervisor's routing state and cannot edit another specialist's results.
   This is the part that decides whether a multi-agent system is debuggable,
   and it is why `SpecialistResult` is a declared type rather than a dict that
   accumulates whatever each agent felt like adding.

**When one agent is better.** Almost always, for three tools. This exists
because the tool sets genuinely differ in blast radius -- one of them writes --
and because routing lets the cheap read-only path skip the expensive one
entirely. The single-agent graph in `app/graph.py` is still the default; see
the README on which to use.

The supervisor reuses the specialists' planners and the same guards, so a tool
behaves identically whichever agent calls it.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .config import settings
from .events import (
    Citation,
    PlanStep,
    PlanStepState,
    ToolCall,
    ToolCallState,
    citations_event,
    plan_event,
    thinking_event,
    tool_call_event,
)
from .graph import _citations_from
from .guards import run_tool
from .planner import heuristic_plan, resolve_args
from .specialists import BY_NAME, SPECIALISTS, Specialist, ordered
from .state import append
from .tools import TOOLS_BY_NAME, is_write_tool
from .tracing import set_attributes, span


def _emit(payload: dict[str, Any]) -> None:
    get_stream_writer()(payload)


class Finding(TypedDict):
    """One tool result, attributed to the specialist that produced it.

    `duration_ms` is here because the late-binding resolvers in `planner.py`
    read it: they were written against the single agent's `Observation`, and a
    Finding has to satisfy that same contract or `report_from_observations`
    raises KeyError at argument-resolution time -- which silently skipped the
    scribe's approval gate entirely, since resolution happens before the
    interrupt. Keeping the two shapes compatible is the contract; the extra
    `specialist` key is additive and harmless.
    """

    specialist: str
    tool: str
    ok: bool
    result: Any
    error: str | None
    duration_ms: int


class SupervisorState(TypedDict, total=False):
    question: str
    roster: list[str]          # specialists to run, in dispatch order
    cursor: int                # which one is running
    findings: Annotated[list[Finding], append]
    citations: Annotated[list[dict], append]
    plan: list[dict]
    answer: str
    halt_reason: Literal["completed", "no_specialist", "rejected"]
    rejection_note: str
    routing_rationale: str


# --------------------------------------------------------------------------- #
# routing
# --------------------------------------------------------------------------- #
def choose_specialists(question: str) -> tuple[list[str], str]:
    """Heuristic router: keyword match per specialist, then fixed ordering.

    Deterministic on purpose -- the same reason the single agent's planner is.
    The tests assert exact rosters, and the demo runs with no API key. An LLM
    router would slot in here behind the same signature; what it must not change
    is that the roster is decided *once*, before dispatch, so the UI can render
    the whole plan up front.
    """
    lowered = question.lower()
    chosen = [s.name for s in SPECIALISTS if any(k in lowered for k in s.keywords)]

    # A writer with nothing to write is the failure this guards: if the scribe
    # was picked alone, give it something to report on.
    if chosen == ["scribe"]:
        chosen = ["analyst", "scribe"]
    if not chosen:
        chosen = ["researcher"]

    roster = ordered(chosen)
    rationale = ", ".join(f"{n} ({BY_NAME[n].description.split('.')[0].lower()})" for n in roster)
    return roster, rationale


def route(state: SupervisorState) -> dict[str, Any]:
    question = state["question"]
    with span("supervisor.route", **{"agent.question_chars": len(question)}) as route_span:
        roster, rationale = choose_specialists(question)
        set_attributes(
            route_span,
            **{"agent.roster": ",".join(roster), "agent.specialists": len(roster)},
        )

    steps = [
        PlanStep(
            id=f"sp-{name}",
            label=f"{name}: {BY_NAME[name].description}",
            state=PlanStepState.pending,
        )
        for name in roster
    ]
    _emit(thinking_event(f"Supervisor dispatching {len(roster)} specialist(s): {rationale}"))
    _emit(plan_event(steps))

    return {
        "roster": roster,
        "cursor": 0,
        "plan": [s.wire() for s in steps],
        "routing_rationale": rationale,
    }


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
def _specialist_plan(specialist: Specialist, question: str) -> list[dict]:
    """Reuse the single-agent planner, then keep only this specialist's tools.

    Filtering after planning rather than writing a planner per specialist keeps
    one place where question -> tool mapping lives. If the filter empties the
    plan, the specialist contributes nothing and says so, which is a better
    outcome than inventing a call it is not allowed to make.
    """
    _, actions = heuristic_plan(question)
    allowed = [a for a in actions if a["tool"] in specialist.tools]
    return allowed[: specialist.max_steps]


def _set_plan_state(plan: list[dict], step_id: str, value: str) -> list[dict]:
    return [{**s, "state": value} if s["id"] == step_id else dict(s) for s in plan]


async def dispatch(state: SupervisorState, config: RunnableConfig) -> dict[str, Any]:
    """Run one specialist to completion, then hand control back."""
    cursor = state["cursor"]
    name = state["roster"][cursor]
    specialist = BY_NAME[name]
    step_id = f"sp-{name}"
    run_id = ((config or {}).get("configurable") or {}).get("thread_id", "run")

    plan = _set_plan_state(state["plan"], step_id, PlanStepState.running.value)
    _emit(plan_event([PlanStep(**s) for s in plan]))

    actions = _specialist_plan(specialist, state["question"])
    if not actions:
        _emit(thinking_event(f"{name} has no applicable tool for this question; skipping."))
        return {
            "plan": _set_plan_state(plan, step_id, PlanStepState.skipped.value),
            "cursor": cursor + 1,
        }

    cfg = settings()
    findings: list[Finding] = []
    citations_out: list[dict] = []
    failed = False

    with span(
        f"specialist.{name}",
        **{"agent.specialist": name, "agent.budget": specialist.max_steps,
           "agent.planned_steps": len(actions), "agent.can_write": specialist.can_write},
    ) as sp_span:
        for index, action in enumerate(actions):
            tool_name = action["tool"]
            # Belt and braces: the plan was filtered, but a specialist must not
            # be able to reach outside its tool set even if that filter changes.
            if tool_name not in specialist.tools:
                raise RuntimeError(f"{name} attempted {tool_name}, outside its tool set")

            call_id = f"{run_id}:{name}-{index}-{tool_name}"
            try:
                # Earlier findings from OTHER specialists plus the ones this
                # specialist has produced so far in this same dispatch. Without
                # the second half, the analyst's calculator step cannot see the
                # SQL result it is meant to compute from -- state is only
                # committed when the node returns.
                visible = list(state.get("findings", [])) + findings
                args = resolve_args(action, state["question"], visible)
            except Exception as exc:
                _emit(tool_call_event(ToolCall(
                    id=call_id, name=tool_name, state=ToolCallState.failed,
                    error=f"could not resolve arguments: {exc}",
                )))
                findings.append({"specialist": name, "tool": tool_name, "ok": False,
                                 "result": None, "error": str(exc), "duration_ms": 0})
                failed = True
                continue

            if is_write_tool(tool_name):
                _emit(tool_call_event(ToolCall(
                    id=call_id, name=tool_name, state=ToolCallState.pending, args=args,
                )))
                decision = interrupt({
                    "kind": "approval_required",
                    "specialist": name,
                    "step_id": step_id,
                    "tool": tool_name,
                    "args": args,
                    "reason": f"The {name} specialist writes to disk.",
                })
                approved = bool(decision.get("approved")) if isinstance(decision, dict) else bool(decision)
                if not approved:
                    note = (decision or {}).get("note", "") if isinstance(decision, dict) else ""
                    _emit(tool_call_event(ToolCall(
                        id=call_id, name=tool_name, state=ToolCallState.cancelled,
                        args=args, error=note or "rejected by reviewer",
                    )))
                    return {
                        "plan": _set_plan_state(plan, step_id, PlanStepState.skipped.value),
                        "cursor": cursor + 1,
                        "rejection_note": note or "rejected by reviewer",
                        "findings": [{"specialist": name, "tool": tool_name, "ok": False,
                                      "result": None, "error": "rejected by reviewer",
                                      "duration_ms": 0}],
                    }

            _emit(tool_call_event(ToolCall(
                id=call_id, name=tool_name, state=ToolCallState.running, args=args,
            )))
            with span(f"tool.{tool_name}", **{"tool.name": tool_name, "agent.specialist": name}) as t:
                outcome = await run_tool(
                    TOOLS_BY_NAME[tool_name], args,
                    timeout_s=cfg.tool_timeout_s, retries=cfg.tool_retries,
                    backoff_s=cfg.retry_backoff_s,
                )
                set_attributes(t, **{"tool.ok": outcome.ok, "tool.attempts": outcome.attempts})

            _emit(tool_call_event(ToolCall(
                id=call_id, name=tool_name,
                state=ToolCallState.success if outcome.ok else ToolCallState.failed,
                args=args, result=outcome.result, error=outcome.error,
                duration_ms=outcome.duration_ms,
            )))

            found = _citations_from(tool_name, outcome.result) if outcome.ok else []
            if found:
                _emit(citations_event(found))
                citations_out.extend(c.wire() for c in found)

            findings.append({"specialist": name, "tool": tool_name, "ok": outcome.ok,
                             "result": outcome.result, "error": outcome.error,
                             "duration_ms": outcome.duration_ms})
            failed = failed or not outcome.ok

        set_attributes(sp_span, **{"agent.findings": len(findings), "agent.failed": failed})

    final_state = PlanStepState.failed if failed and not any(f["ok"] for f in findings) else PlanStepState.done
    plan = _set_plan_state(plan, step_id, final_state.value)
    _emit(plan_event([PlanStep(**s) for s in plan]))
    _emit(thinking_event(f"{name} finished with {len(findings)} finding(s)."))

    return {"plan": plan, "cursor": cursor + 1, "findings": findings, "citations": citations_out}


def route_after_dispatch(state: SupervisorState) -> Literal["dispatch", "synthesize"]:
    return "dispatch" if state["cursor"] < len(state.get("roster", [])) else "synthesize"


# --------------------------------------------------------------------------- #
# synthesis
# --------------------------------------------------------------------------- #
def synthesize(state: SupervisorState) -> dict[str, Any]:
    """Combine the specialists' findings, attributed to whoever produced them.

    Attribution is the whole reason to keep findings typed per specialist: an
    answer you cannot trace back to an agent is a multi-agent system you cannot
    debug.
    """
    findings = list(state.get("findings", []))
    by_specialist: dict[str, list[Finding]] = {}
    for finding in findings:
        by_specialist.setdefault(finding["specialist"], []).append(finding)

    lines: list[str] = []
    for name, items in by_specialist.items():
        good = [f for f in items if f["ok"]]
        if not good:
            continue
        lines.append(f"**{name}**")
        for finding in good:
            if finding["tool"] == "web_search" and isinstance(finding["result"], list):
                for hit in finding["result"]:
                    lines.append(f"- [{hit.get('source_id')}] {str(hit.get('snippet','')).strip()}")
            else:
                lines.append(f"- `{finding['tool']}` → {str(finding['result']).strip()}")
        lines.append("")

    body = "\n".join(lines).strip() or "No specialist produced a usable result."

    footer = []
    failures = [f for f in findings if not f["ok"]]
    if failures:
        footer.append(
            "_Failed: " + ", ".join(f"{f['specialist']}/{f['tool']} ({f['error']})" for f in failures) + "._"
        )
    if state.get("rejection_note"):
        footer.append(f"_A write action was rejected: {state['rejection_note']}._")

    text = "\n\n".join([body, *footer]) if footer else body
    _emit(thinking_event("Supervisor synthesising the specialists' findings."))
    return {"answer": text, "halt_reason": "completed"}


def build_supervisor_graph(checkpointer: Any | None = None):
    builder = StateGraph(SupervisorState)
    builder.add_node("route", route)
    builder.add_node("dispatch", dispatch)
    builder.add_node("synthesize", synthesize)

    builder.add_edge(START, "route")
    builder.add_edge("route", "dispatch")
    builder.add_conditional_edges("dispatch", route_after_dispatch, ["dispatch", "synthesize"])
    builder.add_edge("synthesize", END)

    return builder.compile(checkpointer=checkpointer or MemorySaver())
