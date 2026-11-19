# Explainer — agent-kit-server

A plain-language walkthrough of what an agent loop actually is, what each piece
of this one does, and why each decision went the way it did. No prior LangGraph
knowledge assumed.

---

## 1. What problem an agent solves

A plain LLM call maps text to text. It cannot look anything up, cannot do
arithmetic reliably, and cannot change the world. For a question like *"how does
our average first-response time compare across plans, and are we meeting the
Enterprise SLA?"* it will produce a fluent, confident, invented answer, because
every number it needs lives in a database it has never seen.

An **agent** is the loop that fixes that: give the model a set of tools with
described inputs, let it choose which to call, feed the results back, and repeat
until it can answer. The model supplies judgement about *what to do next*; the
tools supply facts and effects.

Which turns one hard problem into four:

| Problem | This repo's answer |
|---|---|
| The model picks a tool that does not exist, or valid-looking nonsense arguments | validate the plan against the real tool registry **before** anything runs |
| A tool hangs, or fails transiently, or fails permanently | one shared execution path with a timeout and a permanent/transient retry split |
| The loop never terminates | a step budget enforced in exactly one node |
| The model takes an action nobody sanctioned | the graph pauses for human approval before any write |

An agent without those four is a demo. The interesting engineering is entirely
in them.

---

## 2. Why a graph and not a `while` loop

The simplest agent is a loop: ask the model, call the tool it names, append the
result, repeat. It is ~40 lines and it is genuinely fine for a lot of cases.

A graph framework (LangGraph) buys three things that are hard to retrofit:

1. **Durable state.** The graph's state is checkpointed between nodes, so a run
   can stop in the middle, be persisted, and resume later — possibly in a
   different process. This is what makes human approval work at all. A `while`
   loop's state lives in local variables, which die when the request ends.
2. **Explicit edges.** "Where can execution go from here" is data, not control
   flow buried in `if`/`break`. You can draw it, and a reviewer can see that
   there is exactly one path to the answer node.
3. **Interrupts as a first-class thing.** `interrupt()` suspends the run and
   hands the decision out of the process. Hand-rolling that means inventing your
   own serialisation of "where was I".

The cost is a framework in the stack and its own set of surprises — §7 is about
one of them.

---

## 3. The four nodes

```
plan  →  act  →  observe  →  reflect  ─┬→  act  (more steps)
                                       └→  answer
```

### `plan`
Turns the question into a list of `PlanStep`s (what the UI shows) plus a parallel
list of `Action`s (which tool each step calls). Emits a `plan` event immediately,
so the Flutter timeline renders before any tool starts — otherwise the user
watches an empty panel while a tool runs.

### `act`
Resolves the current step's arguments and, if the tool writes, **stops**.
Nothing here executes a tool. That separation is the whole reason the interrupt
is safe: on resume the node re-runs from the top, and re-running `act` has no
side effects.

### `observe`
Executes the resolved call through `guards.run_tool` and records what happened.
Emits the terminal `tool_call` event, the plan transition, and any citations.

### `reflect`
Advances the cursor and decides whether to continue. **The only node that may
set `halt_reason`**, so there is exactly one place where "why did the agent stop"
is decided.

### Why four nodes and not one ReAct node

A single node that thinks, calls a tool and loops is less code and worse here:

- **Each phase emits a different UI event.** `plan` emits a plan tree, `act` a
  `pending` tool call, `observe` the terminal one, `reflect` a thinking bubble.
  Collapsing them means one function that knows about every widget.
- **The interrupt needs a boundary where state is complete.** `act` is that
  boundary: the tool and arguments are decided, nothing has executed.
  Interrupting mid-ReAct means checkpointing a half-made decision.
- **The loop guard needs its own place.** A step limit checked inside the same
  node that does the work is a limit you can forget on one of the paths out.

---

## 4. The state, and why it is boring on purpose

`AgentState` is a flat `TypedDict` of JSON-safe values. Two reasons:

**It has to survive a process boundary.** The checkpointer persists it across the
approval round-trip. Whatever is not in the TypedDict does not survive — "keep it
in a closure" works until the resume call lands on a different worker. This is
why `resolved_args` is a state field rather than a value passed from `act` to
`observe`: the interrupt happens *between* those two nodes.

**It goes straight to the client.** `plan`, `tool_calls` and `citations` are
stored as the exact wire dicts the Flutter app decodes. One representation, no
mapping layer, no chance of the stored and transmitted shapes diverging.

### Reducers

List fields are annotated with an `append` reducer. Without one, two nodes
writing the same list key overwrite each other and you silently lose the earlier
node's tool calls. With it, each node returns only what it added.

---

## 5. Tools: what makes one real

A tool is three things: a name, a described input schema, and a function. The
schema is not documentation — it is what the model is given to fill in, and what
rejects it when it fills it in wrong. Here every schema is a Pydantic model, and
the JSON Schema is *generated* from it. Nothing is hand-written, so a schema
cannot drift from a signature. `GET /tools`, the LLM planner and the MCP server
all read the same generated schema.

### The sandboxes

This is where tool design gets real. A tool argument is **untrusted input that
arrives with the authority of your own code**.

**`calculator` does not use `eval()`.** `__import__('os').system('rm -rf /')` is
a valid Python expression, and over enough requests a model will emit something
in that family. It parses to an AST and walks it, rejecting every node type that
is not a number, an arithmetic operator or a parenthesis — so a call, a name and
an attribute access are all unreachable, not merely filtered. `2 ** 10 ** 9` is
rejected separately: that one is a denial of service, not an injection, and a
blocklist of strings would miss it.

**`query_tickets` lets SQLite enforce read-only.** A regex over the SQL string is
the obvious approach and it loses to things like `/*comment*/DELETE`. Instead
`set_authorizer` installs a callback that permits only `SQLITE_SELECT`,
`SQLITE_READ` and `SQLITE_FUNCTION`; the database itself refuses a write. The
authorizer is installed *after* the CSV loads, so the loader can write and every
query afterwards cannot. Two more layers: a single-statement check (stops
`SELECT 1; DROP TABLE tickets`) and a forced `LIMIT` (an unbounded `SELECT` is a
memory incident waiting for a bigger table).

Error messages are deliberately legible — "query rejected: no such column:
ticekts" is what the model reads to correct itself on the retry instead of
looping.

**`save_report` treats the filename as hostile.** `title="../../.ssh/authorized_keys"`
is slugified through a character allowlist and then checked with `is_relative_to`
against the reports directory. Two independent mechanisms, because slugification
is easy to weaken by accident.

### Why SQL rather than a filter DSL

An LLM writes SQL far better than it writes any bespoke query language — there is
vastly more SQL in its training data. SQL is also self-documenting in the
tool-call card the Flutter client renders: a reviewer sees exactly what was asked.
And the sandbox becomes the database's job rather than a parser you maintain.

### `web_search` and being honest about it

The default provider searches a bundled six-document corpus, **not the
internet**. That is a trade, and the README says so rather than letting the tool
name imply otherwise. A deterministic provider is what lets the test suite assert
on actual tool *results* and lets the repo demo with no key and no network.
`SEARCH_PROVIDER=tavily` makes it a real web search; the tool name, schema and
result shape are unchanged, which is the point of a provider seam.

Ranking is IDF-weighted term overlap with length normalisation — keyword
matching, not vectors. For six documents an embedding model is ceremony.

---

## 6. Guards

### Timeout and the permanent/transient split

All tool execution goes through one function, `guards.run_tool`, so a new tool
inherits the guards by existing.

The distinction that matters: **a validation error is permanent.** Retrying
`SELECT * FROM ticekts` three times produces the same typo three times, spends
the step budget and buries the real problem. So `ValueError`, `TypeError`,
`KeyError` and `ValidationError` fail immediately with a legible message;
timeouts and transport errors get exponential backoff. Most retry code in the
wild retries everything, which is how a bad argument becomes four bad arguments.

`run_tool` **never raises**. Every node downstream assumes that — if it raised,
one bad tool would abort the whole graph instead of failing one step.

Tools run via `asyncio.to_thread`. They are blocking, and that matters twice:
one slow tool must not stall the SSE stream of every other in-flight run, and
`asyncio.wait_for` can only actually interrupt a coroutine. A test asserts four
0.3-second tools finish in under a second, which fails if they ever serialise.

### The step budget

`MAX_STEPS` is checked in `reflect()`, the single place the loop can end. When it
trips, the agent **still answers from what it gathered** and the answer says it
stopped early. Halting with nothing is the wrong failure: the user asked a
question, and two of three steps' worth of findings beats an error.

### Validation as a guardrail, not error handling

The LLM planner's output is checked against the real registry *before* execution:
an unknown tool name or a schema violation discards the whole plan at the
planning boundary. This is the difference between "the model can call tools" and
"the model can call *these* tools with *these* arguments". It is also the cheapest
guardrail in the system — a dictionary lookup and a `model_validate`.

### Where a guardrail belongs, generally

Four places, increasing in cost:

1. **The prompt** — free, weakest. It shifts the distribution; it does not bound it.
2. **The schema / parser** — deterministic and cheap. Anything with a checkable
   shape belongs here. The tool allowlist lives here.
3. **A sandbox** — when the action itself is dangerous. The AST walker and the
   SQLite authorizer are this.
4. **A human** — when the action is irreversible and the cost of being wrong
   exceeds the cost of waiting. That is the approval interrupt, and it is
   reserved for exactly one tool.

A system that puts everything at level 1 is hoping. One that puts everything at
level 4 is unusable.

---

## 7. Human-in-the-loop

`save_report` is in `WRITE_TOOLS`, and the graph keys on that set — so "requires
approval" is a property of the *tool*, not something each call site remembers.

When `act()` reaches a write tool it calls `interrupt()`. Execution stops, the
checkpointer persists the state, and the HTTP stream ends with a `control` frame
saying what is being asked for. Approval arrives as a **separate request** that
resumes the run with `Command(resume={"approved": …})`.

### Why approval is a second request

Because the run has to outlive the connection. A reviewer might take an hour; the
client might be a phone that backgrounds; with a durable checkpointer the process
itself can restart. Holding the decision on the original socket makes a human
decision depend on a TCP connection staying up, which is a strange thing to bet
a side effect on.

### The gotcha: nodes re-run after an interrupt

**On resume, LangGraph re-executes the interrupted node from the top.**
Everything above `interrupt()` runs twice.

This bit, visibly. `act()` originally emitted a thinking bubble *and* a pending
tool-call card before interrupting, and the Flutter client showed the thinking
bubble twice — because `ThinkingStep` has no id to dedupe on, while `ToolCall`
does. The fix is a rule, not a patch: **everything before `interrupt()` must be
idempotent.** `act()` now emits exactly one thing there, a `tool_call` with a
stable id, which renders as the same card both times.

### Rejection is not failure

A rejected write marks the call `cancelled`, keeps every read-only result already
gathered, and produces an answer that says a write was rejected and why. A test
asserts no file appears on disk and that the earlier query result survives.
Throwing away the whole run because the last step was declined would punish the
user for the reviewer's decision.

---

## 8. The wire protocol

`agent_kit` decodes a tagged map:

```json
{"type": "tool_call", "data": {"id": "...", "name": "...", "state": "running", ...}}
```

`AgentEvent.fromJson` accepts exactly four tags and throws `FormatException` on
anything else. Fields are snake_case (`duration_ms`, `is_streaming`,
`source_id`), enum values are the Dart enum *names* (`pending`, `running`,
`success`, `failed`, `cancelled`), and `Citation.url` is **non-nullable** — a
local file still needs a real `file://` url, not an empty string.

All of that lives in `app/events.py` and nowhere else, so a breaking change in
the package is a one-file change here rather than a hunt through the nodes.

### Two SSE channels

`event: agent` frames are exactly what the Dart union accepts. Everything else —
`run_started`, `approval_required`, `done`, `error` — rides `event: control` and
carries `kind` rather than `type`. A fifth `type` in the agent stream would
throw in the client; a client that ignores unknown SSE event names keeps working.

### Stable ids are load-bearing

`agent_kit` keys its widgets on `ToolCall.id`. The same id across
`pending → running → success` makes one card advance; three different ids make
three cards. The id also includes the run id, because `step_id` alone is unique
only *within* a run — two runs both start at `s1`, and a client showing both
would collide their cards.

---

## 9. Testing a protocol you do not own

The failure mode here is specific: the Python server emits a shape the Dart
client cannot parse, and nothing in the Python test suite can see it. Python
tests assert that Python produces what Python expects.

So the contract is tested **from the Dart side, against the real published
package**:

1. `scripts/capture_events.py` runs the real agent — a read-only run, plus a
   write run that pauses and is approved — and dumps all 39 emitted frames to a
   JSONL fixture.
2. `clients/flutter_smoke` decodes every frame with the real `agent_kit` models
   and asserts: every frame decodes; each decodes to the subtype its tag claims;
   **`fromJson` → `toJson` round-trips losslessly**; every tool-call id reaches a
   terminal state and never moves backwards out of one; every plan step
   terminates; every citation has a parseable url.
3. CI runs both, so the sides cannot drift silently.

The round-trip assertion is the subtle one: a field the server sends that the
Dart model silently drops passes `fromJson` and fails the round-trip.

Generating the fixture from the real server is what makes this prove anything. A
hand-written fixture only proves the fixture matches the client.

**It found two real bugs.** Both are now regression-tested on both sides:

- **`observe()` updated the plan in state but never emitted it.** The checkpoint
  was correct and the client was wrong — the last plan step stayed `running`
  forever in the timeline. A pure-Python test would not have noticed, because the
  state was right.
- **Tool-call ids collided across runs**, as described above.

That is the argument for this kind of test in one line: both bugs were invisible
to the server's own tests and visible to the client's.

---

## 10. MCP in one paragraph

MCP is a protocol for exposing tools to a host application (Claude Code, Claude
Desktop, Cursor) over JSON-RPC. `mcp_server.py` iterates the same `ALL_TOOLS`
list and registers each with the input schema from the same Pydantic model — add
a tool to the registry and it appears over MCP with no edit there. Transport is
stdio, which means **stdout is the protocol channel**: anything printed that is
not JSON-RPC corrupts the session, so logs go to stderr. That is the single most
common way to break an MCP server.

`save_report` is advertised `readOnlyHint: false`. MCP has no equivalent of the
graph's approval interrupt — the *host* owns that decision and prompts the user
itself. The annotation is what lets it; it is not a gate this process enforces.
Worth being precise about, because "we have HITL over MCP" would be false.

---

## 11. What this deliberately does not do

| Missing | Why | What adding it looks like |
|---|---|---|
| Durable checkpointer | `MemorySaver` is enough to demonstrate the interrupt | `PostgresSaver`; a config change, not a redesign |
| Generated final answer | this node's job is proving results survived the graph | one LLM call in `answer()`; the protocol does not change |
| Token-level streaming | the extractive answer has no tokens to stream | `thinking.is_streaming` already exists in the protocol |
| Auth, rate limiting, multi-tenancy | out of scope for a protocol reference | API key middleware; per-tenant budgets |
| Per-user tool permissions | approval is a hardcoded set, not policy | move `WRITE_TOOLS` into a policy object keyed by user |
| A tool-selection eval set | the heuristic planner is deterministic, so there is nothing to measure yet | questions → expected (tool, args); score the LLM planner against it |
| Verified `docker compose up` | no Docker on the build machine | run it; it is written and unverified, and the README says so |

The last row is the pattern. A reviewer who finds an unstated gap stops trusting
the stated claims too.

---

## 12. Reading the code in 15 minutes

Follow the data:

1. `app/events.py` — the protocol. Read it first; everything else serves it.
2. `app/tools/calculator.py` — the smallest complete tool, and the AST sandbox.
3. `app/tools/csv_query.py` — the authorizer callback, which is the idea worth stealing.
4. `app/guards.py` — ~60 lines, and the permanent/transient split.
5. `app/planner.py` — `heuristic_plan()`, then `_validate()` (the guardrail), then the resolvers.
6. `app/graph.py` — the four nodes in order. `act()` is where the interrupt is.
7. `clients/flutter_smoke/test/wire_compat_test.dart` — what the contract actually is.

Then run `make test-dart` and read the fixture. 39 frames is the entire protocol,
in the order a real run emits them.
