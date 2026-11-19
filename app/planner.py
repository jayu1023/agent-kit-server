"""Turns a question into a plan: a list of UI steps plus the tool call each one
makes.

Two implementations behind one function.

**`heuristic` (default)** — intent keywords to a tool and arguments. It is not
pretending to be an LLM; it exists because a deterministic planner is what lets
the test suite assert on an exact plan and lets the repo demo with no API key.
It recognises five intents and falls back to search-only.

**`llm`** — one structured-output call that picks tools and arguments. It is
validated against the real tool registry before anything runs: a hallucinated
tool name or a schema violation is rejected here, at the planning boundary,
rather than discovered when `act()` tries to invoke it. That validation is a
guardrail, not error handling — it is the difference between "the model can
call tools" and "the model can call *these* tools with *these* arguments".

**Late binding.** Some arguments cannot be known at plan time: the calculator's
expression depends on numbers the SQL step has not returned yet. Those actions
carry a `derive` key instead of final `args`, and `act()` resolves it against
the observations collected so far. Planning the *shape* early and the *values*
late is what keeps the plan renderable in the UI from the first event.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .config import settings
from .events import PlanStep, PlanStepState
from .state import Action
from .tools import TOOLS_BY_NAME, tool_schemas

# --------------------------------------------------------------------------- #
# heuristic planner
# --------------------------------------------------------------------------- #
# A metrics question needs BOTH a subject and an aggregation word. Requiring
# only a subject made "Can a support ticket page on-call?" run a pointless SQL
# step, because "ticket" matched -- a policy question dressed as a metrics one.
METRIC_SUBJECTS = (
    "ticket", "response", "csat", "escalat", "resolution", "severity", "sev",
    "backlog", "plan", "tenant", "category",
)
METRIC_AGGREGATIONS = (
    "average", "avg", "mean", "count", "how many", "how much", "rate", "total",
    "volume", "percent", "%", "compare", "versus", " vs", "ratio", "trend",
    "highest", "lowest", "slowest", "fastest", "worst", "best", "most", "by plan",
    "by category", "by severity", "breakdown", "open", "distribution",
)
COMPARE_WORDS = ("compare", "percent", "%", "difference", "versus", " vs", "ratio", "slower", "faster", "times")
WRITE_WORDS = ("save", "write a report", "write report", "document this", "file a report", "persist")

SQL_BY_INTENT: list[tuple[tuple[str, ...], str, str]] = [
    (
        ("csat", "satisfaction"),
        "Query CSAT by plan (closed tickets only)",
        "SELECT plan, COUNT(csat) AS n, ROUND(AVG(CAST(csat AS REAL)), 1) AS avg_csat "
        "FROM tickets WHERE status = 'closed' AND csat IS NOT NULL "
        "GROUP BY plan ORDER BY avg_csat",
    ),
    (
        ("escalat",),
        "Query escalation rate by category",
        "SELECT category, COUNT(*) AS n, "
        "ROUND(100.0 * SUM(CAST(escalated_to_eng AS INTEGER)) / COUNT(*), 1) AS escalated_pct "
        "FROM tickets GROUP BY category ORDER BY escalated_pct DESC",
    ),
    (
        ("resolution", "resolve"),
        "Query resolution hours by severity (closed tickets only)",
        "SELECT severity, COUNT(*) AS n, "
        "ROUND(AVG(CAST(resolution_hours AS REAL)), 1) AS avg_resolution_hours "
        "FROM tickets WHERE status = 'closed' AND resolution_hours IS NOT NULL "
        "GROUP BY severity ORDER BY severity",
    ),
    (
        ("open", "backlog"),
        "Query open tickets by plan",
        "SELECT plan, COUNT(*) AS open_tickets FROM tickets "
        "WHERE status = 'open' GROUP BY plan ORDER BY open_tickets DESC",
    ),
    (
        ("response", "frt", "first"),
        "Query first-response minutes by plan",
        "SELECT plan, COUNT(*) AS n, "
        "ROUND(AVG(CAST(first_response_mins AS REAL)), 1) AS avg_first_response_mins "
        "FROM tickets GROUP BY plan ORDER BY avg_first_response_mins",
    ),
]

FALLBACK_SQL = (
    "Query ticket volume by plan and severity",
    "SELECT plan, severity, COUNT(*) AS n FROM tickets GROUP BY plan, severity "
    "ORDER BY plan, severity",
)


def _matches(question: str, words: tuple[str, ...]) -> bool:
    lowered = question.lower()
    return any(w in lowered for w in words)


def heuristic_plan(question: str) -> tuple[list[PlanStep], list[Action]]:
    steps: list[PlanStep] = []
    actions: list[Action] = []

    def add(label: str, tool: str, args: dict[str, Any] | None = None, derive: str | None = None) -> None:
        step_id = f"s{len(steps) + 1}"
        steps.append(PlanStep(id=step_id, label=label, state=PlanStepState.pending))
        action: Action = {"step_id": step_id, "tool": tool, "args": args or {}}
        if derive:
            action["derive"] = derive  # type: ignore[typeddict-unknown-key]
        actions.append(action)

    # 1. Always ground in documented policy before touching numbers.
    add(
        "Search policy documents for context",
        "web_search",
        {"query": question, "limit": 3},
    )

    # 2. Numbers, if the question is about them.
    if _matches(question, METRIC_SUBJECTS) and _matches(question, METRIC_AGGREGATIONS):
        label, sql = FALLBACK_SQL
        for words, intent_label, intent_sql in SQL_BY_INTENT:
            if _matches(question, words):
                label, sql = intent_label, intent_sql
                break
        add(label, "query_tickets", {"sql": sql})

        # 3. Arithmetic the model should not do in its head. Late-bound: the
        #    expression needs the rows step 2 has not produced yet.
        if _matches(question, COMPARE_WORDS):
            add(
                "Compute the comparison from the query result",
                "calculator",
                derive="ratio_from_last_table",
            )

    # 4. The write action, gated on human approval by the graph.
    if _matches(question, WRITE_WORDS):
        add(
            "Save the findings as a report (requires approval)",
            "save_report",
            derive="report_from_observations",
        )

    return steps, actions


# --------------------------------------------------------------------------- #
# late-binding argument resolvers
# --------------------------------------------------------------------------- #
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _last_table(observations: list[dict]) -> str | None:
    for obs in reversed(observations):
        if obs["tool"] == "query_tickets" and obs["ok"]:
            return str(obs["result"])
    return None


def _ratio_from_last_table(question: str, observations: list[dict]) -> dict[str, Any]:
    """Build a ratio expression from the first and last data rows of the last
    SQL result. The rows are ordered by the metric, so first vs last is the
    widest gap in the table -- which is the comparison these questions ask for."""
    table = _last_table(observations)
    if not table:
        raise ValueError("no successful query_tickets result to compare")

    rows = [ln for ln in table.splitlines()[1:] if "|" in ln]
    if len(rows) < 2:
        raise ValueError("need at least two data rows to compare")

    def last_number(row: str) -> float:
        found = _NUMBER.findall(row.split("|")[-1])
        if not found:
            raise ValueError(f"no numeric value in row: {row.strip()!r}")
        return float(found[-1])

    low, high = last_number(rows[0]), last_number(rows[-1])
    if low == 0:
        raise ValueError("cannot divide by a zero baseline")
    return {"expression": f"{high} / {low}"}


def _report_from_observations(question: str, observations: list[dict]) -> dict[str, Any]:
    lines = [f"**Question:** {question}", ""]
    for obs in observations:
        status = "ok" if obs["ok"] else f"failed: {obs['error']}"
        lines.append(f"## {obs['tool']} ({status}, {obs['duration_ms']}ms)")
        lines.append("")
        lines.append(f"```\n{str(obs['result'])[:2000]}\n```" if obs["ok"] else "")
        lines.append("")
    title = question.strip().rstrip("?")[:110] or "Agent report"
    return {"title": title, "body": "\n".join(lines)}


RESOLVERS = {
    "ratio_from_last_table": _ratio_from_last_table,
    "report_from_observations": _report_from_observations,
}


def resolve_args(action: Action, question: str, observations: list[dict]) -> dict[str, Any]:
    derive = action.get("derive")  # type: ignore[attr-defined]
    if not derive:
        return dict(action["args"])
    resolver = RESOLVERS.get(derive)
    if resolver is None:
        raise ValueError(f"unknown resolver: {derive}")
    return resolver(question, observations)


# --------------------------------------------------------------------------- #
# llm planner
# --------------------------------------------------------------------------- #
PLANNER_SYSTEM = """You plan tool calls for a support-analytics agent.

Return JSON only: {"steps":[{"label":"...","tool":"...","args":{...}}]}

Rules:
- `tool` must be one of the provided tool names. Never invent a tool.
- `args` must satisfy that tool's JSON schema exactly.
- Ground policy claims with web_search before asserting them.
- Use query_tickets for any number. Use calculator for any arithmetic.
- Only include save_report if the user explicitly asked for something saved.
- 2 to 4 steps. Fewer is better."""


def _validate(raw_steps: list[dict]) -> tuple[list[PlanStep], list[Action]]:
    """Reject anything the registry does not recognise, before it can execute."""
    steps: list[PlanStep] = []
    actions: list[Action] = []

    for index, raw in enumerate(raw_steps[:6], start=1):
        name = raw.get("tool")
        if name not in TOOLS_BY_NAME:
            raise ValueError(f"planner chose an unknown tool: {name!r}")
        tool = TOOLS_BY_NAME[name]
        args = raw.get("args") or {}
        if tool.args_schema is not None:
            # Raises ValidationError on a schema violation -> caught by the
            # caller and the whole plan is discarded.
            args = tool.args_schema.model_validate(args).model_dump()

        step_id = f"s{index}"
        label = str(raw.get("label") or f"Call {name}")[:120]
        steps.append(PlanStep(id=step_id, label=label, state=PlanStepState.pending))
        actions.append({"step_id": step_id, "tool": name, "args": args})

    if not steps:
        raise ValueError("planner returned no steps")
    return steps, actions


def llm_plan(question: str) -> tuple[list[PlanStep], list[Action]]:
    from openai import OpenAI

    cfg = settings()
    client = OpenAI(api_key=cfg.openai_api_key)
    response = client.chat.completions.create(
        model=cfg.planner_model,
        temperature=0.0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": PLANNER_SYSTEM},
            {
                "role": "user",
                "content": f"Tools:\n{json.dumps(tool_schemas(), indent=2)}\n\nQuestion: {question}",
            },
        ],
    )
    payload = json.loads(response.choices[0].message.content or "{}")
    return _validate(payload.get("steps") or [])


def make_plan(question: str) -> tuple[list[PlanStep], list[Action], str]:
    """Returns (steps, actions, planner_used).

    The LLM planner falls back to the heuristic rather than failing the request:
    a planner outage should degrade the plan, not drop the user's question. The
    name of the planner actually used is returned so the UI and the logs can say
    which one ran instead of quietly implying the LLM.
    """
    cfg = settings()
    if cfg.planner == "llm" and cfg.openai_api_key:
        try:
            steps, actions = llm_plan(question)
            return steps, actions, "llm"
        except Exception as exc:
            steps, actions = heuristic_plan(question)
            return steps, actions, f"heuristic (llm planner failed: {type(exc).__name__})"
    return (*heuristic_plan(question), "heuristic")
