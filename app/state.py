"""The graph's state. Explicit, flat, and JSON-serialisable.

Explicit because a checkpointer has to persist it across the
human-in-the-loop interrupt and an HTTP process boundary: whatever is not in
this TypedDict does not survive the approval round-trip. "Keep it in a closure"
works until the resume call lands on a different worker.

Flat (wire dicts, not nested model objects) because the same structures go
straight out to the Flutter client. One representation, no mapping layer.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from .events import PlanStep


def append(left: list, right: list) -> list:
    """Reducer: nodes return only what they added, LangGraph concatenates.

    Without a reducer, two nodes writing the same list key overwrite each other
    and you lose the earlier node's tool calls.
    """
    return (left or []) + (right or [])


class Action(TypedDict):
    """One planned tool invocation, bound to the plan step it serves."""

    step_id: str
    tool: str
    args: dict[str, Any]


class Observation(TypedDict):
    step_id: str
    tool: str
    ok: bool
    result: Any
    error: str | None
    duration_ms: int
    attempts: int


HaltReason = Literal["completed", "max_steps", "rejected", "no_plan", "tool_failed"]


class AgentState(TypedDict, total=False):
    question: str

    # plan -> act -> observe -> reflect
    plan: list[dict]          # PlanStep wire dicts, mirrored to the UI as-is
    actions: list[Action]     # one per plan step, index-aligned with `plan`
    cursor: int               # index of the step currently being executed

    # Arguments decided by act() and executed by observe(). It lives in state
    # rather than being passed between the nodes because the human-in-the-loop
    # interrupt happens *between* them: the checkpointer has to carry it across
    # the approval round-trip, which may be a different process.
    resolved_args: dict[str, Any] | None

    tool_calls: Annotated[list[dict], append]
    observations: Annotated[list[Observation], append]
    citations: Annotated[list[dict], append]

    steps_taken: int
    answer: str
    halt_reason: HaltReason
    rejection_note: str


def plan_steps(state: AgentState) -> list[PlanStep]:
    return [PlanStep(**s) for s in state.get("plan", [])]


def set_step_state(plan: list[dict], step_id: str, value: str) -> list[dict]:
    """Return a copy of the plan with one step's state changed.

    A copy rather than a mutation because the checkpointer snapshots state
    between nodes; mutating in place makes the history lie about what happened.
    """
    return [{**s, "state": value} if s["id"] == step_id else dict(s) for s in plan]
