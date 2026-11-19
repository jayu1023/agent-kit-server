# Interview Guide — agent-kit-server

How to present this project and survive the follow-ups. Written for an AI Engineer loop.

**The one thing to lead with:** most candidate agent projects are a `while` loop
around a tool-calling API. The differentiator here is not the LangGraph graph —
it is that **the protocol contract is tested from the client side, and that test
found two bugs the server's own tests could not see.** That is a story about
engineering judgement, not about a framework.

**Where it pairs with P0.** `rag-service` is the retrieval answer; this is the
agent answer. If a job description leads with RAG, open with that one. If it
leads with agents, tools or MCP, open with this.

---

## 1. The 60-second version

> "It's the backend for a Flutter package I publish. `agent_kit` ships the
> widgets for agent UIs — tool-call cards, plan timelines, citation chips — and
> it's backend-agnostic, so every adopter had to write the agent loop themselves.
> This is that loop.
>
> It's a LangGraph graph with four explicit nodes — plan, act, observe, reflect —
> four real tools, and a human approval gate that pauses the run before any write
> action. The SSE stream decodes directly into the package's `AgentEvent` type,
> and the same tools are exposed over MCP so an MCP client can call them too.
>
> The part I'd actually want to talk about is how I tested the protocol. The
> Python server emits JSON that a Dart package has to parse — and a Python test
> can only assert that Python produces what Python expects. So I capture the
> frames from a real agent run and decode them in Dart, against the real
> published package. That test found two bugs my Python suite was blind to."

Stop there. The last line is bait and a good interviewer always takes it.

### The two bugs — have these precise

**1. `observe()` updated the plan in state but never emitted it.**
The checkpoint was correct and the client was wrong: the last plan step stayed
`running` forever in the Flutter timeline. Invisible to Python tests, because the
*state* was right — only a client decoding the actual event stream could see it.

**2. Tool-call ids were only unique within a run.**
`agent_kit` keys its widgets on `ToolCall.id`. Every run starts at step `s1`, so
a client rendering two runs in one view would collide their cards into one. Found
because the fixture concatenates two runs — ids now include the run id.

Both are now regression-tested on both sides. Volunteering bugs you found in your
own code reads as confidence and makes the rest of your claims credible.

---

## 2. The demo, in order

Five minutes, no slides:

1. `make test` — 94 Python tests.
2. `make test-dart` — regenerates the fixture from a real agent run, then decodes
   it with the real `agent_kit` package. **This is the money shot.** Say out loud
   that the fixture is generated, not written.
3. `make dev`, then `make ask Q="How does average first response time compare across plans?"` —
   watch the plan render first, then the tool cards advance, then the calculator
   step use a number the SQL step produced.
4. `make ask Q="What is our CSAT by plan and save a report about it"` — the stream
   **ends** at `approval_required`. Point out that the connection closed and the
   run is still alive.
5. `curl :8000/runs/demo` — show the paused run readable over HTTP.
6. `make approve OK=false` — rejection keeps the earlier work and says why.

Then, if there is time: `claude mcp add` and call `query_tickets` from inside
Claude Code. Same tool object, no duplicated definition.

---

## 3. The questions you will get

Direct answer, then the reason, then the limit. Never the reverse.

### "Why LangGraph? Couldn't this be a while loop?"

> "It could, and for a lot of agents it should — a tool-calling loop is about 40
> lines and I'd start there.
>
> Three things made the graph worth it here. First, durable state: the state is
> checkpointed between nodes, so a run can stop mid-flight and resume later,
> possibly in a different process. That's what makes the human approval gate work
> at all — a while loop's state is local variables, and they die with the request.
>
> Second, interrupts as a first-class primitive. `interrupt()` suspends the run
> and hands the decision out of the process. Hand-rolling that means inventing
> your own serialisation of 'where was I'.
>
> Third, the edges are data. 'Where can execution go from here' is declared, not
> buried in if/break, so a reviewer can see there's exactly one path to the answer
> node.
>
> The cost is a framework and its own surprises — I hit a real one, which I'm
> happy to get into."

### "What surprised you about LangGraph?"

> "After an interrupt, the node re-runs from the top on resume. Everything above
> `interrupt()` executes twice.
>
> It bit visibly. `act()` emitted a thinking bubble *and* a pending tool-call card
> before interrupting, and the client showed the thinking bubble twice — because
> `ToolCall` has a stable id the widget dedupes on and `ThinkingStep` doesn't.
>
> The fix is a rule, not a patch: everything before `interrupt()` has to be
> idempotent. `act()` now emits exactly one thing there, a tool call with a stable
> id, which renders as the same card both times. It's a comment in the code,
> because the next person will hit it too."

### "Why four nodes instead of one ReAct node?"

> "Three reasons, and the UI is the first one. Each phase emits a different event
> to the Flutter client — plan tree, pending tool call, terminal tool call,
> thinking bubble. Collapsing them gives you one function that knows about every
> widget.
>
> Second, the interrupt needs a boundary where the state is complete and nothing
> has executed. `act` is exactly that: tool and arguments decided, no side
> effects. Interrupting mid-ReAct means checkpointing a half-made decision, and
> re-running it on resume isn't safe.
>
> Third, the loop guard needs its own place. A step limit checked inside the node
> that also does the work is a limit you can forget on one of the paths out.
> `reflect` is the only node that can set `halt_reason`, so there's one place
> where 'why did it stop' is decided."

### "Where do you put a guardrail — prompt, parser, or a separate pass?"

The highest-signal question in an agent interview. Answer in layers.

> "Four places, increasing in cost, and you pick per failure mode.
>
> The prompt is free and weakest — it shifts the distribution, it doesn't bound
> it. Worth doing, never worth trusting.
>
> The schema and parser layer is where anything with a checkable shape belongs,
> and it's where my tool allowlist lives: the planner's output is validated
> against the real tool registry *before* execution, so a hallucinated tool name
> or a schema violation discards the whole plan at the planning boundary. That's
> the difference between 'the model can call tools' and 'the model can call these
> tools with these arguments', and it costs a dict lookup.
>
> A sandbox is for when the action itself is dangerous — my calculator walks an
> AST instead of calling `eval`, and the SQL tool installs a SQLite authorizer
> callback so the database refuses writes rather than a regex trying to.
>
> A human is for irreversible actions where being wrong costs more than waiting.
> That's the approval interrupt, and it guards exactly one tool.
>
> The failure is putting everything at layer one, which is hoping, or everything
> at layer four, which is unusable."

### "Walk me through the tool sandboxes."

> "The calculator doesn't use `eval`, because `__import__('os').system(...)` is a
> valid Python expression and over enough requests a model emits something in that
> family. It parses to an AST and walks it, rejecting every node type that isn't a
> number, an arithmetic operator or a parenthesis. So a call, a name and an
> attribute access aren't filtered, they're unreachable. `2 ** 10 ** 9` is a
> separate check — that's a denial of service, not an injection, and a string
> blocklist would miss it.
>
> The SQL tool installs `set_authorizer` and permits only SELECT and READ
> actions. The database refuses writes itself, which beats a regex that loses to
> `/*comment*/DELETE`. It's installed *after* the CSV loads, so the loader can
> write and nothing afterwards can. Then a single-statement check for
> `SELECT 1; DROP TABLE`, and a forced LIMIT because an unbounded SELECT is a
> memory incident waiting for a bigger table.
>
> And the write tool treats the model-chosen filename as hostile —
> `title="../../.ssh/authorized_keys"` goes through a character allowlist and then
> an `is_relative_to` check. Two independent mechanisms, because slugification is
> easy to weaken by accident."

### "Why SQL as a tool interface rather than a filter API?"

> "Three reasons. Models write SQL much better than they write any bespoke query
> language — there's vastly more SQL in the training data. It's self-documenting
> in the tool-call card the client renders, so a reviewer sees exactly what was
> asked. And the sandbox becomes the database's job instead of a parser I
> maintain.
>
> The cost is that you have to actually sandbox it, and a lot of people ship a
> regex and call it done."

### "How do you handle a tool that fails?"

> "Depends on *how* it failed, and that distinction is the thing I'd argue for.
>
> A validation error is permanent. Retrying `SELECT * FROM ticekts` three times
> gives you the same typo three times, spends the step budget, and buries the
> real problem. So ValueError, TypeError and ValidationError fail immediately —
> but with a legible message, because that message is what the model reads to
> correct itself instead of looping.
>
> Timeouts and transport errors are transient and get exponential backoff.
>
> Most retry code retries everything, which is how one bad argument becomes four
> bad arguments and a blown budget.
>
> And `run_tool` never raises — it returns an outcome object. Every node
> downstream depends on that: if it raised, one bad tool would abort the whole
> graph instead of failing one step. A failing step marks that step failed and the
> answer names what didn't work."

### "How does the human-in-the-loop actually work? Why not just hold the stream open?"

> "The graph calls `interrupt()` in `act`, before anything executes. The
> checkpointer persists the state and the HTTP stream ends with a control frame
> saying what's being asked. Approval is a *separate* POST that resumes the run
> with `Command(resume=...)`.
>
> Separate, because the run has to outlive the connection. A reviewer might take
> an hour; the client might be a phone that backgrounds; with a durable
> checkpointer the process itself can restart. Holding the decision on the
> original socket makes a human decision depend on a TCP connection staying up,
> which is a strange thing to bet a side effect on.
>
> And a rejection isn't a failed run — the write is marked cancelled, every
> read-only result already gathered is kept, and the answer says a write was
> rejected and why. There's a test asserting no file lands on disk and that the
> earlier query result survives."

### "How do you stop the agent looping forever?"

> "A step budget in `reflect`, the one node that can end the loop. When it trips
> the agent still answers from what it gathered, and the answer says it stopped
> early — halting with nothing is the wrong failure, because the user asked a
> question and two of three steps' findings beats an error.
>
> It's tested by setting `MAX_STEPS=1` against a three-step plan and asserting
> `halt_reason == 'max_steps'`, one step taken, and a non-empty answer."

### "What does your MCP support actually mean?"

Resist overclaiming here — interviewers probe MCP because most claims are thin.

> "It means the four tools are exposed over MCP stdio to any host — Claude Code,
> Claude Desktop, Cursor. The thing I'd point at is that the definitions aren't
> duplicated: the MCP server iterates the same tool list and takes each input
> schema from the same Pydantic model the graph uses. Add a tool to the registry
> and it appears over MCP with no edit to that file. That's the only version that
> doesn't rot.
>
> One precision: **MCP has no equivalent of my approval interrupt.** The write
> tool is advertised with `readOnlyHint: false`, and the *host* owns the
> permission prompt — Claude Code asks the user itself. The annotation is what
> lets it do that; it isn't a gate my process enforces. So 'HITL over MCP' would
> be false and I don't say it."

### "Why test the protocol in Dart? Isn't that overkill?"

> "It's the only test that can catch the actual failure. The Python server emits
> JSON that a Dart package parses, and a Python test can only assert Python
> produces what Python expects. `AgentEvent.fromJson` accepts exactly four type
> tags and throws on anything else, with snake_case keys and specific enum
> spellings — a typo there is a crash on someone's phone, and green Python tests.
>
> So I capture frames from a real agent run — read-only plus a write run that
> pauses and is approved, 39 frames — and decode them in Dart against the real
> published package. The assertions that matter: every frame decodes, each decodes
> to the subtype its tag claims, and `fromJson`→`toJson` round-trips losslessly.
> That last one is the subtle one: a field the server sends that the Dart model
> silently drops passes `fromJson` and fails the round-trip.
>
> Generating the fixture from the real server is the whole point. A hand-written
> fixture only proves the fixture matches the client.
>
> And it earned its keep immediately — two bugs, both invisible to the Python
> suite."

### "Your planner is rule-based. Isn't that cheating?"

Expected, and the honest answer is the strong one.

> "The default is rule-based, yes, and it's in the README in those words. It
> recognises five intents and falls back to search-only.
>
> It exists for a specific reason: a deterministic planner is what lets the test
> suite assert on an exact plan and lets anyone clone the repo and run the whole
> thing with no API key. Every number and every test in there comes from the
> heuristic planner.
>
> The LLM planner is implemented and it's where the interesting guardrail lives —
> structured output, then validation against the real registry before anything
> executes. But I haven't run it with a key, so I don't claim measurements from
> it. If you want to see tool selection quality, the honest answer is I'd need an
> eval set for it, and that's the next thing I'd build: questions mapped to the
> expected tool and arguments, scored against the LLM planner."

### "What's your eval story for an agent?"

> "Weaker than my RAG project's, and I'd say that upfront.
>
> What I have is a protocol contract test and 94 unit and integration tests
> covering the guards — those are correctness tests, not quality measurements.
>
> What an agent actually needs is three layers. **Tool selection**: given a
> question, did it choose the right tool with the right arguments? That's a
> labelled set and it's cheap to build. **Trajectory**: did it reach the answer
> in a reasonable number of steps, or wander? Step count and whether any step's
> result went unused. **Outcome**: is the final answer right? That needs either
> ground truth or an LLM judge.
>
> The order matters — tool selection is cheapest and catches the most. I'd build
> that first, and it's in the Next section."

### "How would you scale this?"

> "The blocker is specific: `MemorySaver` keeps runs in process memory, so an
> approval has to reach the same worker, a restart loses paused runs, and it
> scales to exactly one replica. The fix is the Postgres checkpointer — a config
> change, not a redesign, which is most of why I used a graph framework.
>
> After that: tool execution is already off the event loop in threads, but
> CPU-heavy tools would want a separate worker pool. And per-run tool permissions,
> so approval is policy keyed by user rather than a hardcoded set of one tool."

---

## 4. Questions where the honest answer is "I didn't"

Do not bluff these. One confident wrong answer devalues everything true you said.

| Question | Answer |
|---|---|
| "Did you run it in Docker?" | "No. The Dockerfile is two-stage and non-root, and compose is written, but I have no Docker on that machine and haven't run it — it's flagged unverified in the README. The 94 Python tests, the 8 Dart tests, the MCP server, and the full approval flow over real HTTP against uvicorn were all executed." |
| "Did you run the LLM planner?" | "No — no API key. It's implemented and the validation guardrail is tested with hand-built plans, but every result in the repo comes from the heuristic planner." |
| "Does `web_search` search the web?" | "Not by default. It searches a bundled six-document corpus, and that's in the README rather than implied away. The Tavily provider is implemented and untested against the live API. The default is deterministic on purpose — it's what makes the tests assert on real tool results." |
| "Is the final answer LLM-generated?" | "No, it's extractive — it concatenates tool results and citations. In a reference implementation this node's job is proving the results survived the graph intact. Swapping in a generation call doesn't touch the event protocol." |
| "Did you run the Flutter UI?" | "I ran Flutter *tests* against the real package, which is the part that can break. I didn't record the widgets rendering. A GIF isn't evidence of anything the wire tests don't already cover — though I'd grant it's better marketing." |
| "Multi-agent?" | "No. One graph, one agent, four tools. 'Multi-agent' usually means a supervisor routing to specialists, and I didn't need one here — adding it to look impressive would be the wrong instinct." |
| "Have you served real traffic?" | "No. No auth, no rate limiting, no multi-tenancy, no load test." |

**The framing that makes this land:** "here's what I measured, here's what I
didn't, and here's which I'd do first." Nobody expects a complete production
system from a side project. They are checking whether you know the difference
between what you verified and what you assumed.

---

## 5. If they ask you to extend it live

Say the approach out loud before typing.

**"Add a tool."**
`app/tools/` + one line in the registry. Point out it inherits the guards, shows
up in `GET /tools`, in the planner's prompt, and over MCP automatically — and
that if it writes, adding it to `WRITE_TOOLS` is the entire approval wiring.

**"Make the agent replan when a step fails."**
A conditional edge from `reflect` back to `plan`, with a replan counter in state
so it can't ping-pong. Feed the failure into the replan prompt. The guard matters
more than the feature: replanning without a budget is how you get an infinite
loop with extra steps.

**"Stream the answer token by token."**
`thinking.is_streaming` already exists in the protocol. Swap `answer()` for a
streaming LLM call and emit a `thinking` frame per delta — then a final one with
`is_streaming: false`. No protocol change, which is the point of having one.

**"Two agents, one supervisor."**
A supervisor node routing to subgraphs. Be honest that the hard part isn't the
topology, it's the state contract between them and who owns the step budget — a
shared budget means one agent can starve the other.

**"Make approval policy-driven."**
Move `WRITE_TOOLS` into a policy object keyed by user and tool, resolved in
`act`. The tell that it's the right shape: `act` keeps asking one question —
"does this call need approval?" — and only the answer's source changes.

---

## 6. Where this sits on your résumé

**Claims this repo makes defensible:** AI agents, agentic AI, LangGraph, tool
use / function calling, JSON-schema tool definitions, MCP, guardrails,
human-in-the-loop, SSE streaming, FastAPI, async Python, sandboxing untrusted
model output, cross-language contract testing.

**Claims it does not back.** Don't let it imply: multi-agent orchestration (one
agent), RAG or vector search (that's `rag-service`), fine-tuning / LoRA / PEFT
(unbuilt), LangSmith tracing (not wired), production agent operations.

**Three sentences for when someone opens your GitHub:**

1. "It's the missing backend for a Flutter package I publish — so the protocol
   isn't hypothetical, it's a contract with something on pub.dev."
2. "The SSE protocol is tested from the Dart side against the real package, and
   that test found two bugs my Python suite was structurally blind to."
3. "The guards are in the graph, not bolted on — and the README says which parts
   I never verified."

---

## 7. The mindset

Two things make this project read as senior, and neither is the framework.

**You tested across the boundary you don't control.** Most people test the side
they wrote. Capturing real output and decoding it with the real consumer is the
move, and the two bugs it found are the proof it wasn't theatre.

**You distinguished guards from error handling.** A guard stops a class of
failure by construction — an AST walker, an authorizer callback, a registry
check. Error handling cleans up after one. Being able to say which of your code
is which, and why each one lives where it does, is the signal.

Avoid:
- **Overselling the planner.** It's rule-based by default. Say it before they ask.
- **Overclaiming MCP.** Exposing tools is real; the host owns permissions. Say that.
- **Apologising for scope.** It's a protocol reference with a tested contract,
  built on purpose. "Just a side project" invites a discount; "here's the
  contract, here's what I didn't verify" does not.
