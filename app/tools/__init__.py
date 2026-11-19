"""Tool registry.

Four tools, three read-only and one write. The write tool is marked with
`WRITE_TOOLS` and that set is what the graph's human-in-the-loop interrupt
keys on — so "requires approval" is a property of the tool, not something each
call site has to remember.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from .calculator import calculator
from .csv_query import query_tickets
from .save_report import save_report
from .web_search import web_search

ALL_TOOLS: list[BaseTool] = [web_search, query_tickets, calculator, save_report]

TOOLS_BY_NAME: dict[str, BaseTool] = {t.name: t for t in ALL_TOOLS}

#: Tools with side effects. The graph interrupts for approval before any of these.
WRITE_TOOLS: frozenset[str] = frozenset({"save_report"})


def is_write_tool(name: str) -> bool:
    return name in WRITE_TOOLS


def tool_schemas() -> list[dict]:
    """JSON schemas for every tool — what `GET /tools` serves and what the LLM
    planner is given. Generated from the Pydantic args models, never hand-written,
    so the schema cannot drift from the function signature."""
    return [
        {
            "name": t.name,
            "description": t.description,
            "requires_approval": is_write_tool(t.name),
            "args_schema": t.args_schema.model_json_schema() if t.args_schema else {},
        }
        for t in ALL_TOOLS
    ]
