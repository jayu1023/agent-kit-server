"""Specialist definitions for the supervisor architecture.

A specialist is a named agent with a **restricted tool set** and its **own step
budget**. The restriction is the point: a specialist that can reach every tool
is not a specialist, it is the single agent with a label on it.

Three of them, split along the line that actually matters -- what a step costs
and whether it can change anything:

  researcher   read-only, cheap, no side effects        -> web_search
  analyst      read-only, computes over data            -> query_tickets, calculator
  scribe       WRITES. every call needs human approval  -> save_report

That last row is why the split is worth having at all. With one agent, "can
this run write to disk?" is a question about the current step. With specialists
it is a question about which specialist was dispatched, which is decidable
before anything runs and is visible in the plan the UI renders.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .tools import TOOLS_BY_NAME, WRITE_TOOLS


@dataclass(frozen=True)
class Specialist:
    name: str
    description: str
    tools: tuple[str, ...]
    #: Per-specialist cap. A shared budget lets one specialist starve the others,
    #: which is the classic multi-agent failure and the reason this is not global.
    max_steps: int = 3
    keywords: tuple[str, ...] = field(default_factory=tuple)

    @property
    def can_write(self) -> bool:
        return bool(set(self.tools) & WRITE_TOOLS)

    def __post_init__(self) -> None:
        unknown = set(self.tools) - set(TOOLS_BY_NAME)
        if unknown:
            raise ValueError(f"{self.name} declares unknown tools: {sorted(unknown)}")


RESEARCHER = Specialist(
    name="researcher",
    description="Finds documented policy and process in the knowledge base. Read-only.",
    tools=("web_search",),
    max_steps=2,
    keywords=(
        "policy", "procedure", "rule", "sla", "runbook", "documented", "guideline",
        "what is the", "how do we", "are we allowed", "should we", "process",
    ),
)

ANALYST = Specialist(
    name="analyst",
    description="Answers questions about the ticket data with SQL and arithmetic. Read-only.",
    tools=("query_tickets", "calculator"),
    max_steps=3,
    keywords=(
        "average", "avg", "mean", "count", "how many", "how much", "rate", "total",
        "percent", "%", "compare", "versus", " vs", "ratio", "trend", "highest",
        "lowest", "slowest", "fastest", "worst", "best", "most", "breakdown",
        "by plan", "by category", "by severity", "distribution", "open",
    ),
)

SCRIBE = Specialist(
    name="scribe",
    description="Writes findings to a report file. Every call requires human approval.",
    tools=("save_report",),
    max_steps=1,
    keywords=("save", "write a report", "write report", "document this", "file a report", "persist"),
)

SPECIALISTS: tuple[Specialist, ...] = (RESEARCHER, ANALYST, SCRIBE)
BY_NAME: dict[str, Specialist] = {s.name: s for s in SPECIALISTS}

#: The supervisor may never dispatch a writer before a reader: a report with
#: nothing in it is the failure mode, and ordering is cheaper than detecting it.
DISPATCH_ORDER = {"researcher": 0, "analyst": 1, "scribe": 2}


def ordered(names: list[str]) -> list[str]:
    return sorted(dict.fromkeys(names), key=lambda n: DISPATCH_ORDER.get(n, 99))
