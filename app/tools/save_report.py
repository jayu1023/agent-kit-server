"""The write tool. Everything about the human-in-the-loop path exists for this.

It is a real side effect — a file appears on disk — rather than a simulated one,
because an approval gate that guards nothing is not tested by anything. The
graph interrupts before this is ever invoked; see `WRITE_TOOLS` in the registry
and `act()` in `app/graph.py`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from ..config import settings

_SAFE_SLUG = re.compile(r"[^a-z0-9._-]+")


class SaveReportArgs(BaseModel):
    title: str = Field(max_length=120, description="Short report title.")
    body: str = Field(max_length=20_000, description="Report body, markdown.")


def _slug(title: str) -> str:
    # Path traversal is the obvious attack on a model-chosen filename:
    # title="../../.ssh/authorized_keys" must not escape reports_dir.
    slug = _SAFE_SLUG.sub("-", title.lower()).strip("-.") or "report"
    return slug[:60]


@tool("save_report", args_schema=SaveReportArgs)
def save_report(title: str, body: str) -> dict:
    """Write a report to disk. THIS IS A WRITE ACTION and requires human approval
    before it runs. Returns the path written and a byte count."""
    directory = settings().reports_dir
    directory.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = (directory / f"{stamp}-{_slug(title)}.md").resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError("refusing to write outside the reports directory")

    content = f"# {title}\n\n_Generated {stamp} by agent-kit-server._\n\n{body}\n"
    path.write_text(content, encoding="utf-8")
    return {
        "path": str(path.relative_to(settings().reports_dir.parents[1])),
        "bytes": len(content.encode()),
        "meta": json.dumps({"title": title, "written_at": stamp}),
    }
