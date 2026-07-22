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
| `ModelProvider` | Converts runtime requests into provider responses. |
| `Tool` | Typed, atomic executable capability. |
| `Skill` | Versioned instructions, tool requirements, permissions, and output contract. |
| `Supervisor` | Applies budgets, permissions, loop detection, and intervention policies. |
| `Run` | Durable execution aggregate containing status and references to its history. |
| `Event` | Immutable observation of a runtime transition. |
| `Artifact` | File or structured output produced by a run. |

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

The runtime owns:

- state transitions;
- model and tool sequencing;
- event emission;
- budget and permission checks;
- cancellation checks;
- final result construction.

The runtime does not own:

- HTTP or SSE transport;
- process or container scheduling;
- database connections;
- provider-specific clients;
- application authentication;
- domain-specific repair or analysis logic.

## Skill boundary

A skill is not an agent subclass and not merely an unrestricted prompt. It is a versioned package
that declares instructions, required tools, permissions, input expectations, and output
expectations. The runtime will load skills progressively and record the selected skill versions in
the run history.

Domain skills remain with their domain applications. For example, build-lineage exposes atomic
tools through an adapter while BI-WIKI owns lineage analysis and SQL repair skills.

## Persistence boundary

The core defines store protocols and ships in-memory implementations. PostgreSQL and Redis are
optional adapters. Redis may distribute events but must not be the sole durable source of run
state.

## Security boundary

- Secrets never belong in profiles, skills, examples, or committed configuration.
- Tools declare permissions and are checked before execution.
- Shell, filesystem, network, browser, and sandbox access are optional capabilities.
- The core does not mount Docker sockets or assume root access.

## Compatibility policy

Before `1.0`, public API changes are allowed between minor versions and documented in release
notes. Patch releases should remain backward compatible. Public exports are intentionally kept
small and covered by contract tests.
