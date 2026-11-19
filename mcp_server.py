#!/usr/bin/env python
"""MCP server exposing this agent's tools to any MCP client (Claude Code,
Claude Desktop, Cursor, …).

The point is that the tool definitions are **not duplicated**. Each
`langchain_core` tool the LangGraph agent calls is registered here by wrapping
the same object, with the input schema taken from the same Pydantic model. Add
a tool to `app/tools/__init__.py` and it appears over MCP with no edit to this
file — which is the only version of "we support MCP" that does not rot.

Transport is stdio, which is what MCP clients launch. Register with:

    claude mcp add agent-kit-server -- /abs/path/.venv/bin/python /abs/path/mcp_server.py

`save_report` is exposed and annotated `readOnlyHint: false`. **MCP has no
equivalent of the graph's approval interrupt** — the host application owns that
decision, and Claude Code/Desktop prompt for tool permission themselves. The
annotation is what lets a host do that; it is not a gate this process enforces.

Written against the `mcp` 2.x SDK, where FastMCP was renamed `MCPServer`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from app.tools import ALL_TOOLS, is_write_tool

# stdio IS the protocol channel: anything on stdout that is not JSON-RPC
# corrupts the session, so logs go to stderr. This is the single most common way
# to break an MCP server.
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
log = logging.getLogger("mcp")

server = MCPServer(
    name="agent-kit-server",
    version="0.1.0",
    instructions=(
        "Support-analytics tools over a fictional company's policy docs and ticket "
        "table. Use web_search for documented policy, query_tickets for any number, "
        "calculator for arithmetic. save_report writes a file to disk."
    ),
)


def _register(tool: Any) -> None:
    """Wrap one langchain tool as an MCP tool.

    The wrapper exists because MCP infers the schema from the *function
    signature*, while a langchain tool carries its schema on an attribute. So
    the wrapper takes **kwargs and the real schema is attached explicitly via
    `add_tool`'s structured metadata path — the Pydantic model stays the one
    source of truth either way.
    """
    schema = tool.args_schema

    async def _invoke(**kwargs: Any) -> str:
        # Validate against the tool's own model first: an MCP client is just as
        # capable of sending the wrong shape as an LLM planner is.
        args = schema.model_validate(kwargs).model_dump() if schema else kwargs
        result = await asyncio.to_thread(tool.invoke, args)
        return result if isinstance(result, str) else json.dumps(result, indent=2, default=str)

    _invoke.__name__ = tool.name
    _invoke.__doc__ = (tool.description or "").strip()
    # Give the wrapper the real parameters so MCP's schema inference sees them.
    if schema is not None:
        _invoke.__annotations__ = {
            name: field.annotation for name, field in schema.model_fields.items()
        } | {"return": str}
        _invoke.__signature__ = __import__("inspect").Signature(
            [
                __import__("inspect").Parameter(
                    name,
                    __import__("inspect").Parameter.KEYWORD_ONLY,
                    annotation=field.annotation,
                    default=(
                        field.default
                        if field.default is not __import__("pydantic_core").PydanticUndefined
                        else __import__("inspect").Parameter.empty
                    ),
                )
                for name, field in schema.model_fields.items()
            ],
            return_annotation=str,
        )

    read_only = not is_write_tool(tool.name)
    server.add_tool(
        _invoke,
        name=tool.name,
        description=(tool.description or "").strip(),
        # snake_case on the Python model, camelCase on the wire (mcp 2.x).
        annotations=ToolAnnotations(
            read_only_hint=read_only,
            destructive_hint=False,
            idempotent_hint=read_only,
        ),
    )


for _tool in ALL_TOOLS:
    _register(_tool)


if __name__ == "__main__":
    server.run(transport="stdio")
