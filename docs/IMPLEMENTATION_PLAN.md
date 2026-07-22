# Implementation Plan

## Working method

Development proceeds through small vertical milestones. Every milestone must leave the package
installable, tested, linted, typed, and buildable. mock-manus, AgentDemo, PentAGI, BI-WIKI, and
build-lineage are design references or downstream consumers; their code is not copied into the
core.

## Milestone 0 — Project baseline

Status: completed on 2026-07-21

Deliverables:

- `src` package layout;
- build configuration;
- pytest, Ruff, and mypy configuration;
- architecture and implementation documents;
- minimal package import test;
- clean build verification.

Acceptance:

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy src
uv build
```

All commands pass. A license decision is recorded before public distribution.

Verification result:

- pytest: 1 passed;
- Ruff: all checks passed;
- mypy: no issues found;
- build: source distribution and wheel built successfully.

## Milestone 1 — Model contracts

Status: completed on 2026-07-21

Deliverables:

- `Message`, `ToolCall`, `ModelRequest`, `ModelResponse`, and `AgentResult` models;
- asynchronous `ModelProvider` protocol;
- deterministic `FakeModel` with scripted responses;
- serialization and contract tests.

Acceptance:

- no real provider or network is required;
- the fake model can return text or one or more tool calls;
- invalid message and tool-call shapes fail at the boundary;
- public models round-trip through their serialized form.

Verification result:

- 13 tests passed with warnings treated as errors;
- Ruff passed;
- mypy passed for 9 source files;
- source distribution and wheel built successfully.

## Milestone 2 — Minimal agent runtime

Status: completed on 2026-07-21

Deliverables:

- `AgentProfile`, `Agent`, `RuntimeContext`, and `AgentRuntime`;
- explicit run state transitions;
- maximum-step enforcement;
- text-only execution path.

Acceptance:

- `await agent.run("hello")` completes against `FakeModel`;
- invalid transitions are rejected;
- a maximum-step breach produces a typed terminal result;
- repeated runs do not leak state into one another.

Verification result:

- 23 tests passed with warnings treated as errors;
- Ruff passed;
- mypy passed for 15 source files;
- source distribution and wheel built successfully.

## Milestone 3 — Tool runtime

Status: completed on 2026-07-21

Deliverables:

- typed `Tool` contract and `ToolResult`;
- `@tool` decorator with schema generation;
- `ToolRegistry` and `ToolExecutor`;
- validation, timeout, permission, and error handling;
- tool-result feedback into the model loop.

Acceptance:

- valid tool calls execute and return typed results;
- malformed arguments and unknown tools are rejected before execution;
- tool exceptions and timeouts do not crash the runtime;
- all tool calls from one model response are handled deterministically.

Verification result:

- 31 tests passed with warnings treated as errors;
- Ruff passed;
- mypy passed for 21 source files;
- source distribution and wheel built successfully.

## Milestone 4 — Runs and events

Status: completed on 2026-07-21

Deliverables:

- `Run`, `RunStatus`, and immutable runtime event models;
- `RunStore` and `EventSink` protocols;
- in-memory implementations;
- cancellation checks and event replay.

Initial event vocabulary:

```text
run.created
run.started
model.requested
model.responded
tool.requested
tool.started
tool.completed
tool.failed
run.completed
run.failed
run.cancelled
```

Acceptance:

- a run can be inspected and replayed from ordered events;
- terminal state agrees with the terminal event;
- cancellation prevents subsequent model and tool execution;
- no process-global task registry is required.

Verification result:

- 36 tests passed with warnings treated as errors;
- Ruff passed;
- mypy passed for 27 source files;
- source distribution and wheel built successfully;
- cancellation was verified during a multi-tool response without starting the next tool or model
  step.

## Milestone 5 — Skill runtime

Status: completed on 2026-07-21

Deliverables:

- `SKILL.md` front-matter manifest;
- `SkillLoader` and `SkillRegistry`;
- explicit skill selection;
- required-tool and allowed-tool validation;
- selected skill versions recorded in run state.

Acceptance:

- missing skills and missing required tools fail before model execution;
- a skill cannot invoke a tool outside its allowlist;
- full instructions load only after a skill is selected;
- one example skill runs against fake components.

Automatic semantic skill selection is deferred until explicit selection is stable.

Verification result:

- 47 tests passed with warnings treated as errors;
- Ruff passed;
- mypy passed for 33 source files;
- source distribution and wheel built successfully;
- selected Skill allowlists were verified against hallucinated tool calls;
- Skill versions were verified in both Run snapshots and event history.

## Milestone 6 — Supervision

Status: completed on 2026-07-21

Deliverables:

- supervisor protocol;
- execution budget policy;
- duplicate tool-call detector;
- no-progress intervention hook;
- supervisor decision events.

Acceptance:

- repeated identical tool calls trigger the configured policy;
- model steps and tool calls have independent limits;
- supervisor policies do not depend on a concrete provider or tool;
- interventions are visible in the run event history.

Verification result:

- 51 tests passed with warnings treated as errors;
- Ruff passed;
- mypy passed for 38 source files;
- source distribution and wheel built successfully;
- independent model-step and tool-call budgets were verified;
- duplicate calls were redirected without re-execution;
- consecutive failures produced a recoverable no-progress intervention;
- ToolCall/ToolResult message adjacency remains valid during redirect.

## Milestone 7 — Developer experience

Status: completed on 2026-07-21

Deliverables:

- hello, tool, and skill examples;
- tool and skill test harnesses;
- getting-started, extension, and troubleshooting guides;
- package build and installation smoke test.

Acceptance:

- a new user can run the first example in ten minutes;
- tests and examples work without a real API key;
- a custom tool requires no runtime subclass;
- a custom agent is primarily profile, skill, and tool configuration.

Verification result:

- 56 tests passed with warnings treated as errors;
- hello, Tool, and Skill examples executed in isolated subprocesses;
- ToolHarness and SkillHarness use production validation paths;
- Ruff passed;
- mypy passed for 40 source files;
- source distribution and wheel built successfully;
- the wheel installed in an isolated environment and exposed the documented public APIs.

## Milestone 8 — Reference extraction and optional adapters

Status: in progress

Reference applications now inform generic capabilities; optional infrastructure follows after the
core API is proven:

1. OpenAI-compatible provider;
2. generic runtime lifecycle capabilities distilled from reference applications;
3. FastAPI run server;
4. PostgreSQL run store;
5. Redis event publisher;
6. MCP tool adapter;
7. sandbox and browser adapters;
8. build-lineage tool adapter and BI-WIKI domain skills.

Reference projects never become core dependencies. Each infrastructure adapter is optional and may
not introduce its dependency into the base installation.

Progress:

- [x] OpenAI-compatible Chat Completions Provider;
- [x] generic RunHandle and replayable live event subscription;
- [x] generic waiting state with resume/cancel transitions;
- [x] resume input API, atomic checkpoint claim, and context restoration;
- [x] generic orchestration strategy plus immutable plan/step models and lifecycle events;
- [x] execution-scoped resource lifecycle with Tool context injection and resume semantics;
- [x] Attachment/Artifact references, binary store port, Tool access, and checkpoint semantics;
- [x] optional structured Memory retrieval, failure policy, Tool access, and resume semantics;
- [x] optional FastAPI Run server with HTTP, SSE, resume, cancellation, and Artifact APIs;
- [x] PostgreSQL Run/Event/Checkpoint/Artifact store and polling EventStream;
- [x] Redis event publisher with durable cursor replay and polling fallback;
- [x] MCP Tool discovery/invocation adapter with stdio and Streamable HTTP transports;
- [ ] sandbox and browser adapters;
- [ ] build-lineage tool adapter and BI-WIKI domain skills.

Current verification baseline:

- 126 tests passed with warnings treated as errors, including live PostgreSQL and Redis integration
  tests when their test URLs are configured;
- Ruff passed;
- mypy passed for 78 source files;
- source distribution and wheel built successfully;
- the base wheel imports without the OpenAI SDK installed;
- the wheel's `openai` extra installs the SDK and exposes `OpenAIChatProvider`;
- Provider behavior is tested with an in-memory fake client and does not require a live API key;
- the generic `WAITING` Run/Result state and its resume/cancel transitions are covered by tests;
- background Runs, live subscription, cursor replay, isolation, shielded waiting, and cancellation
  are covered without a server or queue;
- Tool-driven suspension, serialized checkpoints, repeated waits, resume cursor continuation,
  concurrent resume rejection, and waiting cancellation are covered by deterministic tests;
- the default model/tool loop is a replaceable `OrchestrationStrategy`; custom strategies are
  covered without consuming a model response;
- immutable dependency-validated plans, step transitions, Run snapshots, lifecycle events, and
  checkpoint round-trips are covered by deterministic tests;
- lazy/eager resource acquisition, reverse same-task cleanup, partial failures, cleanup failures,
  task cancellation, WAITING/reacquisition, and resource-aware Tools are covered by tests;
- structured input Attachments, Tool-generated Artifacts, content isolation, access boundaries,
  lifecycle events, Run/Result snapshots, and WAITING checkpoint restoration are covered by tests;
- deterministic Memory retrieval, namespace/filter limits, structured Provider requests, event
  redaction, best-effort/required failures, Tool queries, and resume stability are covered by tests;
- FastAPI background start, Run lookup, cooperative cancellation, shielded resume, cursor-based SSE,
  history-only store rejection, Attachment references, and guarded Artifact download are covered;
- PostgreSQL protocol conformance, atomic event sequencing, Run cancellation, binary content,
  atomic Checkpoint claim, Agent waiting/resume, and cursor replay are covered against PostgreSQL;
- Redis cross-client Pub/Sub wake-up, durable-first publishing, connection failure fallback, and
  optional dependency boundaries are covered;
- MCP pagination, namespacing, permissions, JSON Schema validation, result/error normalization,
  private metadata filtering, and a real stdio MCP server-to-Agent loop are covered;
- adopted and rejected reference patterns are recorded in `docs/REFERENCE_DESIGN.md`.

## Decisions still requiring explicit approval

- public software license;
- package publication destination and ownership;
- support window for Python versions below 3.12;
- whether optional adapters live in this repository or separate repositories.
