# agent-kit-server

The backend [`agent_kit`](https://pub.dev/packages/agent_kit) was missing.

`agent_kit` ships the Flutter widgets for the non-text parts of an agent UI —
tool-call cards, thinking bubbles, citation chips, plan timelines — and is
backend-agnostic, which means every adopter has had to write the agent loop
themselves. This is that loop: a LangGraph agent whose SSE stream decodes
directly into `AgentEvent`, with four real tools, a human approval gate before
any write, and the same tools exposed over MCP.

```dart
AgentView(stream: agentKitServerEvents(question: 'How does CSAT compare across plans?'))
```

It runs with **no API key, no network and no Docker**: the default planner is
deterministic and `web_search` searches a bundled corpus. `make install && make test`
is the whole setup.

**Docs:** [`docs/EXPLAINER.md`](docs/EXPLAINER.md) — how each piece works and why.
[`docs/INTERVIEW_GUIDE.md`](docs/INTERVIEW_GUIDE.md) — how to present and defend it.
[`docs/TRACE.md`](docs/TRACE.md) — real captured span trees, including the approval pause.

---

## The graph

```
                  ┌──────────────────────────────────────────────┐
  POST /runs ────▶│ plan                                         │
                  │  heuristic | llm → PlanStep[] + Action[]      │
                  │  emits: plan, thinking                        │
                  └───────────────────┬──────────────────────────┘
                                      ▼
                  ┌──────────────────────────────────────────────┐
         ┌───────▶│ act                                          │
         │        │  resolve args (late-binding resolvers)        │
         │        │  write tool? ──▶ interrupt() ──▶ PAUSE        │
         │        │  emits: plan(running), tool_call(pending|…)    │
         │        └───────────────────┬──────────────────────────┘
         │                            ▼
         │        ┌──────────────────────────────────────────────┐
         │        │ observe                                      │
         │        │  run_tool: timeout + bounded retry            │
         │        │  emits: tool_call(terminal), plan, citations   │
         │        └───────────────────┬──────────────────────────┘
         │                            ▼
         │        ┌──────────────────────────────────────────────┐
         │        │ reflect                                      │
         └────────┤  advance cursor · enforce MAX_STEPS           │
       more steps │  emits: thinking                              │
                  └───────────────────┬──────────────────────────┘
                                      ▼ halt_reason set
                  ┌──────────────────────────────────────────────┐
                  │ answer → control: done                       │
                  └──────────────────────────────────────────────┘

  POST /runs/{id}/approve ──▶ Command(resume=…) ──▶ re-enters `act`
```

| File | Responsibility |
|---|---|
| `app/events.py` | the `agent_kit` wire protocol — the **only** module that knows the Dart shapes |
| `app/state.py` | the graph's state; what survives the approval round-trip |
| `app/planner.py` | heuristic + LLM planners, the tool allowlist, late-binding resolvers |
| `app/graph.py` | the four nodes, the interrupt, the loop guard |
| `app/guards.py` | tool timeout, bounded retry, the permanent/transient split |
| `app/tools/` | `web_search`, `query_tickets`, `calculator`, `save_report` |
| `app/main.py` | `POST /runs` (SSE), `POST /runs/{id}/approve`, `GET /runs/{id}`, `/tools`, `/health` |
| `mcp_server.py` | the same tool objects over MCP stdio — no duplicated definitions |
| `app/tracing.py` | OpenTelemetry spans; file / OTLP exporters, off by default |
| `app/trace_tree.py` | renders a JSONL trace as the span trees in `docs/TRACE.md` |
| `clients/flutter_smoke/` | Dart tests that decode real server output with the real package |

---

## Quick start

```bash
make venv install
make test                    # 94 python tests
make test-dart               # regenerate the fixture, then 8 Dart wire tests

make dev                     # uvicorn on :8000
make ask Q="How does average first response time compare across plans?"
make ask Q="What is our CSAT by plan and save a report about it"   # pauses
make approve                 # or: make approve OK=false
```

---

## The four tools

| Tool | What it does | Sandbox |
|---|---|---|
| `web_search` | passages from the policy corpus | provider-pluggable: `offline` (default, deterministic) or `tavily` |
| `query_tickets` | one read-only SELECT over a 35-row ticket table | SQLite `authorizer` callback, single-statement check, forced `LIMIT` |
| `calculator` | arithmetic | AST allowlist — no names, attributes or calls are reachable |
| `save_report` | **writes a file** | path-traversal check; **requires human approval** |

Schemas are generated from the Pydantic args models, never hand-written, so a
schema cannot drift from a function signature. `GET /tools` serves them, the LLM
planner is given them, and MCP advertises them — one definition, three consumers.

### The sandboxes are the interesting part

`calculator` does not use `eval()`, because `__import__('os').system('rm -rf /')`
is a valid Python expression and an LLM will eventually emit one. It walks the
AST and rejects every node type that is not a number, an operator or a
parenthesis, so there is no reachable path to a call. `2 ** 10 ** 9` is rejected
separately — that one is a denial of service, not an injection.

`query_tickets` installs a SQLite `set_authorizer` callback that permits only
`SQLITE_SELECT`/`SQLITE_READ`/`SQLITE_FUNCTION`. The database refuses writes
itself, which is a stronger guarantee than a regex over the SQL string. The
single-statement check stops `SELECT 1; DROP TABLE tickets`, and a `LIMIT` is
appended when the model omits one.

`save_report` treats the model-chosen filename as untrusted input:
`title="../../.ssh/authorized_keys"` is slugified and then checked with
`is_relative_to` against the reports directory.

---

## Guards, and where each one lives

Deliberately in the graph and in one shared execution path — never bolted onto
individual call sites, because a guard you have to remember is a guard you will
forget on one branch.

| Guard | Where | What it stops |
|---|---|---|
| step budget (`MAX_STEPS`) | `reflect()` | an agent that never terminates |
| per-tool timeout | `guards.run_tool` | one hung tool holding a request open |
| bounded retry + **permanent/transient split** | `guards.run_tool` | retrying a bad argument forever |
| approval before writes | `act()`, via `interrupt()` | a side effect nobody sanctioned |
| tool allowlist + arg schema validation | `planner._validate` | a hallucinated tool or argument ever executing |
| failures do not cascade | `reflect()` | one bad step discarding a usable answer |

**The permanent/transient split is the one worth arguing about.** Retrying
`SELECT * FROM ticekts` three times produces the same typo three times, spends
the step budget and hides the real problem. So `ValueError`/`TypeError`/
`ValidationError` fail immediately and are reported back in a legible form the
model can correct from; timeouts and transport errors get exponential backoff.

**Validation is a guardrail, not error handling.** The LLM planner's output is
checked against the real tool registry *before* anything runs — an unknown tool
name or a schema violation discards the whole plan at the planning boundary. That
is the difference between "the model can call tools" and "the model can call
*these* tools with *these* arguments".

---

## Human-in-the-loop

`save_report` is in `WRITE_TOOLS`, so the graph interrupts before it runs.
Approval is a **separate HTTP request**, not a reply on the open stream:

```bash
curl -N -X POST :8000/runs -d '{"question":"... and save a report about it","run_id":"r1"}'
# … event: control  {"kind":"approval_required","tool":"save_report","args":{…}}
# stream ends here. The run is checkpointed, not waiting on the socket.

curl :8000/runs/r1          # {"awaiting_approval":[{"tool":"save_report",…}]}
curl -N -X POST :8000/runs/r1/approve -d '{"approved":true}'
curl -N -X POST :8000/runs/r1/approve -d '{"approved":false,"note":"needs legal review"}'
```

Because the state is in the checkpointer, the run survives the stream closing, a
reviewer taking an hour, and (with a durable checkpointer) a process restart.
Holding approval on the original connection would make a human decision depend on
a socket staying up.

A rejection is not a failed run: the write is marked `cancelled`, the read-only
work already done is kept, and the answer says a write was rejected and why.

### The LangGraph gotcha this surfaced

**After an interrupt, the node re-runs from the top on resume** — everything
above `interrupt()` executes twice. So `act()` emits exactly one thing before
interrupting: a `tool_call` whose id is stable, which the Flutter client renders
as the *same* pending card both times. A thinking bubble was emitted there
originally and appeared twice in the client, because `ThinkingStep` has no id to
dedupe on.

---

## MCP

The same four tools, over stdio, to any MCP client:

```bash
claude mcp add agent-kit-server -- /abs/path/.venv/bin/python /abs/path/mcp_server.py
```

The tool definitions are **not duplicated**. `mcp_server.py` iterates
`ALL_TOOLS` and registers each one with the input schema taken from the same
Pydantic model the LangGraph agent uses. Add a tool to `app/tools/__init__.py`
and it appears over MCP with no edit to that file — the only version of "we
support MCP" that does not rot.

`save_report` is advertised with `readOnlyHint: false`. **MCP has no equivalent
of the graph's approval interrupt** — the host application owns that decision,
and Claude Code/Desktop prompt for permission themselves. The annotation is what
lets a host do that; it is not a gate this process enforces.

---

## Wire compatibility is tested, not asserted

This is the part worth looking at. `agent_kit`'s `AgentEvent.fromJson` accepts
exactly four `type` tags and throws `FormatException` on anything else, with
snake_case field names and specific enum spellings. A typo here is a crash on
someone's phone.

So the contract is tested from the Dart side, against the real published package:

1. `python scripts/capture_events.py` runs the **real agent** — a read-only run,
   plus a write run that pauses and is approved — and dumps all 39 emitted frames
   to `clients/flutter_smoke/test/fixtures/events.jsonl`.
2. `cd clients/flutter_smoke && flutter test` decodes every frame with the real
   `agent_kit` models and asserts: every frame decodes; each decodes to the
   subtype its tag claims; **`fromJson` → `toJson` round-trips losslessly** (which
   catches a field the server sends and the Dart model silently drops); every
   tool-call id reaches a terminal state and never moves backwards out of one;
   every plan step terminates; every citation carries a parseable url.
3. CI runs both jobs, so the two sides cannot drift silently.

A hand-written fixture would only prove the fixture matches the client. Capturing
from the real server is what makes it prove anything.

**It found two real bugs**, both now regression-tested on both sides:

- **`observe()` updated the plan in state but never emitted it.** The checkpoint
  was right and the client was wrong: the last plan step stayed `running` forever
  in the Flutter timeline. Caught by "every plan step reaches a terminal state".
- **Tool-call ids were only unique within a run.** Two runs both start at step
  `s1`, so a client showing more than one run in a view would collide their
  cards. Ids now include the run id. Caught by a test decoding two concatenated
  runs from one fixture.

---

## Tracing

```bash
make trace     # two traced runs, rendered as span trees -> docs/TRACE.md
```

OpenTelemetry, **off by default** (`TRACE_EXPORTER=none`) — the no-op tracer makes every
`with span(...)` free, which is why no node needs an `if tracing_enabled` guard. Set
`file` for a JSONL span log you can render, or `otlp` to ship to any collector.

A rendered tree rather than a screenshot, deliberately: it is diffable, it needs no image
host, and anyone can regenerate it and check it matches. For an agent this tree *is* the
artifact — "why did that run take nine seconds and call search twice" is answered by the
shape of the traversal, not by reading interleaved logs.

```
Run 1 — read-only (search -> SQL -> calculator), no approval needed

agent.plan                 0.42ms   6.2% █                    planner=heuristic steps=3 tools=web_search,query_tickets,calculator
agent.act                  0.02ms   0.3%                      step_id=s1 tool=web_search requires_approval=False outcome=ready
tool.web_search            1.30ms  19.2% ████                 ok=True attempts=1
agent.reflect              0.01ms   0.1%                      steps_taken=1 total_steps=3 failed_steps=0 budget=8
agent.act                  0.01ms   0.1%                      step_id=s2 tool=query_tickets requires_approval=False outcome=ready
tool.query_tickets         0.53ms   7.8% ██                   ok=True attempts=1
agent.reflect              0.01ms   0.1%                      steps_taken=2 total_steps=3 failed_steps=0 budget=8
agent.act                  0.02ms   0.3%                      step_id=s3 tool=calculator requires_approval=False outcome=ready
tool.calculator            0.21ms   3.1% █                    ok=True attempts=1
agent.reflect              0.01ms   0.1%                      steps_taken=3 total_steps=3 failed_steps=0 budget=8
agent.answer               0.01ms   0.1%                      successes=3 failures=0 halt_reason=completed

11 spans, 6.78ms wall clock

Run 2 — write action: pauses for approval, then resumes

agent.plan                 0.02ms   0.3%                      planner=heuristic steps=3 tools=web_search,query_tickets,save_report
agent.act                  0.01ms   0.2%                      step_id=s1 tool=web_search requires_approval=False outcome=ready
tool.web_search            0.48ms   7.7% ██                   ok=True attempts=1
agent.reflect              0.01ms   0.2%                      steps_taken=1 total_steps=3 failed_steps=0 budget=8
agent.act                  0.08ms   1.3%                      step_id=s2 tool=query_tickets requires_approval=False outcome=ready
tool.query_tickets         0.20ms   3.2% █                    ok=True attempts=1
agent.reflect              0.01ms   0.2%                      steps_taken=2 total_steps=3 failed_steps=0 budget=8
agent.act                  0.02ms   0.3%                      step_id=s3 tool=save_report requires_approval=True outcome=awaiting_approval
agent.act                  0.02ms   0.3%                      step_id=s3 tool=save_report requires_approval=True outcome=awaiting_approval
agent.approval             0.01ms   0.2%                      step_id=s3 tool=save_report approved=True
tool.save_report           0.50ms   8.1% ██                   ok=True attempts=1
agent.reflect              0.01ms   0.2%                      steps_taken=3 total_steps=3 failed_steps=0 budget=8
agent.answer               0.01ms   0.2%                      successes=3 failures=0 halt_reason=completed

13 spans, 6.21ms wall clock
```

**Three things worth reading off that output:**

- **The budget is visible on every hop.** `agent.reflect` carries
  `steps_taken=2 total_steps=3 budget=8`, so a run that halted early tells you *why* from
  the trace rather than from a log line you have to go find.
- **Retries are visible after the fact.** `tool.* attempts=1` is the only place a silent
  retry shows up once the request is over. An `attempts=3` on a tool that still returned
  `ok=True` is the signature of a flaky dependency that no error log would catch.
- **The approval pause appears as two `agent.act` spans, both `awaiting_approval`.** That
  is not a bug in the instrumentation — it is the LangGraph semantics being honest.
  `interrupt()` unwinds the stack, so the node genuinely re-executes from the top on
  resume, and the trace shows two executions because there were two. It is the same
  property that forces everything before `interrupt()` to be idempotent.

### On LangSmith

`TRACE_EXPORTER=otlp` pointed at LangSmith's OTLP endpoint with an `Authorization` header
works, and the config is in `.env.example`. **That path is implemented and has never been
executed** — no LangSmith key was used in this repo. This makes *OpenTelemetry, tracing and
observability* defensible; it does **not** make *LangSmith* defensible, and that should not
go on a CV off the back of this repo.

---

## Tests

```
make test        # 94 python tests
make test-dart   #  8 dart wire-compatibility tests
```

What they actually cover, rather than a coverage number:

- **`test_events.py`** — every type tag, enum spelling and snake_case key asserted
  against the shapes read out of `agent_kit`'s source. Including that control
  frames never look like agent events, because a fifth `type` throws in Dart.
- **`test_tools.py`** — the sandboxes: `eval`-style injection, SQL writes,
  statement smuggling, `ATTACH DATABASE`, DoS exponents, path traversal.
- **`test_guards.py`** — transient retried, permanent *not* retried (asserted on
  the call count), timeout honoured, `run_tool` never raises, and four 0.3s tools
  finishing in under 1s to prove they do not serialise on the event loop.
- **`test_planner.py`** — intent routing, the late-binding resolvers, and that a
  hallucinated tool or a schema violation is rejected at planning time.
- **`test_graph.py`** — plan emitted before the first tool call, approval pauses
  *before* executing, rejection keeps prior work, the step budget halts a long
  plan and still answers, a failing step does not abort the run.
- **`test_api.py`** — SSE framing, the two-request approval flow, double-approve
  is a 409.

---

## API

```bash
POST /runs                    {"question": "...", "run_id": "optional"}  → SSE
POST /runs/{id}/approve       {"approved": true, "note": "..."}          → SSE
GET  /runs/{id}               plan, answer, halt_reason, awaiting_approval
GET  /tools                   names, schemas, requires_approval
GET  /health
```

Two SSE event names, on purpose:

```
event: agent     → exactly what AgentEvent.fromJson accepts (4 types)
event: control   → run_started | approval_required | done | error
```

Control frames carry `kind`, not `type`, and ride a different event name,
because the Dart union has four members and a fifth would throw. A client that
ignores unknown SSE event names keeps working either way.

---

## Honest status

A working reference implementation with a tested protocol contract. Specifically
**not** a production deployment:

- **`docker compose up` is written but has not been run** — no Docker on the
  build machine. The Dockerfile is two-stage and runs as a non-root user; it is
  unverified. Everything else below was executed: 94 Python tests, 8 Dart tests,
  the MCP server listing and calling tools, and the full approval flow over real
  HTTP against `uvicorn`.
- **No LLM was used in any verified run.** `PLANNER=llm` is implemented and
  validated against the tool registry, but every number and every test here comes
  from the heuristic planner. The planner is rule-based, not clever — it
  recognises five intents and falls back to search-only.
- **`web_search` searches a bundled corpus by default, not the internet.** The
  Tavily provider is implemented and untested against the live API.
- **The final answer is extractive, not generated.** `answer()` concatenates tool
  results and citations rather than calling an LLM, because this node's job in a
  reference implementation is to prove the results survived the graph intact.
  Swapping in a generation call does not touch the event protocol.
- **`MemorySaver` keeps runs in process memory.** So an approval must reach the
  same worker, a restart loses paused runs, and this scales to exactly one
  replica. The Postgres checkpointer is the fix and is a config change, not a
  redesign.
- **No auth, no rate limiting, no multi-tenancy, no per-user tool permissions.**
- **Tracing has only been exercised with the file exporter.** The OTLP path (and therefore
  the LangSmith path) is implemented and unrun — no collector and no key were used.
- **No screen recording of the Flutter UI.** The wire contract is tested against
  the real package, which is the part that can actually break; a GIF of widgets
  rendering is not evidence of anything the tests do not already cover.

## Next

- Postgres checkpointer, so paused runs survive a restart and more than one replica
- Token-level answer streaming (the protocol already has `thinking.is_streaming`)
- Per-run tool permissions, so approval is policy rather than a hardcoded set
- A tool-level eval set: given a question, did it pick the right tool and arguments

## License

BSD-3-Clause.
