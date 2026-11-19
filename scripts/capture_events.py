#!/usr/bin/env python
"""Capture a full run's agent_kit frames to a JSONL fixture.

The fixture is consumed by `clients/flutter_smoke` and decoded with the real
`agent_kit` Dart models. Generating it from the real server rather than writing
it by hand is the whole point: a hand-written fixture tests that the fixture
matches the client, which proves nothing. This tests that *the server's actual
output* matches the client.

Covers both paths in one file: a read-only run, and a write run that pauses for
approval and is then approved.

    python scripts/capture_events.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langgraph.types import Command  # noqa: E402

from app.graph import build_graph  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "clients" / "flutter_smoke" / "test" / "fixtures" / "events.jsonl"

READ_ONLY_Q = "How does average first response time compare across plans?"
WRITE_Q = "What is our CSAT by plan and save a report about it"


async def capture() -> list[dict]:
    frames: list[dict] = []
    graph = build_graph()

    async def drain(config, payload) -> bool:
        """Returns True if the run paused for approval."""
        paused = False
        async for mode, chunk in graph.astream(
            payload, config=config, stream_mode=["custom", "updates"]
        ):
            if mode == "custom":
                frames.append(chunk)
            elif mode == "updates" and isinstance(chunk, dict) and "__interrupt__" in chunk:
                paused = bool(chunk["__interrupt__"])
        return paused

    config = {"configurable": {"thread_id": "capture-readonly"}}
    await drain(config, {"question": READ_ONLY_Q})

    config = {"configurable": {"thread_id": "capture-write"}}
    paused = await drain(config, {"question": WRITE_Q})
    if not paused:
        raise SystemExit("expected the write run to pause for approval")
    await drain(config, Command(resume={"approved": True, "note": "captured"}))

    return frames


def main() -> int:
    frames = asyncio.run(capture())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(json.dumps(f, ensure_ascii=False, default=str) for f in frames) + "\n")

    counts: dict[str, int] = {}
    for frame in frames:
        counts[frame["type"]] = counts.get(frame["type"], 0) + 1
    print(f"wrote {len(frames)} frames to {OUT.relative_to(Path.cwd())}")
    for kind, count in sorted(counts.items()):
        print(f"  {kind:<10} {count}")

    missing = {"plan", "thinking", "tool_call", "citations"} - set(counts)
    if missing:
        raise SystemExit(f"fixture does not cover every event type: missing {missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
