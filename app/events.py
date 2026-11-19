"""The `agent_kit` wire protocol — the only module that knows the Dart shapes.

`agent_kit` (pub.dev) decodes a tagged JSON map with `type` + `data` into a
sealed `AgentEvent`:

    {"type": "tool_call", "data": {...ToolCall}}
    {"type": "thinking",  "data": {...ThinkingStep}}
    {"type": "citations", "data": [ ...Citation ]}
    {"type": "plan",      "data": [ ...PlanStep ]}

Anything else raises `FormatException('Unknown AgentEvent type: ...')` on the
client. Field names are snake_case and the enum values are the Dart enum
*names*, so they are reproduced here as literals rather than derived — a typo
becomes a client-side crash, which is why `tests/test_events.py` asserts every
one of them against the shapes in the package source.

Keeping the mapping in one file means a breaking change in the Dart package is
a one-file change here, instead of a hunt through the graph nodes.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolCallState(str, Enum):
    """Mirrors `ToolCallState` in agent_kit. Terminal: success/failed/cancelled."""

    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"
    cancelled = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {ToolCallState.success, ToolCallState.failed, ToolCallState.cancelled}


class PlanStepState(str, Enum):
    """Mirrors `PlanStepState` in agent_kit. Terminal: done/failed/skipped."""

    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"
    skipped = "skipped"


class ToolCall(BaseModel):
    """`ToolCall.fromJson` requires id/name/state; the rest are optional.

    Note `duration_ms`, not `durationMs` — the Dart side reads the snake_case key.
    """

    id: str
    name: str
    state: ToolCallState = ToolCallState.pending
    args: dict[str, Any] | None = None
    result: Any | None = None
    error: str | None = None
    duration_ms: int | None = None

    def wire(self) -> dict[str, Any]:
        # Dart's toJson omits null fields; mirroring that keeps the payload
        # small and keeps `ToolCall.fromJson` on its non-null paths.
        return {"id": self.id, "name": self.name, "state": self.state.value} | {
            k: v
            for k, v in (
                ("args", self.args),
                ("result", self.result),
                ("error", self.error),
                ("duration_ms", self.duration_ms),
            )
            if v is not None
        }


class PlanStep(BaseModel):
    id: str
    label: str
    state: PlanStepState = PlanStepState.pending
    children: list["PlanStep"] = Field(default_factory=list)

    def wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "state": self.state.value,
            "children": [c.wire() for c in self.children],
        }


class Citation(BaseModel):
    """`url` is non-nullable in Dart — a missing url crashes `Citation.fromJson`.

    Local sources therefore get a `file://` url rather than an empty string.
    """

    source_id: str
    title: str
    url: str
    snippet: str | None = None

    def wire(self) -> dict[str, Any]:
        out = {"source_id": self.source_id, "title": self.title, "url": self.url}
        if self.snippet is not None:
            out["snippet"] = self.snippet
        return out


EventType = Literal["tool_call", "thinking", "citations", "plan"]


def tool_call_event(call: ToolCall) -> dict[str, Any]:
    return {"type": "tool_call", "data": call.wire()}


def thinking_event(text: str, *, is_streaming: bool = False) -> dict[str, Any]:
    return {"type": "thinking", "data": {"text": text, "is_streaming": is_streaming}}


def citations_event(citations: list[Citation]) -> dict[str, Any]:
    return {"type": "citations", "data": [c.wire() for c in citations]}


def plan_event(steps: list[PlanStep]) -> dict[str, Any]:
    return {"type": "plan", "data": [s.wire() for s in steps]}


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
def sse(payload: dict[str, Any], *, event: str = "agent") -> str:
    """One SSE frame.

    The SSE `event:` name is always `agent` and the discriminator lives *inside*
    the JSON, because that is where `AgentEvent.fromJson` looks for it. Putting
    the type in the SSE event name instead would force every client to
    reconstruct the envelope before it could decode.
    """
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


def control_event(kind: str, **fields: Any) -> dict[str, Any]:
    """Out-of-band server control frames (approval requests, done, errors).

    Deliberately *not* an `AgentEvent`: the Dart union has exactly four members
    and a fifth type would throw. These go out on a different SSE event name so
    `AgentView` never sees them, and a client that ignores unknown SSE event
    names keeps working.
    """
    return {"kind": kind, **fields}
