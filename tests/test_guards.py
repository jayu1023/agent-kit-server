"""The guards are the difference between an agent and a liability. Test them."""

import asyncio

import pytest
from langchain_core.tools import tool

from app.guards import run_tool


# A StructuredTool is a pydantic model, so call counts live outside it.
CALLS: dict[str, int] = {}


@tool("flaky")
def flaky(fail_times: int) -> str:
    """Fails `fail_times` times with a transient error, then succeeds."""
    CALLS["flaky"] = CALLS.get("flaky", 0) + 1
    if CALLS["flaky"] <= fail_times:
        raise ConnectionError("transient")
    return f"ok on attempt {CALLS['flaky']}"


@tool("permanent")
def permanent(x: int) -> str:
    """Always fails with a validation-shaped error."""
    CALLS["permanent"] = CALLS.get("permanent", 0) + 1
    raise ValueError("bad sql")


@tool("slow")
def slow(seconds: float) -> str:
    """Blocks."""
    import time

    time.sleep(seconds)
    return "finished"


async def test_transient_failures_are_retried_until_success():
    CALLS["flaky"] = 0
    outcome = await run_tool(flaky, {"fail_times": 2}, timeout_s=2, retries=2, backoff_s=0.01)
    assert outcome.ok
    assert outcome.attempts == 3


async def test_transient_failures_give_up_after_the_retry_budget():
    CALLS["flaky"] = 0
    outcome = await run_tool(flaky, {"fail_times": 99}, timeout_s=2, retries=1, backoff_s=0.01)
    assert not outcome.ok
    assert outcome.attempts == 2
    assert "transient" in outcome.error


async def test_a_permanent_error_is_not_retried():
    """The point of the permanent/transient split: retrying a bad argument
    produces the identical failure and burns the step budget."""
    CALLS["permanent"] = 0
    outcome = await run_tool(permanent, {"x": 1}, timeout_s=2, retries=3, backoff_s=0.01)
    assert not outcome.ok
    assert outcome.attempts == 1
    assert CALLS["permanent"] == 1
    assert "bad sql" in outcome.error


async def test_a_hung_tool_is_timed_out_and_reported():
    outcome = await run_tool(slow, {"seconds": 5}, timeout_s=0.2, retries=0, backoff_s=0.01)
    assert not outcome.ok
    assert "timed out" in outcome.error
    assert outcome.duration_ms < 3000


async def test_run_tool_never_raises():
    """Every node downstream assumes this. If it raised, one bad tool would
    abort the whole graph instead of failing one step."""
    outcome = await run_tool(permanent, {"x": 1}, timeout_s=1, retries=0, backoff_s=0.01)
    assert outcome.error is not None


async def test_a_slow_tool_does_not_block_the_event_loop():
    """Tools run in a thread, so concurrent runs do not serialise behind one
    another -- and `wait_for` can actually interrupt them."""
    started = asyncio.get_running_loop().time()
    await asyncio.gather(
        *(run_tool(slow, {"seconds": 0.3}, timeout_s=2, retries=0, backoff_s=0.01) for _ in range(4))
    )
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 1.0, f"4 x 0.3s tools took {elapsed:.2f}s — they ran serially"
