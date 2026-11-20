"""Tracing must be free when off, correct when on, and never break the request.

The failure mode worth guarding: tracing that throws, or that quietly costs
something on every request when nobody asked for it.
"""

import json
import os

import pytest

from app import tracing


@pytest.fixture(autouse=True)
def reset_tracing():
    tracing._provider.cache_clear()
    tracing.tracer.cache_clear()
    yield
    tracing._provider.cache_clear()
    tracing.tracer.cache_clear()


def test_tracing_is_off_by_default(monkeypatch):
    """Opt-in, not opt-out. A library that traces by default is a nuisance."""
    monkeypatch.delenv("TRACE_EXPORTER", raising=False)
    assert tracing._provider() is None


def test_spans_are_a_no_op_when_disabled(monkeypatch):
    """The call sites have no `if enabled` guard, so this has to hold."""
    monkeypatch.delenv("TRACE_EXPORTER", raising=False)
    with tracing.span("agent.anything", **{"agent.x": 1}) as s:
        tracing.set_attributes(s, **{"agent.y": 2})
    tracing.flush()  # must not raise with no provider


def test_an_unknown_exporter_fails_loudly(monkeypatch):
    monkeypatch.setenv("TRACE_EXPORTER", "carrier-pigeon")
    with pytest.raises(ValueError, match="unknown TRACE_EXPORTER"):
        tracing._provider()


def test_file_exporter_writes_one_json_object_per_span(monkeypatch, tmp_path):
    path = tmp_path / "t.jsonl"
    monkeypatch.setenv("TRACE_EXPORTER", "file")
    monkeypatch.setenv("TRACE_FILE", str(path))

    with tracing.span("agent.parent", **{"agent.top_k": 5}):
        with tracing.span("agent.child", **{"agent.hits": 3}):
            pass
    tracing.flush()

    spans = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert len(spans) == 2
    by_name = {s["name"]: s for s in spans}
    assert by_name["agent.parent"]["attributes"]["agent.top_k"] == 5
    assert by_name["agent.child"]["attributes"]["agent.hits"] == 3


def test_child_spans_record_their_parent(monkeypatch, tmp_path):
    """A flat list of spans is not a trace. Nesting is the whole product."""
    path = tmp_path / "t.jsonl"
    monkeypatch.setenv("TRACE_EXPORTER", "file")
    monkeypatch.setenv("TRACE_FILE", str(path))

    with tracing.span("agent.parent"):
        with tracing.span("agent.child"):
            pass
    tracing.flush()

    spans = {s["name"]: s for s in (json.loads(l) for l in path.read_text().splitlines() if l.strip())}
    assert spans["agent.parent"]["parent_id"] is None
    assert spans["agent.child"]["parent_id"] == spans["agent.parent"]["span_id"]
    assert spans["agent.child"]["trace_id"] == spans["agent.parent"]["trace_id"]


def test_none_valued_attributes_are_dropped(monkeypatch, tmp_path):
    """OTel rejects None attribute values; the helper must filter them."""
    path = tmp_path / "t.jsonl"
    monkeypatch.setenv("TRACE_EXPORTER", "file")
    monkeypatch.setenv("TRACE_FILE", str(path))

    with tracing.span("agent.x", **{"agent.present": 1, "agent.absent": None}):
        pass
    tracing.flush()

    attributes = json.loads(path.read_text().splitlines()[0])["attributes"]
    assert attributes == {"agent.present": 1}


def test_the_file_is_truncated_per_run(monkeypatch, tmp_path):
    """A trace file that accumulates across runs is an archive, not a trace."""
    path = tmp_path / "t.jsonl"
    path.write_text('{"stale": true}\n')
    monkeypatch.setenv("TRACE_EXPORTER", "file")
    monkeypatch.setenv("TRACE_FILE", str(path))

    with tracing.span("agent.fresh"):
        pass
    tracing.flush()

    assert "stale" not in path.read_text()


def test_duration_is_recorded_in_milliseconds(monkeypatch, tmp_path):
    import time

    path = tmp_path / "t.jsonl"
    monkeypatch.setenv("TRACE_EXPORTER", "file")
    monkeypatch.setenv("TRACE_FILE", str(path))

    with tracing.span("agent.slow"):
        time.sleep(0.05)
    tracing.flush()

    duration = json.loads(path.read_text().splitlines()[0])["duration_ms"]
    assert 40 < duration < 500
