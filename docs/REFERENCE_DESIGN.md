# Reference Design Decisions

base-agent uses mock-manus and AgentDemo as reference implementations. They are consumers and
design inputs, not dependencies of the core package. No core module may import them or expose a
type named after either application.

## Dependency direction

```text
mock-manus ─┐
AgentDemo ──┼──> base-agent
BI-WIKI ────┘
```

Applications select Providers, Tools, Skills, stores, orchestration strategies, and infrastructure
adapters. The core supplies stable contracts and lifecycle behavior.

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

## Extraction sequence

1. `RunHandle` and replayable live event subscriptions;
2. complete wait/resume input semantics (implemented);
3. generic orchestration strategy and step/plan models (implemented);
4. task-scoped resource lifecycle and Tool context injection (implemented);
5. artifact and attachment ports with Tool context access (implemented);
6. optional memory retrieval port with explicit failure policy (implemented);
7. transport and infrastructure adapters outside the core loop (FastAPI, PostgreSQL, Redis, MCP,
   Docker Sandbox, and Playwright Browser adapters implemented).

Each extracted capability must work with in-memory fakes and without importing either reference
application.
