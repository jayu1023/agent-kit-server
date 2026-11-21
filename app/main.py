"""HTTP surface.

    POST /runs                 start a run, stream agent_kit events over SSE
    POST /runs/{id}/approve    resume a run paused at a write action
    GET  /runs/{id}            current state of a run (plan, answer, halt reason)
    GET  /tools                tool names + JSON schemas + which need approval
    GET  /health

**Two SSE event names, on purpose.** `event: agent` frames are exactly what
`AgentEvent.fromJson` accepts, so a Flutter client pipes them straight into
`AgentView`. Everything else -- approval requests, completion, errors -- goes out
as `event: control`, because the Dart union has four members and a fifth `type`
would throw `FormatException` in the client. A client that ignores unknown SSE
event names keeps working either way.

**Why approval is a second request and not a reply on the open stream.** The
interrupt is persisted by the checkpointer, so the run survives the stream
closing, a reviewer taking an hour, or the process restarting. Holding approval
on the original connection would make a human decision depend on a socket
staying up.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic import BaseModel, Field

from .config import settings
from .events import control_event, sse
from .graph import build_graph
from .specialists import SPECIALISTS
from .supervisor import build_supervisor_graph
from .tools import tool_schemas

log = logging.getLogger("agent")

# ponytail: MemorySaver keeps runs in process memory, so an approval must reach
# the same worker and a restart loses paused runs. Swap in the Postgres
# checkpointer when runs have to outlive the process or scale past one worker.
CHECKPOINTER = MemorySaver()
GRAPH = build_graph(CHECKPOINTER)

# The multi-agent graph shares the checkpointer, so a run of either mode is
# addressable and resumable by the same thread_id and the approve endpoint does
# not need to know which one it is resuming.
SUPERVISOR_GRAPH = build_supervisor_graph(CHECKPOINTER)

#: Which graph a run used, so /approve resumes the right one. Lives beside the
#: checkpointer and shares its lifetime; see the MemorySaver note above.
RUN_MODE: dict[str, str] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings().reports_dir.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="agent_kit_server", version="0.1.0", lifespan=lifespan)


class RunRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    run_id: str | None = Field(default=None, description="Supply one to make the run addressable.")
    mode: Literal["single", "supervisor"] = Field(
        default="single",
        description=(
            "single     = one agent, all four tools (the default; right answer for most tasks). "
            "supervisor = route to specialist agents with restricted tool sets and "
            "per-specialist step budgets."
        ),
    )


class ApproveRequest(BaseModel):
    approved: bool
    note: str | None = Field(default=None, max_length=500)


def _config(run_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": run_id}}


def _graph_for(run_id: str):
    return SUPERVISOR_GRAPH if RUN_MODE.get(run_id) == "supervisor" else GRAPH


async def _stream(run_id: str, payload: Any) -> AsyncIterator[str]:
    """Translate the graph's event stream into SSE frames.

    `stream_mode=["custom", "updates"]` because the two carry different things:
    `custom` is the agent_kit events the nodes emit, `updates` is where
    LangGraph reports `__interrupt__`. Without `updates`, a paused run would
    look to the client like a stream that just stopped.
    """
    graph = _graph_for(run_id)
    yield sse(
        control_event("run_started", run_id=run_id, mode=RUN_MODE.get(run_id, "single")),
        event="control",
    )
    try:
        async for mode, chunk in graph.astream(
            payload, config=_config(run_id), stream_mode=["custom", "updates"]
        ):
            if mode == "custom":
                yield sse(chunk, event="agent")
            elif mode == "updates" and isinstance(chunk, dict) and "__interrupt__" in chunk:
                interrupts = chunk["__interrupt__"]
                if interrupts:
                    # The interrupt payload already carries its own `kind`
                    # (`approval_required`), so it is spread in rather than
                    # passed alongside one -- doing both is a TypeError.
                    value = dict(interrupts[0].value)
                    kind = value.pop("kind", "approval_required")
                    yield sse(
                        control_event(kind, run_id=run_id, **value),
                        event="control",
                    )
                    return

        state = await graph.aget_state(_config(run_id))
        yield sse(
            control_event(
                "done",
                run_id=run_id,
                halt_reason=state.values.get("halt_reason"),
                answer=state.values.get("answer"),
                steps_taken=state.values.get("steps_taken"),
            ),
            event="control",
        )
    except asyncio.CancelledError:
        log.info("client disconnected from run %s", run_id)
        raise
    except Exception as exc:  # in-band, so a half-rendered client learns why
        log.exception("run %s failed", run_id)
        yield sse(
            control_event("error", run_id=run_id, message=f"{type(exc).__name__}: {exc}"),
            event="control",
        )


SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}


@app.post("/runs")
async def start_run(req: RunRequest):
    run_id = req.run_id or uuid.uuid4().hex[:12]
    RUN_MODE[run_id] = req.mode
    return StreamingResponse(
        _stream(run_id, {"question": req.question}),
        media_type="text/event-stream",
        headers={**SSE_HEADERS, "X-Run-Id": run_id},
    )


@app.post("/runs/{run_id}/approve")
async def approve(run_id: str, req: ApproveRequest):
    state = await _graph_for(run_id).aget_state(_config(run_id))
    if not state.created_at:
        raise HTTPException(status_code=404, detail=f"no run {run_id}")
    if not state.interrupts:
        raise HTTPException(status_code=409, detail=f"run {run_id} is not awaiting approval")

    return StreamingResponse(
        _stream(run_id, Command(resume={"approved": req.approved, "note": req.note or ""})),
        media_type="text/event-stream",
        headers={**SSE_HEADERS, "X-Run-Id": run_id},
    )


@app.get("/runs/{run_id}")
async def get_run(run_id: str):
    state = await _graph_for(run_id).aget_state(_config(run_id))
    if not state.created_at:
        raise HTTPException(status_code=404, detail=f"no run {run_id}")
    values = state.values
    return {
        "run_id": run_id,
        "mode": RUN_MODE.get(run_id, "single"),
        "roster": values.get("roster", []),
        "question": values.get("question"),
        "plan": values.get("plan", []),
        "answer": values.get("answer"),
        "halt_reason": values.get("halt_reason"),
        "steps_taken": values.get("steps_taken", 0),
        "awaiting_approval": [i.value for i in state.interrupts],
        "citations": values.get("citations", []),
    }


@app.get("/tools")
def tools():
    return {"tools": tool_schemas()}


@app.get("/specialists")
def specialists():
    """The supervisor's roster: who exists, what each may call, and its budget."""
    return {
        "specialists": [
            {
                "name": s.name,
                "description": s.description,
                "tools": list(s.tools),
                "max_steps": s.max_steps,
                "can_write": s.can_write,
            }
            for s in SPECIALISTS
        ]
    }


@app.get("/health")
def health():
    cfg = settings()
    return {
        "status": "ok",
        "planner": cfg.planner,
        "search_provider": cfg.search_provider,
        "max_steps": cfg.max_steps,
        "tool_timeout_s": cfg.tool_timeout_s,
        "tools": [t["name"] for t in tool_schemas()],
        "modes": ["single", "supervisor"],
        "specialists": [s.name for s in SPECIALISTS],
    }
