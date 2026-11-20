#!/usr/bin/env python
"""Render a JSONL trace file as a tree. The README's trace evidence comes from here.

    TRACE_EXPORTER=file python -m app.trace_tree --demo
    TRACE_EXPORTER=file python -m app.trace_tree --demo --write
    python -m app.trace_tree data/trace.jsonl

A rendered tree rather than a screenshot, deliberately: it is diffable, it
survives in a README without an image host, and anyone can regenerate it with
one command and check it matches. A screenshot of a trace UI proves only that
a screenshot exists.

For an agent this tree is the point. "Why did that run take nine seconds and
call search twice" is answered by the shape of the traversal, not by reading
interleaved logs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Attributes worth printing inline. Everything else stays in the JSON.
INTERESTING = (
    "agent.planner", "agent.steps", "agent.tools", "agent.step_id", "agent.tool",
    "agent.requires_approval", "agent.outcome", "agent.approved",
    "tool.ok", "tool.attempts", "tool.error",
    "agent.steps_taken", "agent.total_steps", "agent.failed_steps", "agent.budget",
    "agent.successes", "agent.failures", "agent.halt_reason",
)


def load(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"no trace file at {path} — run with TRACE_EXPORTER=file first")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def render(spans: list[dict]) -> str:
    if not spans:
        return "(no spans)"

    by_parent: dict[str | None, list[dict]] = {}
    for s in spans:
        by_parent.setdefault(s["parent_id"], []).append(s)
    for children in by_parent.values():
        children.sort(key=lambda s: s["start"])

    known = {s["span_id"] for s in spans}
    # A span whose parent was never exported is a root for rendering purposes.
    roots = [s for s in spans if s["parent_id"] is None or s["parent_id"] not in known]
    roots.sort(key=lambda s: s["start"])

    total = max(s["end"] for s in spans) - min(s["start"] for s in spans)
    total_ms = total / 1e6 or 1.0
    lines: list[str] = []

    def walk(span: dict, prefix: str, is_last: bool, depth: int) -> None:
        connector = "" if depth == 0 else ("└─ " if is_last else "├─ ")
        share = span["duration_ms"] / total_ms * 100
        bar = "█" * max(1, round(share / 5)) if share >= 2.5 else ""
        def fmt(key: str) -> str:
            value = span["attributes"][key]
            if isinstance(value, str) and len(value) > 60:
                value = value[:57] + "..."
            return f"{key.split('.', 1)[1]}={value}"

        attrs = " ".join(fmt(k) for k in INTERESTING if k in span["attributes"])
        lines.append(
            f"{prefix}{connector}{span['name']:<22} {span['duration_ms']:>8.2f}ms "
            f"{share:>5.1f}% {bar:<20} {attrs}"
        )
        kids = by_parent.get(span["span_id"], [])
        child_prefix = prefix + ("" if depth == 0 else ("   " if is_last else "│  "))
        for i, kid in enumerate(kids):
            walk(kid, child_prefix, i == len(kids) - 1, depth + 1)

    for root in roots:
        walk(root, "", True, 0)
    lines.append("")
    lines.append(f"{len(spans)} spans, {total_ms:.2f}ms wall clock")
    return "\n".join(lines)


def demo() -> int:
    """Run one read-only agent run and one write run that pauses for approval."""
    import asyncio

    os.environ.setdefault("TRACE_EXPORTER", "file")
    trace_file = os.environ.setdefault("TRACE_FILE", str(ROOT / "data" / "trace.jsonl"))
    sys.path.insert(0, str(ROOT))

    from langgraph.types import Command

    from app.graph import build_graph
    from app.tracing import flush

    async def run(graph, config, payload) -> bool:
        paused = False
        async for mode, chunk in graph.astream(
            payload, config=config, stream_mode=["custom", "updates"]
        ):
            if mode == "updates" and isinstance(chunk, dict) and "__interrupt__" in chunk:
                paused = bool(chunk["__interrupt__"])
        return paused

    async def main_async() -> tuple[str, str]:
        graph = build_graph()

        question = "How does average first response time compare across plans?"
        open(trace_file, "w").close()
        await run(graph, {"configurable": {"thread_id": "trace-readonly"}}, {"question": question})
        flush()
        read_only = render(load(Path(trace_file)))

        write_question = "What is our CSAT by plan and save a report about it"
        config = {"configurable": {"thread_id": "trace-write"}}
        open(trace_file, "w").close()
        paused = await run(graph, config, {"question": write_question})
        assert paused, "expected the write run to pause for approval"
        await run(graph, config, Command(resume={"approved": True, "note": "traced"}))
        flush()
        write_run = render(load(Path(trace_file)))

        return read_only, write_run

    read_only, write_run = asyncio.run(main_async())

    out = []
    out.append("Run 1 — read-only (search -> SQL -> calculator), no approval needed")
    out.append("")
    out.append(read_only)
    out.append("")
    out.append("Run 2 — write action: pauses for approval, then resumes")
    out.append("")
    out.append(write_run)
    rendered = "\n".join(out)
    print(rendered)

    if "--write" in sys.argv:
        target = ROOT / "docs" / "TRACE.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "# Captured traces\n\n"
            "Generated by `TRACE_EXPORTER=file python -m app.trace_tree --demo --write`.\n"
            "Regenerate and diff to check these are current.\n\n"
            "```\n" + rendered + "\n```\n"
        )
        print(f"\nwrote {target.relative_to(ROOT)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default=str(ROOT / "data" / "trace.jsonl"))
    parser.add_argument("--demo", action="store_true", help="run two traced agent runs, then render")
    parser.add_argument("--write", action="store_true", help="also write docs/TRACE.md")
    args = parser.parse_args()
    if args.demo:
        return demo()
    print(render(load(Path(args.path))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
