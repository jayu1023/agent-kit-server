"""The multi-agent supervisor.

What these pin is the difference between real multi-agent and one agent wearing
a label: restricted tool sets, per-specialist budgets, and an explicit state
contract between supervisor and specialist.
"""

import pytest
from langgraph.types import Command

from app.specialists import ANALYST, BY_NAME, RESEARCHER, SCRIBE, SPECIALISTS, Specialist, ordered
from app.supervisor import build_supervisor_graph, choose_specialists
from app.tools import WRITE_TOOLS


# --------------------------------------------------------------------------- #
# the specialists themselves
# --------------------------------------------------------------------------- #
def test_each_specialist_has_a_restricted_tool_set():
    """A specialist that can reach every tool is not a specialist."""
    all_tools = {t for s in SPECIALISTS for t in s.tools}
    for specialist in SPECIALISTS:
        assert set(specialist.tools) < all_tools, f"{specialist.name} is not restricted"


def test_tool_sets_do_not_overlap():
    """Overlap means two agents can do the same job, so routing stops meaning
    anything and a failure is ambiguous between them."""
    seen: set[str] = set()
    for specialist in SPECIALISTS:
        assert not (seen & set(specialist.tools)), f"{specialist.name} overlaps another specialist"
        seen |= set(specialist.tools)


def test_exactly_one_specialist_can_write():
    writers = [s.name for s in SPECIALISTS if s.can_write]
    assert writers == ["scribe"]
    assert set(SCRIBE.tools) == WRITE_TOOLS


def test_budgets_are_per_specialist_not_shared():
    """A shared budget lets one agent starve the others."""
    assert RESEARCHER.max_steps == 2
    assert ANALYST.max_steps == 3
    assert SCRIBE.max_steps == 1


def test_a_specialist_declaring_an_unknown_tool_is_rejected_at_definition():
    with pytest.raises(ValueError, match="unknown tools"):
        Specialist(name="rogue", description="x", tools=("rm_rf",))


def test_dispatch_order_puts_readers_before_the_writer():
    """A report written before anything was gathered is the failure this stops."""
    assert ordered(["scribe", "analyst", "researcher"]) == ["researcher", "analyst", "scribe"]
    assert ordered(["scribe", "researcher"]) == ["researcher", "scribe"]


def test_ordering_removes_duplicates():
    assert ordered(["analyst", "analyst"]) == ["analyst"]


# --------------------------------------------------------------------------- #
# routing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "question,expected",
    [
        ("What is the Enterprise first-response SLA?", ["researcher"]),
        ("How does average first response compare across plans?", ["analyst"]),
        ("What is our CSAT by plan and save a report about it", ["analyst", "scribe"]),
        ("Tell me something", ["researcher"]),
    ],
)
def test_routing(question, expected):
    assert choose_specialists(question)[0] == expected


def test_a_write_only_question_still_gets_a_reader():
    """The scribe alone would write a report about nothing."""
    roster, _ = choose_specialists("save a report")
    assert roster[0] != "scribe"
    assert "scribe" in roster


def test_the_rationale_names_every_dispatched_specialist():
    roster, rationale = choose_specialists("What is our CSAT by plan and save a report about it")
    for name in roster:
        assert name in rationale


# --------------------------------------------------------------------------- #
# the graph
# --------------------------------------------------------------------------- #
async def collect(graph, config, payload):
    events, paused = [], False
    async for mode, chunk in graph.astream(
        payload, config=config, stream_mode=["custom", "updates"]
    ):
        if mode == "custom":
            events.append(chunk)
        elif mode == "updates" and isinstance(chunk, dict) and "__interrupt__" in chunk:
            paused = bool(chunk["__interrupt__"])
    return events, paused


def tool_calls(events):
    return [e["data"] for e in events if e["type"] == "tool_call"]


async def test_only_the_routed_specialist_runs():
    graph = build_supervisor_graph()
    events, _ = await collect(
        graph, {"configurable": {"thread_id": "sup-route"}},
        {"question": "What is the Enterprise first-response SLA?"},
    )
    called = {c["name"] for c in tool_calls(events)}
    assert called == {"web_search"}
    assert "query_tickets" not in called


async def test_a_specialist_sees_its_own_earlier_findings():
    """The analyst's calculator step computes from the SQL result it just
    produced. State is only committed when the node returns, so this breaks
    unless in-flight findings are passed through."""
    graph = build_supervisor_graph()
    events, _ = await collect(
        graph, {"configurable": {"thread_id": "sup-latebind"}},
        {"question": "How does average first response time compare across plans?"},
    )
    calc = [c for c in tool_calls(events) if c["name"] == "calculator"]
    assert calc and calc[-1]["state"] == "success"


async def test_the_plan_names_the_specialists_before_any_tool_runs():
    graph = build_supervisor_graph()
    events, _ = await collect(
        graph, {"configurable": {"thread_id": "sup-plan"}}, {"question": "What is the SLA?"}
    )
    kinds = [e["type"] for e in events]
    assert kinds.index("plan") < kinds.index("tool_call")
    first_plan = [e["data"] for e in events if e["type"] == "plan"][0]
    assert all(step["id"].startswith("sp-") for step in first_plan)


async def test_findings_are_attributed_to_the_specialist_that_produced_them():
    """An answer you cannot trace back to an agent is undebuggable."""
    graph = build_supervisor_graph()
    config = {"configurable": {"thread_id": "sup-attrib"}}
    await collect(graph, config, {"question": "How does average first response compare across plans?"})
    state = await graph.aget_state(config)
    assert state.values["findings"]
    for finding in state.values["findings"]:
        assert finding["specialist"] in BY_NAME


async def test_findings_satisfy_the_resolver_contract():
    """Findings are consumed by the same late-binding resolvers as the single
    agent's observations, so they must carry duration_ms. Dropping it raised a
    KeyError during argument resolution, which silently skipped the scribe's
    approval gate."""
    graph = build_supervisor_graph()
    config = {"configurable": {"thread_id": "sup-contract"}}
    await collect(graph, config, {"question": "What is average csat by plan?"})
    for finding in (await graph.aget_state(config)).values["findings"]:
        assert set(finding) >= {"specialist", "tool", "ok", "result", "error", "duration_ms"}


async def test_the_write_specialist_pauses_for_approval():
    graph = build_supervisor_graph()
    config = {"configurable": {"thread_id": "sup-hitl"}}
    events, paused = await collect(
        graph, config, {"question": "What is our CSAT by plan and save a report about it"}
    )
    assert paused
    interrupt = (await graph.aget_state(config)).interrupts[0].value
    assert interrupt["specialist"] == "scribe"
    assert interrupt["tool"] == "save_report"
    saves = [c for c in tool_calls(events) if c["name"] == "save_report"]
    assert saves and all(c["state"] == "pending" for c in saves)


async def test_approving_lets_the_write_specialist_finish():
    graph = build_supervisor_graph()
    config = {"configurable": {"thread_id": "sup-approve"}}
    await collect(graph, config, {"question": "What is our CSAT by plan and save a report about it"})
    events, _ = await collect(graph, config, Command(resume={"approved": True}))
    save = [c for c in tool_calls(events) if c["name"] == "save_report"][-1]
    assert save["state"] == "success"

    state = await graph.aget_state(config)
    assert state.values["halt_reason"] == "completed"
    assert {f["specialist"] for f in state.values["findings"]} == {"analyst", "scribe"}


async def test_rejecting_keeps_the_reader_findings():
    graph = build_supervisor_graph()
    config = {"configurable": {"thread_id": "sup-reject"}}
    await collect(graph, config, {"question": "What is our CSAT by plan and save a report about it"})
    events, _ = await collect(
        graph, config, Command(resume={"approved": False, "note": "not for sharing"})
    )
    save = [c for c in tool_calls(events) if c["name"] == "save_report"][-1]
    assert save["state"] == "cancelled"

    state = await graph.aget_state(config)
    assert "not for sharing" in state.values["answer"]
    assert any(f["specialist"] == "analyst" and f["ok"] for f in state.values["findings"])


async def test_the_answer_attributes_findings_by_specialist_name():
    graph = build_supervisor_graph()
    config = {"configurable": {"thread_id": "sup-answer"}}
    await collect(graph, config, {"question": "How does average first response compare across plans?"})
    assert "**analyst**" in (await graph.aget_state(config)).values["answer"]


async def test_tool_call_ids_are_unique_across_specialists_and_runs():
    graph = build_supervisor_graph()
    question = "What is our CSAT by plan and save a report about it"
    a, _ = await collect(graph, {"configurable": {"thread_id": "sup-id-a"}}, {"question": question})
    b, _ = await collect(graph, {"configurable": {"thread_id": "sup-id-b"}}, {"question": question})
    assert not ({c["id"] for c in tool_calls(a)} & {c["id"] for c in tool_calls(b)})
