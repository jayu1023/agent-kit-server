"""The wire protocol is a contract with a Dart package. Assert it literally.

Every string here was read out of `agent_kit`'s own source. A rename on either
side has to break a test, not a user's app — these assertions are the only thing
standing between a typo and a `FormatException` on a phone.
"""

import json

from app.events import (
    Citation,
    PlanStep,
    PlanStepState,
    ToolCall,
    ToolCallState,
    citations_event,
    control_event,
    plan_event,
    sse,
    thinking_event,
    tool_call_event,
)


def test_event_type_tags_match_the_dart_sealed_union():
    # AgentEvent.fromJson switches on exactly these four and throws otherwise.
    assert tool_call_event(ToolCall(id="a", name="t"))["type"] == "tool_call"
    assert thinking_event("x")["type"] == "thinking"
    assert citations_event([])["type"] == "citations"
    assert plan_event([])["type"] == "plan"


def test_every_event_has_exactly_type_and_data():
    for event in (
        tool_call_event(ToolCall(id="a", name="t")),
        thinking_event("x"),
        citations_event([]),
        plan_event([]),
    ):
        assert set(event) == {"type", "data"}


def test_tool_call_state_names_match_the_dart_enum():
    assert [s.value for s in ToolCallState] == [
        "pending", "running", "success", "failed", "cancelled",
    ]


def test_plan_step_state_names_match_the_dart_enum():
    assert [s.value for s in PlanStepState] == [
        "pending", "running", "done", "failed", "skipped",
    ]


def test_terminal_states_match_the_dart_isterminal_getters():
    assert {s for s in ToolCallState if s.is_terminal} == {
        ToolCallState.success, ToolCallState.failed, ToolCallState.cancelled,
    }


def test_tool_call_omits_null_fields_like_the_dart_tojson():
    wire = ToolCall(id="a", name="t").wire()
    assert wire == {"id": "a", "name": "t", "state": "pending"}
    assert "args" not in wire and "result" not in wire


def test_tool_call_uses_snake_case_duration_key():
    # Dart reads json['duration_ms']; durationMs would silently decode to null.
    assert ToolCall(id="a", name="t", duration_ms=12).wire()["duration_ms"] == 12


def test_required_tool_call_keys_are_always_present():
    # ToolCall.fromJson does `json['id']! as String` — a missing key crashes.
    for key in ("id", "name", "state"):
        assert key in ToolCall(id="a", name="t").wire()


def test_plan_step_always_emits_a_children_list():
    wire = PlanStep(id="s1", label="L").wire()
    assert wire["children"] == []
    nested = PlanStep(id="s1", label="L", children=[PlanStep(id="s2", label="C")]).wire()
    assert nested["children"][0]["id"] == "s2"


def test_citation_uses_snake_case_source_id_and_requires_url():
    wire = Citation(source_id="c1", title="T", url="file:///x").wire()
    assert wire == {"source_id": "c1", "title": "T", "url": "file:///x"}
    assert "snippet" not in wire


def test_thinking_uses_snake_case_is_streaming():
    assert thinking_event("x", is_streaming=True)["data"] == {
        "text": "x", "is_streaming": True,
    }


def test_sse_frame_shape_and_default_event_name():
    frame = sse(thinking_event("hi"))
    assert frame.startswith("event: agent\ndata: ")
    assert frame.endswith("\n\n")
    assert json.loads(frame.split("data: ", 1)[1])["type"] == "thinking"


def test_control_events_are_not_agent_events():
    # A fifth `type` in the agent stream would throw in Dart, so control frames
    # carry `kind` and ride a different SSE event name.
    control = control_event("approval_required", tool="save_report")
    assert "type" not in control
    assert control["kind"] == "approval_required"
    assert sse(control, event="control").startswith("event: control\n")
