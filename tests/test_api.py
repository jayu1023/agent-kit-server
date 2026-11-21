"""HTTP surface, including the SSE framing and the two-request approval flow."""

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def frames(response) -> list[tuple[str, dict]]:
    """Parse an SSE body into (event_name, payload) pairs."""
    out: list[tuple[str, dict]] = []
    name = "message"
    for line in response.text.splitlines():
        if line.startswith("event: "):
            name = line.removeprefix("event: ").strip()
        elif line.startswith("data: "):
            out.append((name, json.loads(line.removeprefix("data: "))))
    return out


def agent_frames(parsed):
    return [p for n, p in parsed if n == "agent"]


def control_frames(parsed):
    return [p for n, p in parsed if n == "control"]


def test_health_reports_the_active_configuration(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["planner"] == "heuristic"
    assert body["search_provider"] == "offline"
    assert set(body["tools"]) == {"web_search", "query_tickets", "calculator", "save_report"}


def test_tools_endpoint_publishes_schemas_and_approval_flags(client):
    tools = {t["name"]: t for t in client.get("/tools").json()["tools"]}
    assert tools["calculator"]["args_schema"]["properties"]["expression"]
    assert tools["save_report"]["requires_approval"] is True
    assert tools["web_search"]["requires_approval"] is False


def test_a_blank_question_is_rejected(client):
    assert client.post("/runs", json={"question": ""}).status_code == 422


def test_a_read_only_run_streams_agent_frames_then_done(client):
    response = client.post(
        "/runs",
        json={"question": "How does average first response compare across plans?", "run_id": "api-ro"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-run-id"] == "api-ro"

    parsed = frames(response)
    controls = control_frames(parsed)
    assert controls[0]["kind"] == "run_started"
    assert controls[-1]["kind"] == "done"
    assert controls[-1]["halt_reason"] == "completed"
    assert controls[-1]["answer"]

    kinds = {f["type"] for f in agent_frames(parsed)}
    assert kinds == {"plan", "thinking", "tool_call", "citations"}


def test_agent_frames_only_ever_carry_the_four_dart_types(client):
    """A fifth `type` would raise FormatException in the Flutter client."""
    response = client.post("/runs", json={"question": "What is the Sev-1 target?", "run_id": "api-types"})
    for frame in agent_frames(frames(response)):
        assert frame["type"] in {"plan", "thinking", "tool_call", "citations"}
        assert set(frame) == {"type", "data"}


def test_control_frames_never_look_like_agent_events(client):
    response = client.post("/runs", json={"question": "What is the Sev-1 target?", "run_id": "api-ctrl"})
    for frame in control_frames(frames(response)):
        assert "type" not in frame
        assert frame["kind"]


def test_a_write_run_stops_at_approval_required(client):
    response = client.post(
        "/runs",
        json={"question": "What is our CSAT by plan and save a report about it", "run_id": "api-hitl"},
    )
    controls = control_frames(frames(response))
    assert controls[-1]["kind"] == "approval_required"
    assert controls[-1]["tool"] == "save_report"
    # The stream ends at the pause; it does not hold the connection open.
    assert not any(c["kind"] == "done" for c in controls)


def test_the_paused_run_is_readable_over_http(client):
    body = client.get("/runs/api-hitl").json()
    assert body["awaiting_approval"]
    assert body["awaiting_approval"][0]["tool"] == "save_report"
    assert body["answer"] is None


def test_approving_resumes_the_same_run_and_finishes(client):
    response = client.post("/runs/api-hitl/approve", json={"approved": True})
    assert response.status_code == 200
    controls = control_frames(frames(response))
    assert controls[-1]["kind"] == "done"

    saves = [
        f["data"]
        for f in agent_frames(frames(response))
        if f["type"] == "tool_call" and f["data"]["name"] == "save_report"
    ]
    assert saves[-1]["state"] == "success"


def test_approving_twice_is_a_conflict_not_a_second_write(client):
    assert client.post("/runs/api-hitl/approve", json={"approved": True}).status_code == 409


def test_rejecting_records_the_note(client):
    client.post(
        "/runs",
        json={"question": "Save a report on csat by plan", "run_id": "api-reject"},
    )
    response = client.post(
        "/runs/api-reject/approve", json={"approved": False, "note": "needs legal review"}
    )
    cancelled = [
        f["data"]
        for f in agent_frames(frames(response))
        if f["type"] == "tool_call" and f["data"]["name"] == "save_report"
    ]
    assert cancelled[-1]["state"] == "cancelled"
    assert "needs legal review" in cancelled[-1]["error"]


def test_approving_an_unknown_run_is_a_404(client):
    assert client.post("/runs/nope/approve", json={"approved": True}).status_code == 404


def test_reading_an_unknown_run_is_a_404(client):
    assert client.get("/runs/nope").status_code == 404


def test_run_ids_are_generated_when_not_supplied(client):
    response = client.post("/runs", json={"question": "What is the Sev-3 target?"})
    assert response.headers["x-run-id"]
    assert control_frames(frames(response))[0]["run_id"] == response.headers["x-run-id"]


# --------------------------------------------------------------------------- #
# supervisor mode over HTTP
# --------------------------------------------------------------------------- #
def test_specialists_endpoint_publishes_the_roster(client):
    by_name = {s["name"]: s for s in client.get("/specialists").json()["specialists"]}
    assert set(by_name) == {"researcher", "analyst", "scribe"}
    assert by_name["scribe"]["can_write"] is True
    assert by_name["analyst"]["can_write"] is False
    assert by_name["researcher"]["tools"] == ["web_search"]


def test_health_advertises_both_modes(client):
    body = client.get("/health").json()
    assert body["modes"] == ["single", "supervisor"]
    assert body["specialists"] == ["researcher", "analyst", "scribe"]


def test_an_unknown_mode_is_rejected(client):
    assert client.post("/runs", json={"question": "x", "mode": "swarm"}).status_code == 422


def test_supervisor_mode_runs_only_the_routed_specialists_tools(client):
    response = client.post(
        "/runs",
        json={
            "question": "How does average first response compare across plans?",
            "run_id": "api-sup",
            "mode": "supervisor",
        },
    )
    called = {
        f["data"]["name"] for f in agent_frames(frames(response)) if f["type"] == "tool_call"
    }
    assert called == {"query_tickets", "calculator"}
    assert "web_search" not in called


def test_the_run_reports_its_mode_and_roster(client):
    body = client.get("/runs/api-sup").json()
    assert body["mode"] == "supervisor"
    assert body["roster"] == ["analyst"]


def test_single_mode_is_the_default(client):
    response = client.post("/runs", json={"question": "What is the Sev-1 target?", "run_id": "api-def"})
    assert control_frames(frames(response))[0]["mode"] == "single"
    assert client.get("/runs/api-def").json()["mode"] == "single"


def test_approve_resumes_the_supervisor_graph_not_the_single_agent(client):
    """Both graphs share a checkpointer, so /approve has to resume the right
    one. Resuming the wrong graph is a 409, which is how this would fail."""
    client.post(
        "/runs",
        json={
            "question": "What is our CSAT by plan and save a report about it",
            "run_id": "api-sup-hitl",
            "mode": "supervisor",
        },
    )
    assert client.get("/runs/api-sup-hitl").json()["awaiting_approval"]

    response = client.post("/runs/api-sup-hitl/approve", json={"approved": True})
    assert response.status_code == 200
    saves = [
        f["data"] for f in agent_frames(frames(response))
        if f["type"] == "tool_call" and f["data"]["name"] == "save_report"
    ]
    assert saves[-1]["state"] == "success"
