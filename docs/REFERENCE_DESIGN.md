# Reference Design Decisions

base-agent uses mock-manus, AgentDemo, and Hermes Agent as reference implementations. They are
consumers and design inputs, not dependencies of the core package. No core module may import them
or expose a type named after any of these applications.

The source-level adoption matrix, including gaps in the current SDK-facing API, is maintained in
[`REFERENCE_ADOPTION_ZH.md`](REFERENCE_ADOPTION_ZH.md).

## Dependency direction

```text
mock-manus ─┐
AgentDemo ──┼──> base-agent
Hermes ─────┤
BI-WIKI ────┘
```

Applications select Providers, Tools, Skills, stores, orchestration strategies, and infrastructure
adapters. The core supplies stable contracts and lifecycle behavior.

The copyable `starter/` is a downstream application example, not a nested runtime distribution. It
depends on base-agent public APIs in the same direction as any external application.

## Lessons adopted from mock-manus

- Separate background task control from Agent/Flow domain logic.
- Emit typed events during execution instead of returning only a final string.
- Persist events before transporting them, so replay does not depend on an open connection.
- Model waiting for human input explicitly and allow waiting Runs to resume or cancel.
- Keep Planner/ReAct orchestration separate from browser, sandbox, storage, and transport layers.
- Acquire and release task-scoped resources in the same asynchronous execution context.
- Treat attachments and generated files as artifacts, not opaque prompt text.
- Preserve Sandbox/Browser session continuity within a task while keeping their implementations
  behind protocols.

## Lessons adopted from AgentDemo

- Use a bounded step loop with explicit no-progress detection.
- Keep ReAct and Planning as replaceable execution strategies.
- Register Tools as a collection with normalized schema and result handling.
- Allow a Flow to coordinate multiple named Agents without making multi-agent behavior mandatory.
- Keep long-term memory optional and isolated behind a port.
- Let application Agents add domain prompts and Tools through composition.
- Expose browser state through bounded observations and clean up browser resources explicitly.

## Lessons adopted from Hermes Agent

- Keep a narrow core and prefer Skill/Toolkit or optional integrations over adding core Tools.
- Freeze the model-visible Prompt, Skill, Tool schema, model, and policy selection for one Run.
- Separate configured Tool membership, environment readiness, governance authorization, and
  execution.
- Disclose large Skill and Tool catalogs progressively and account for each context component
  independently.
- Keep read-only Observer hooks separate from behavior-changing Middleware, fail-closed Governance,
  and evidence-producing Verification.
- Distinguish context selection, compaction, per-result truncation, and aggregate observation
  budgets.
- Normalize provider responses behind a minimal transport contract while retaining
  provider-specific data outside the common surface.
- Record bounded, scoped verification evidence without treating a targeted check as proof that the
  entire application is correct.

## Patterns deliberately not copied

- Deep Agent inheritance trees;
- mutable Pydantic models as live runtime controllers;
- application-specific browser, search, shell, BI, or lineage event types in the core;
- direct database, queue, HTTP, or LLM construction inside a Flow;
- global task registries and infrastructure singletons;
- returning untyped error strings in place of lifecycle states and events.
- monolithic Sandbox APIs combining shell, sudo, files, browser, VNC, and container management;
- raw shell-string interpolation, unrestricted host paths, and process-global Browser state;
- disabling browser security or exposing arbitrary JavaScript execution by default.
- a very large Agent constructor combining provider, gateway, prompt, plugin, session, and runtime
  configuration;
- import-time global Tool registration, automatic override, and mid-Run capability mutation;
- product-level profile routing, messaging gateways, cron, TUI, or plugin marketplace behavior in
  the SDK core;
- Observer hooks that can silently mutate requests, block execution, or replace results;
- executing inline shell while loading a Skill or Prompt.

## Extraction sequence

1. `RunHandle` and replayable live event subscriptions;
2. complete wait/resume input semantics (implemented);
3. generic orchestration strategy and step/plan models (implemented);
4. task-scoped resource lifecycle and Tool context injection (implemented);
5. artifact and attachment ports with Tool context access (implemented);
6. optional memory retrieval port with explicit failure policy (implemented);
7. transport and infrastructure adapters outside the core loop (FastAPI, PostgreSQL, Redis, MCP,
   Docker Sandbox, and Playwright Browser adapters implemented).
8. versioned Flow definitions, explicit handoff models, the AgentInvoker boundary, and immutable
   FlowRunState/AgentInvocation lifecycle contracts (implemented);
9. revision-checked Flow lifecycle persistence with atomically ordered events and an in-memory
   repository (implemented);
10. a bounded SequentialFlowStrategy with explicit handoffs, WAITING/resume, failure and
    cancellation propagation (implemented);
11. a definition-pinned AgentRuntimeInvoker with correlated child Runtime records and optional
    application-composed shared stores (implemented);
12. versioned Flow-wide invocation, Token, model-call, Tool-call, and wall-clock budgets with
    structured limit events (implemented);
13. state-before-transport cancellation propagation with waiting-checkpoint cleanup, running
    cancellation signals, late-result protection, and failure events (implemented; durable
    distributed execution is not);
14. a whole-Flow deterministic test harness capturing result, state, events, handoffs, resumes,
    budgets, and cancellation evidence (implemented).

Each extracted capability must work with in-memory fakes and without importing either reference
application.
