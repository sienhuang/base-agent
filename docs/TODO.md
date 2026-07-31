# TODO

This document tracks capabilities that are intentionally not implemented yet. Completed historical
milestones remain in [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md).

## Recommended production-hardening sequence

Work through these areas in order unless a concrete application need changes the priority:

1. Event data security policy.
2. HTTP authentication and tenant isolation.
3. Durable `RunExecutor` and worker recovery.
4. Conversation Events, Metrics, and optional tracing.
5. Provider error classification, retries, and capability declarations.
6. Context budget and compaction.
7. Tool side-effect safety and bounded results.
8. Database operations and Starter developer experience.

Keep domain behavior, BI-specific roles, deployment dashboards, and vendor-specific monitoring out
of the core package. They belong in applications or optional adapters.

## Priority 0 — Event data security policy

Status: not implemented

Current file logs omit prompts, model bodies, Tool arguments, resume input, and HTTP bodies.
Runtime Events do not currently provide the same protection: `model.requested`, `model.responded`,
Tool lifecycle, and terminal events may persist full messages, arguments, results, output, or error
text. The HTTP Event endpoint can replay those payloads.

Required capability:

- [ ] Define explicit `FULL`, `REDACTED`, and `METADATA` event payload policies.
- [ ] Keep event type, ordering, correlation identifiers, status, counts, and timing observable in
      every mode.
- [ ] Make sensitive fields redactable without changing the domain event sequence.
- [ ] Bound stored payload size and use Artifact references for large content.
- [ ] Apply the same policy to in-memory, PostgreSQL, Redis-backed replay, SSE, and HTTP history.
- [ ] Default the Starter to a safe mode while retaining an explicit local-debug option.
- [ ] Document that logs and Events have separate retention and access-control requirements.

Acceptance:

- prompts, model output, Tool arguments/results, and secrets do not appear in `METADATA` mode;
- `REDACTED` mode preserves payload shape while replacing configured sensitive values;
- ordered replay, Run recovery, SSE cursors, and terminal boundaries remain unchanged;
- redaction and size-boundary tests cover nested mappings, sequences, exceptions, and credentials.

## Priority 0 — HTTP authentication and tenant isolation

Status: not designed

Required capability:

- [ ] Add an application-supplied authentication/authorization boundary without hard-coding an
      identity vendor into core.
- [ ] Associate tenant/subject ownership with Conversations, Runs, Events, Attachments, and
      Artifacts.
- [ ] Enforce ownership consistently on create, read, stream, resume, cancel, and content download.
- [ ] Avoid revealing resource existence across tenant boundaries.
- [ ] Define trusted internal-worker access separately from end-user HTTP access.

Acceptance:

- one tenant cannot query, stream, resume, cancel, or download another tenant's resources;
- local/offline use remains possible through an explicit development configuration;
- authorization failures are auditable without logging credentials.

## Priority 1 — Durable Run execution

Status: not implemented

The current FastAPI adapter retains process-local `asyncio.Task` objects and returns `202 Accepted`.
Durable stores preserve state but do not make an interrupted active task resume after a process
restart.

Required capability:

- [ ] Define a `RunExecutor` boundary independent of HTTP and the Agent Runtime.
- [ ] Keep `LocalRunExecutor` as the zero-infrastructure default.
- [ ] Add an optional queue/worker executor with leases, heartbeats, retry ownership, and graceful
      shutdown.
- [ ] Define recovery behavior for `CREATED`, `RUNNING`, and `WAITING` Runs after worker loss.
- [ ] Preserve cancellation, single-active Conversation Turn rules, event ordering, and idempotency.
- [ ] Ensure a redelivered job cannot repeat completed side effects silently.

Acceptance:

- a queued Run survives API-process restart;
- worker loss releases or expires its lease and allows deterministic recovery;
- duplicate delivery does not create a second Run or second terminal event;
- the Starter and in-memory tests continue to work without a queue.

## Priority 1 — Complete observability

Status: partially implemented

Available now:

- application-configured daily rotating JSON Lines logs;
- request, Conversation, Run, Turn, Model, and Tool correlation fields;
- per-model-call and Run-accumulated actual Provider token usage;
- ordered Run Events, PostgreSQL persistence, Redis notification/replay, HTTP history, and SSE.

Remaining:

- [ ] Add ordered Conversation Events for create, Turn start/wait/resume/complete/fail/cancel, and
      history selection.
- [ ] Correlate Conversation Event sequence with `conversation_id`, `run_id`, and `turn_sequence`.
- [ ] Define framework-neutral metric names for Run, Model, Tool, token, error, and duration totals.
- [ ] Add optional OpenTelemetry tracing for HTTP → Run → Model → Tool/MCP/Resource boundaries.
- [ ] Keep Prometheus, Grafana, Loki, Datadog, and other vendor deployment configuration outside
      core.

Acceptance:

- one Run can be followed across logs, Events, metrics, and traces with stable identifiers;
- Conversation lifecycle is replayable independently of individual Run histories;
- observability adapters can be omitted without changing Runtime behavior.

## Priority 1 — Provider resilience and capability declarations

Status: partially implemented

- [ ] Define stable Provider error types for authentication, rate limiting, timeout, context limit,
      invalid response, unsupported input, and temporary unavailability.
- [ ] Add bounded retry/backoff policy that does not retry permanent failures.
- [ ] Record whether token usage was Provider-reported or unavailable; do not confuse unavailable
      usage with a verified zero.
- [ ] Declare Provider support for Attachments, Memory mapping, streaming, Tool calls, and structured
      output.
- [ ] Define optional fallback routing without hiding which Provider produced the result.
- [ ] Propagate request/idempotency correlation identifiers where supported.

Acceptance:

- Supervisor/application policy can distinguish retry, fallback, and terminal Provider errors;
- retries preserve one logical model-call lifecycle and respect cancellation/deadlines;
- unsupported capabilities fail before a network request.

## Priority 2 — Tool side-effect safety

Status: partially implemented

- [ ] Classify read-only and side-effecting Tools explicitly.
- [ ] Add application confirmation policy for important side effects.
- [ ] Define idempotency keys and retry rules for side-effecting Tool calls.
- [ ] Add Tool argument/result redaction and audit metadata.
- [ ] Enforce output-size limits and Artifact-reference overflow handling.
- [ ] Define Sandbox filesystem, process, and network policy boundaries.

The concrete ToolResult size-guard work is tracked in
[`TOOLS.md`](TOOLS.md#planned-runtime-enforcement).

## Priority 2 — Database operations and retention

Status: not implemented

- [ ] Add versioned PostgreSQL migrations and startup schema compatibility checks.
- [ ] Define indexes and retention for Runs, Events, Conversations, checkpoints, and Artifacts.
- [ ] Add archival/cleanup jobs that cannot remove active or resumable state.
- [ ] Add `/health/live` and dependency-aware `/health/ready` endpoints.
- [ ] Test upgrade, rollback constraints, connection loss, and partial migration failure.

## Priority 2 — Starter developer experience

Status: partially implemented

- [ ] Move the example System Prompt to `src/agent_app/prompts/system.md`.
- [ ] Add optional PostgreSQL/Redis composition examples without making them Starter requirements.
- [ ] Add a local Docker Compose validation environment.
- [ ] Include read-only, asynchronous, side-effect-confirmed, and wait/resume Tool examples.
- [ ] Add Conversation HTTP, Event replay, SSE, logging, and actual token-usage examples.
- [ ] Add a custom Provider example and production configuration checklist.
- [ ] Keep the copied project independent of mock-manus, BI-WIKI, and domain-specific packages.

## Priority 1 — Make ExecutionPlan executable

Status: first built-in version implemented; advanced execution remains

Current state:

- `ExecutionPlan` and `PlanStep` provide immutable graph validation and lifecycle transitions;
- `Agent.run(..., plan=plan)` and `Agent.start(..., plan=plan)` store an application-supplied plan
  in `RuntimeContext`;
- Run snapshots, events, waiting checkpoints, and resume preserve plan state;
- `update_execution_plan()` persists revisions and emits plan/step lifecycle events;
- the default Runtime selects built-in `PlanningStrategy` only when `plan=` or `planning=True` is
  explicit;
- the strategy executes a dependency-ready Step through the normal model/Tool loop, then invokes
  the Planner to replace all remaining work;
- every Plan update preserves settled Step objects unchanged and revalidates the merged dependency
  graph before another Step runs;
- model-generated Plans, final summarization, cancellation, failure, WAITING/resume, Plan Events,
  checkpoints, and actual Run token totals are integrated;
- plain `Agent.run(prompt)` and application-supplied custom Runtime strategies retain their prior
  behavior.

Implemented:

- [x] Execute supplied dependency Plans in deterministic order.
- [x] Generate and validate a Plan inside the same Run with `planning=True`.
- [x] Reuse Tool permissions, Supervisor budgets, cancellation, Provider usage, and normal Events.
- [x] Preserve Plan/Step state through WAITING and resume.
- [x] Emit start, wait, resume, complete, fail, cancel, and Plan revision events.
- [x] Produce a Tool-free final model summary after all Steps complete.
- [x] Review after every completed Step and replan only when pending work changes.
- [x] Permit new future Steps to depend on retained completed Step IDs.
- [x] Reject replans with duplicate IDs, missing dependencies, cycles, or non-pending new Steps.
- [x] Add standalone and Plan-Step ReAct on the shared Model/Tool loop.
- [x] Persist multi-Tool ActionBatch progress across WAITING/resume.
- [x] Emit safe ReAct iteration, ActionBatch, and ObservationBatch Events.
- [x] Keep the default non-Plan Run behavior unchanged.

Remaining:

- [ ] Add a public custom Step executor registry beyond built-in `model`/`react` aliases.
- [ ] Define explicit retry, skip, continue, and fallback policies.
- [ ] Store large Step results as Artifact references instead of unbounded Plan metadata.
- [ ] Add richer Plan-diff events beyond the persisted `plan.updated` revision.
- [ ] Design deterministic parallel execution for simultaneously ready Steps.
- [ ] Add opt-in parallel execution for explicitly safe independent Tool calls.
- [ ] Distinguish planning, execution, and summarization usage in a structured token breakdown.

## Priority 1 — Bound Tool results

Status: designed, not implemented

Add a provider-independent serialized ToolResult size guard, typed overflow behavior, bounded event
payloads, and Artifact-reference handling. The detailed contract and acceptance outline live in
[`TOOLS.md`](TOOLS.md#planned-runtime-enforcement).

## Priority 1 — Data analysis and development bundles

Status: first concrete versions implemented

- [x] Compose isolated Sandbox Tools and Resource as an explicit `CodingBundle`.
- [x] Add provider-neutral Web Search values, Tool, Bundle, and Brave adapter.
- [x] Add a provider-neutral read-only DataSource Bundle and `mtbi-cli`/OneSQL adapter.
- [x] Externalize oversized bounded DataSource query results as Run Artifacts.
- [x] Keep Tool enablement and permission grants explicit in AgentProfile/Starter configuration.
- [ ] Add safe project snapshot import, Patch Artifact export, and approved Patch application for
      Coding.
- [ ] Add a separately permissioned known-URL fetch Tool with SSRF and content-size policy.
- [ ] Add OneSQL detach/fetch/cancel handling, query resume, and scan/cost policy for long-running
      MTBI queries.
- [ ] Re-evaluate a generic public Capability contract only after these concrete bundles have
      established common lifecycle, conflict, and authorization behavior.

## Priority 2 — Reduce starter capability duplication

Status: implemented

- [x] Derive `AgentProfile.tools` from an explicit starter `ENABLED_TOOLS` collection.
- [x] Keep `REGISTERED_TOOLS`, enabled Tool selection, and granted permissions separate.
- [x] Never infer granted permissions from Tool requirements.

## Priority 2 — Context budget and compaction

Status: not designed

Add a provider-neutral context budget and history-compaction contract. Durable Events and
Checkpoints must retain audit/recovery semantics even when the message view sent to a model is
summarized or trimmed.

## Priority 2 — Additional Provider capabilities

Status: not implemented

- add a separate Responses API Provider;
- define authorized Attachment content mapping;
- define safe structured Memory mapping;
- define model-output streaming independently from Run Event streaming.

## Priority 3 — Supervisor model-directive delivery

Status: not designed

The current redirect path can append `SupervisionDecision.message` directly as a persistent
`system` message. Replace that implicit privilege escalation with an explicit, provider-neutral
directive contract:

- [ ] Separate the audit `reason`, ToolResult observation, and model-facing directive.
- [ ] Define which trusted Supervisor policies may create high-priority directives; never promote
      untrusted user input, Tool output, or exception text into a `system` message.
- [ ] Support an explicit directive scope such as `next_model_request` instead of permanently
      retaining every transient redirect in conversation history.
- [ ] Define Provider capability mapping and fallback behavior without assuming that every Provider
      accepts a mid-conversation `system` message.
- [ ] Complete or explicitly cancel every ToolCall in an ActionBatch before adding a non-Tool
      directive or issuing the next model request.
- [ ] Cover before/after-Tool redirects, multi-Tool batches, WAITING/resume, redaction, and malicious
      message content with deterministic tests.

## Priority 3 — Production application infrastructure

Status: application and adapter work

- external durable task runner, lease, and process-restart recovery;
- object-storage ArtifactStore with tenant authorization;
- explicit observable Skill selection/router;
- application-level multi-Agent orchestration without domain roles entering the core.
