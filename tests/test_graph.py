"""The graph: the loop, the guards that live in it, and the approval path."""

import pytest
from langgraph.types import Command

from app.config import settings
from app.graph import build_graph


async def collect(graph, config, payload):
    """Run the graph and return (agent events, whether it paused)."""
    events: list[dict] = []
    paused = False
    async for mode, chunk in graph.astream(
        payload, config=config, stream_mode=["custom", "updates"]
    ):
        if mode == "custom":
            events.append(chunk)
        elif mode == "updates" and isinstance(chunk, dict) and "__interrupt__" in chunk:
            paused = bool(chunk["__interrupt__"])
    return events, paused


def of_type(events, kind):
    return [e["data"] for e in events if e["type"] == kind]


def tool_calls(events):
    return of_type(events, "tool_call")


async def test_a_read_only_run_completes_without_pausing():
    graph = build_graph()
    config = {"configurable": {"thread_id": "g-readonly"}}
    events, paused = await collect(
        graph, config, {"question": "How does average first response compare across plans?"}
    )

    assert not paused
    state = await graph.aget_state(config)
    assert state.values["halt_reason"] == "completed"
    assert state.values["answer"]


async def test_the_plan_is_emitted_before_any_tool_call():
    """The Flutter timeline has to render before the first tool starts, or the
    user watches an empty panel while a tool runs."""
    graph = build_graph()
    events, _ = await collect(
        graph, {"configurable": {"thread_id": "g-order"}}, {"question": "What is the SLA?"}
    )
    kinds = [e["type"] for e in events]
    assert kinds.index("plan") < kinds.index("tool_call")


async def test_every_tool_call_id_reaches_a_terminal_state():
    graph = build_graph()
    events, _ = await collect(
        graph,
        {"configurable": {"thread_id": "g-terminal"}},
        {"question": "What is average csat by plan?"},
    )
    terminal = {"success", "failed", "cancelled"}
    last_by_id: dict[str, str] = {}
    for call in tool_calls(events):
        last_by_id[call["id"]] = call["state"]
    assert last_by_id
    assert all(state in terminal for state in last_by_id.values())


async def test_tool_call_ids_are_unique_across_runs():
    """Two runs both start at step `s1`; a client showing both must not collide
    their cards. Regression test for a bug the Dart wire test found."""
    graph = build_graph()
    question = "What is the SLA?"
    first, _ = await collect(graph, {"configurable": {"thread_id": "run-a"}}, {"question": question})
    second, _ = await collect(graph, {"configurable": {"thread_id": "run-b"}}, {"question": question})

    ids_a = {c["id"] for c in tool_calls(first)}
    ids_b = {c["id"] for c in tool_calls(second)}
    assert ids_a and ids_b
    assert not (ids_a & ids_b)


async def test_plan_steps_all_reach_a_terminal_state_in_the_emitted_events():
    """Regression test: `observe` used to update the plan in state without
    emitting it, leaving the UI's last step stuck on `running`."""
    graph = build_graph()
    events, _ = await collect(
        graph,
        {"configurable": {"thread_id": "g-plan-terminal"}},
        {"question": "How does average first response compare across plans?"},
    )
    final_plan = of_type(events, "plan")[-1]
    assert all(step["state"] in {"done", "failed", "skipped"} for step in final_plan)


async def test_citations_are_emitted_for_search_and_carry_urls():
    graph = build_graph()
    events, _ = await collect(
        graph, {"configurable": {"thread_id": "g-cite"}}, {"question": "What is the Sev-1 target?"}
    )
    citations = of_type(events, "citations")
    assert citations
    for citation in citations[0]:
        assert citation["url"]
        assert citation["source_id"]


async def test_sql_results_produce_no_citations():
    """Inventing a url for a SQL row would make the citation chips lie."""
    graph = build_graph()
    events, _ = await collect(
        graph,
        {"configurable": {"thread_id": "g-nocite"}},
        {"question": "What is average csat by plan?"},
    )
    cited = {c["source_id"] for batch in of_type(events, "citations") for c in batch}
    assert all(not s.startswith("query_tickets") for s in cited)


# --------------------------------------------------------------------------- #
# human in the loop
# --------------------------------------------------------------------------- #
WRITE_QUESTION = "What is our CSAT by plan and save a report about it"


async def test_a_write_action_pauses_the_run_before_executing():
    graph = build_graph()
    config = {"configurable": {"thread_id": "g-hitl-pause"}}
    events, paused = await collect(graph, config, {"question": WRITE_QUESTION})

    assert paused
    state = await graph.aget_state(config)
    interrupt = state.interrupts[0].value
    assert interrupt["kind"] == "approval_required"
    assert interrupt["tool"] == "save_report"
    assert interrupt["args"]["title"]

    # Crucially: it paused BEFORE running. No terminal save_report state yet.
    saves = [c for c in tool_calls(events) if c["name"] == "save_report"]
    assert saves and all(c["state"] == "pending" for c in saves)


async def test_approving_runs_the_write_and_completes():
    graph = build_graph()
    config = {"configurable": {"thread_id": "g-hitl-approve"}}
    await collect(graph, config, {"question": WRITE_QUESTION})
    events, paused = await collect(graph, config, Command(resume={"approved": True}))

    assert not paused
    save = [c for c in tool_calls(events) if c["name"] == "save_report"][-1]
    assert save["state"] == "success"
    assert save["result"]["bytes"] > 0


async def test_rejecting_cancels_the_write_and_still_answers():
    """A rejection must not throw away the read-only work already done."""
    graph = build_graph()
    config = {"configurable": {"thread_id": "g-hitl-reject"}}
    await collect(graph, config, {"question": WRITE_QUESTION})
    events, _ = await collect(
        graph, config, Command(resume={"approved": False, "note": "not approved"})
    )

    save = [c for c in tool_calls(events) if c["name"] == "save_report"][-1]
    assert save["state"] == "cancelled"
    assert "not approved" in save["error"]

    state = await graph.aget_state(config)
    assert state.values["halt_reason"] == "completed"
    assert "rejected" in state.values["answer"]
    # The earlier query result survived the rejection.
    assert "query_tickets" in state.values["answer"]


async def test_a_rejection_writes_no_file():
    import os

    graph = build_graph()
    config = {"configurable": {"thread_id": "g-hitl-nofile"}}
    before = set(os.listdir(settings().reports_dir))
    await collect(graph, config, {"question": WRITE_QUESTION})
    await collect(graph, config, Command(resume={"approved": False}))
    assert set(os.listdir(settings().reports_dir)) == before


async def test_no_write_question_never_pauses():
    graph = build_graph()
    _, paused = await collect(
        graph, {"configurable": {"thread_id": "g-nopause"}}, {"question": "What is the Sev-2 target?"}
    )
    assert not paused


# --------------------------------------------------------------------------- #
# loop guard
# --------------------------------------------------------------------------- #
async def test_the_step_budget_halts_a_long_plan(monkeypatch):
    """max_steps lives in reflect() so there is exactly one place the loop can
    end. Set it to 1 and a three-step plan must stop after one."""
    from app import config as config_mod

    config_mod.settings.cache_clear()
    monkeypatch.setenv("MAX_STEPS", "1")

    graph = build_graph()
    run_config = {"configurable": {"thread_id": "g-budget"}}
    await collect(
        graph, run_config, {"question": "How does average first response compare across plans?"}
    )

    state = await graph.aget_state(run_config)
    assert state.values["halt_reason"] == "max_steps"
    assert state.values["steps_taken"] == 1
    # It still answers from what it gathered rather than returning nothing.
    assert state.values["answer"]
    assert "Stopped early" in state.values["answer"]

    monkeypatch.delenv("MAX_STEPS")
    config_mod.settings.cache_clear()


async def test_a_failing_step_does_not_abort_the_run(monkeypatch):
    """One bad tool fails one step. The answer still reports what worked and
    names what did not."""
    from app import graph as graph_mod

    async def boom(*args, **kwargs):
        from app.guards import ToolOutcome

        return ToolOutcome(ok=False, result=None, error="exploded", duration_ms=1, attempts=1)

    monkeypatch.setattr(graph_mod, "run_tool", boom)
    graph = build_graph()
    config = {"configurable": {"thread_id": "g-fail"}}
    events, _ = await collect(graph, config, {"question": "What is the SLA?"})

    state = await graph.aget_state(config)
    assert state.values["halt_reason"] == "completed"
    assert "exploded" in state.values["answer"]
    assert of_type(events, "plan")[-1][0]["state"] == "failed"
