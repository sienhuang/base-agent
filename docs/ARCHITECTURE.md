# Architecture

## Purpose

`base-agent` is a reusable Python runtime for executing a model-driven agent with explicit tools,
skills, supervision, run state, and events. It is a library, not a complete agent application.

The intended dependency direction is:

```text
base-agent-starter ─┐
mock-manus ─────────┼──> base-agent
BI-WIKI Agent ──────┘
       │
       └──> build-lineage adapter and domain skills
```

Applications depend on `base-agent`; the core never imports application or domain code.

## Core vocabulary

| Concept | Responsibility |
| --- | --- |
| `Agent` | Small public facade used to start or resume a run. |
| `AgentProfile` | Instructions, enabled capabilities, model route, and execution limits. |
| `Runtime` | Advances the model/tool loop and emits events. |
| `OrchestrationStrategy` | Advances one bounded turn; replaceable without replacing Run lifecycle. |
| `ExecutionPlan` | Immutable dependency graph and step lifecycle used by planning strategies. |
| `Resource` | Task-local stateful capability acquired and released around execution. |
| `ModelProvider` | Converts runtime requests into provider responses. |
| `Tool` | Typed, atomic executable capability. |
| `Skill` | Versioned instructions, tool requirements, permissions, and output contract. |
| `Supervisor` | Applies budgets, permissions, loop detection, and intervention policies. |
| `Run` | Durable execution aggregate containing status and references to its history. |
| `Event` | Immutable observation of a runtime transition. |
| `Artifact` | File or structured output produced by a run. |
| `Attachment` | Stored input reference explicitly selected for a Run. |
| `MemoryRetriever` | Optional structured context search independent of storage technology. |

## Dependency rule

The core is divided into three directions:

```text
Public API
    ↓
Runtime and domain models
    ↓
Ports implemented by in-memory defaults or optional adapters
```

Runtime code may depend on protocols and core models. Provider, database, server, sandbox, browser,
MCP, and domain adapters may depend on the core. The reverse dependency is forbidden.

## Planned package layout

```text
src/base_agent/
├── agent.py
├── profiles.py
├── models/
├── runtime/
├── orchestration/
├── resources/
├── server/             # optional FastAPI import boundary
├── postgres/           # optional durable Store implementation
├── redis/              # optional event notification implementation
├── mcp/                # optional remote Tool transport
├── sandbox/            # generic session/tools plus optional Docker module
├── browser/            # generic session/tools plus optional Playwright module
├── providers/
├── tools/
├── skills/
├── supervision/
├── stores/
└── testing/
```

Folders are introduced only when a milestone requires them. Empty architectural scaffolding is
avoided.

## Runtime boundary

The first runtime is single-agent and asynchronous. A run may perform multiple model and tool
steps, but orchestration of multiple agents is not part of the first core API.

Applications can await a Run directly or start it in the current event loop and receive a
`RunHandle`. Live event subscriptions are a capability of the EventStore, not an HTTP/SSE concern.
This keeps task control and event observation reusable by CLI, server, worker, and notebook hosts.

Human-input suspension is also a core lifecycle concern. Tools return a typed `WaitForInput`
outcome; Runtime state is persisted through `CheckpointStore`; applications collect the answer and
resume the same Run. The core does not prescribe a web socket, form, terminal, or chat transport.

Stateful infrastructure is exposed through execution-scoped `ResourceSpec` factories. Resources
are acquired and released in the same async task, and Tools receive them through a runtime-only
`ToolContext` that is omitted from model schemas. WAITING releases resources; resume reacquires
them. Checkpoints never serialize live infrastructure objects.

Input Attachments and generated Artifacts are immutable references backed by `ArtifactStore`.

Sandbox and Browser capabilities follow this same Resource boundary. Their provider-neutral
sessions and Tools do not own process-global infrastructure. Docker and Playwright implementations
are acquired lazily for one execution segment and are closed on completion, failure, cancellation,
or WAITING. Persistent cross-segment workspaces or browser profiles must be owned explicitly by the
host application; live containers and pages are never serialized into checkpoints.
Binary content never enters messages, events, Runs, Results, or checkpoints. Tools access content
through the Run-scoped ArtifactManager exposed by `ToolContext`; provider adapters receive
structured Attachment references and must map or explicitly reject them.

Memory retrieval is optional and isolated behind `MemoryRetriever`. Initial matches enter the
provider-neutral ModelRequest without changing the system prompt. Retrieval events and durable Run
metadata retain only IDs and scores; WAITING checkpoints retain selected matches so resume does not
silently change context.

The runtime owns:

- state transitions;
- event emission;
- budget and permission checks;
- cancellation checks;
- final result construction.

The selected orchestration strategy owns bounded model/tool sequencing or planning. The default
`ModelToolStrategy` preserves the simple ReAct-style loop. Custom strategies receive only generic
`RuntimeServices`; plan updates use the shared persistence and lifecycle-event operation.

The runtime does not own:

- HTTP or SSE transport;
- process or container scheduling;
- database connections;
- provider-specific clients;
- application authentication;
- domain-specific repair or analysis logic.

The optional FastAPI adapter is a transport layer over these ports. Its task manager is scoped to
one application instance and does not claim process-restart durability. Authentication, tenancy,
upload policy, and distributed scheduling remain host-application responsibilities.

## Skill boundary

A skill is not an agent subclass and not merely an unrestricted prompt. It is a versioned package
that declares instructions, required tools, permissions, input expectations, and output
expectations. The runtime will load skills progressively and record the selected skill versions in
the run history.

Domain skills remain with their domain applications. For example, build-lineage exposes atomic
tools through an adapter while BI-WIKI owns lineage analysis and SQL repair skills.

## MCP boundary

MCP is a Tool transport, not a second Agent runtime. The optional MCP client discovers remote Tool
definitions and maps them onto the existing `Tool` protocol. Model-facing names may be namespaced;
arguments still pass local validation and permissions before a remote call. Session/subprocess
lifecycle stays explicit in the host application's async context. MCP prompts, resources, sampling,
and elicitation are not silently injected into the core runtime.

## Persistence boundary

The core defines store protocols and ships in-memory implementations. The optional
`base_agent.postgres.PostgresStore` implements Run, Event, Checkpoint, Attachment, and Artifact
ports without changing the runtime. Its polling `EventStream` supports the same cursor contract as
the in-memory store, so the HTTP/SSE layer can use either implementation.

`base_agent.redis.RedisEventStore` decorates a durable `EventStore`: it writes the event to that
store first, then publishes a sequence notification. Subscribers always reconcile through the
durable store. Redis therefore reduces cross-process delivery latency but is never the sole source
of Run history, and missed Pub/Sub messages are repaired by cursor replay.

## Security boundary

- Secrets never belong in profiles, skills, examples, or committed configuration.
- Tools declare permissions and are checked before execution.
- Shell, filesystem, network, browser, and sandbox access are optional capabilities.
- The core does not mount Docker sockets or assume root access.

## Compatibility policy

Before `1.0`, public API changes are allowed between minor versions and documented in release
notes. Patch releases should remain backward compatible. Public exports are intentionally kept
small and covered by contract tests.
