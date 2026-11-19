"""Read-only SQL over a CSV, via an in-memory SQLite table.

Why SQL and not a pandas DSL: an LLM writes SQL far better than it writes any
bespoke filter language, SQL is self-documenting in the tool-call card the
Flutter client renders, and SQLite enforces read-only at the connection level
so the sandbox is the database's job rather than a regex's.

Defence in depth, in order of reliability:
  1. `authorizer` callback  -- SQLite itself refuses anything but SELECT/READ
  2. statement count check  -- one statement only, no `;` smuggling
  3. a LIMIT is appended    -- an unbounded SELECT is a memory incident
"""

from __future__ import annotations

import csv
import sqlite3
from functools import lru_cache

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from ..config import settings

TABLE = "tickets"
MAX_ROWS = 50

_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}

SCHEMA_DOC = (
    f"table {TABLE}(ticket_id TEXT, created_at TEXT, tenant TEXT, "
    "plan TEXT CHECK in (enterprise,business,self_serve), category TEXT CHECK in "
    "(billing,ingestion,api,dashboards,how_to), severity TEXT CHECK in (sev1,sev2,sev3), "
    "status TEXT CHECK in (open,closed), first_response_mins INT, "
    "resolution_hours REAL NULL, escalated_to_eng INT, csat INT NULL)"
)


def _authorizer(action: int, *_args) -> int:
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


@lru_cache
def _connection() -> sqlite3.Connection:
    path = settings().tickets_csv
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise RuntimeError(f"{path} is empty")

    columns = list(rows[0])
    conn.execute(f"CREATE TABLE {TABLE} ({', '.join(f'{c} TEXT' for c in columns)})")
    conn.executemany(
        f"INSERT INTO {TABLE} VALUES ({', '.join('?' * len(columns))})",
        [[(r[c] if r[c] != "" else None) for c in columns] for r in rows],
    )
    conn.commit()
    # Authorizer is installed *after* the load, so the loader can write and
    # every query afterwards cannot.
    conn.set_authorizer(_authorizer)
    return conn


class QueryArgs(BaseModel):
    sql: str = Field(
        description=f"A single read-only SELECT against {SCHEMA_DOC}. "
        "Numeric columns are TEXT, so CAST explicitly: "
        "CAST(first_response_mins AS INTEGER). No semicolons, no writes.",
    )


@tool("query_tickets", args_schema=QueryArgs)
def query_tickets(sql: str) -> str:
    """Run one read-only SELECT over the support-ticket table and return the rows
    as pipe-delimited text. Use this for any question about ticket counts,
    response times, severities, plans or escalation rates."""
    statement = sql.strip().rstrip(";").strip()
    if ";" in statement:
        raise ValueError("one statement only")
    if not statement.lower().startswith(("select", "with")):
        raise ValueError("only SELECT / WITH queries are allowed")

    if " limit " not in statement.lower():
        statement = f"{statement} LIMIT {MAX_ROWS}"

    try:
        cursor = _connection().execute(statement)
    except sqlite3.DatabaseError as exc:
        # "not authorized" is what the authorizer raises; make it legible so the
        # model can correct itself on the retry instead of looping.
        raise ValueError(f"query rejected: {exc}") from exc

    headers = [d[0] for d in cursor.description]
    rows = cursor.fetchall()
    if not rows:
        return "0 rows"

    body = "\n".join(" | ".join("" if v is None else str(v) for v in row) for row in rows)
    return f"{' | '.join(headers)}\n{body}\n({len(rows)} rows)"
