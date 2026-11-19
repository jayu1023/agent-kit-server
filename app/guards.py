"""Tool execution guards: timeout, retry, and the permanent/transient split.

These live between the graph and the tools rather than inside each tool, so a
new tool inherits them by existing. The graph-level guard (max steps) is in
`reflect()`, because it is about the loop, not about a call.

The distinction that matters: **a validation error is permanent.** Retrying
`SELECT * FROM ticekts` three times produces the same typo three times, burns
the step budget and hides the real problem. Retries are for timeouts and
transport, never for "the model wrote a bad argument".
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool
from pydantic import ValidationError

#: Errors that will produce the identical failure on a retry.
PERMANENT = (ValueError, TypeError, KeyError, ValidationError)


@dataclass
class ToolOutcome:
    ok: bool
    result: Any
    error: str | None
    duration_ms: int
    attempts: int


async def run_tool(
    tool: BaseTool,
    args: dict[str, Any],
    *,
    timeout_s: float,
    retries: int,
    backoff_s: float,
) -> ToolOutcome:
    """Invoke a tool with a timeout and bounded retries. Never raises."""
    started = time.perf_counter()
    attempts = 0
    last_error = "unknown error"

    for attempt in range(retries + 1):
        attempts = attempt + 1
        try:
            # Tools here are synchronous and CPU/disk-bound. `to_thread` keeps
            # the event loop free so one slow tool cannot stall the SSE streams
            # of every other in-flight run, and it is what makes `wait_for`
            # actually able to time out.
            result = await asyncio.wait_for(
                asyncio.to_thread(tool.invoke, args), timeout=timeout_s
            )
            return ToolOutcome(
                ok=True,
                result=result,
                error=None,
                duration_ms=int((time.perf_counter() - started) * 1000),
                attempts=attempts,
            )
        except asyncio.TimeoutError:
            last_error = f"timed out after {timeout_s:g}s"
        except PERMANENT as exc:
            # Permanent: stop immediately and report. Do not spend the budget.
            return ToolOutcome(
                ok=False,
                result=None,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.perf_counter() - started) * 1000),
                attempts=attempts,
            )
        except Exception as exc:  # transient: network, disk, anything unlabelled
            last_error = f"{type(exc).__name__}: {exc}"

        if attempt < retries:
            await asyncio.sleep(backoff_s * (2**attempt))  # exponential backoff

    return ToolOutcome(
        ok=False,
        result=None,
        error=last_error,
        duration_ms=int((time.perf_counter() - started) * 1000),
        attempts=attempts,
    )
