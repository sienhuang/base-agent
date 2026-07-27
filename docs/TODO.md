# TODO

This document tracks capabilities that are intentionally not implemented yet. Completed historical
milestones remain in [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md).

## Priority 1 — Make ExecutionPlan executable

Status: not implemented

Current state:

- `ExecutionPlan` and `PlanStep` provide immutable graph validation and lifecycle transitions;
- `Agent.run(..., plan=plan)` and `Agent.start(..., plan=plan)` store an application-supplied plan
  in `RuntimeContext`;
- Run snapshots, events, waiting checkpoints, and resume preserve plan state;
- `update_execution_plan()` persists revisions and emits plan/step lifecycle events;
- the default `ModelToolStrategy` does not read the plan, include it in `ModelRequest`, schedule
  `ready_steps()`, or update step state;
- there is no built-in production `PlanningStrategy`; the only executable example is a
  test-local strategy in `tests/test_orchestration.py`;
- no built-in component asks a model to create an `ExecutionPlan`.

Required capability:

1. Add a public built-in planning strategy without changing the lifecycle responsibilities owned
   by `AgentRuntime`.
2. Execute an application-supplied plan in dependency order using `ready_steps()`.
3. Define an explicit step-executor contract. A step must not silently infer whether its executor
   means a Tool, model turn, application handler, or another Agent.
4. Update all plan and step state through `update_execution_plan()` so Run snapshots and events stay
   consistent.
5. Apply Supervisor budgets, Tool permissions, cancellation checks, timeouts, and result-size
   boundaries during step execution.
6. Preserve the active step, plan revision, messages, results, and pending input across
   `WAITING`/resume.
7. Define deterministic failure behavior: stop, retry, skip, or continue must be an explicit policy,
   not an implicit exception path.
8. Keep the current `ModelToolStrategy` behavior stable. Planning must be selected explicitly
   through `AgentRuntime(strategy=...)` unless a separate public API change is approved.
9. Keep the first implementation sequential. Parallel ready-step execution requires a later design
   for event ordering, shared resources, cancellation, budgets, and result merging.
10. Treat model-generated planning as a separate layer: parse and validate a provider response into
    `ExecutionPlan` before the execution strategy consumes it.

Design decisions still needed:

- the public `PlanStep.executor` format and executor registry;
- whether a model step receives the full plan, only the active step, or a bounded plan summary;
- retry/skip/fail policy representation;
- how step outputs reference large Artifacts rather than entering Plan metadata or model context;
- whether a strategy may add steps after execution begins and what graph mutations are legal;
- how planning Token usage is distinguished from execution Token usage.

Acceptance:

- a supplied two-step dependency plan executes in order and completes;
- an invalid, cyclic, or unknown-executor plan fails before side effects;
- each valid transition emits exactly one matching plan/step event;
- Tool-backed steps use the normal ToolExecutor permission, validation, timeout, and result path;
- cancellation stops before the next step and produces the normal terminal Run event;
- a waiting step checkpoints and resumes the same ToolCall and Plan revision safely;
- step failure policies are deterministic and covered individually;
- large step results use bounded summaries and Artifact references;
- the strategy works with FakeModel and in-memory stores without network services;
- pytest, Ruff, strict mypy, build, docs, and public export checks pass.

## Priority 1 — Bound Tool results

Status: designed, not implemented

Add a provider-independent serialized ToolResult size guard, typed overflow behavior, bounded event
payloads, and Artifact-reference handling. The detailed contract and acceptance outline live in
[`TOOLS.md`](TOOLS.md#planned-runtime-enforcement).

## Priority 2 — Reduce starter capability duplication

Status: not implemented

Derive `AgentProfile.tools` from an explicit starter `ENABLED_TOOLS` collection so application
authors do not repeat Tool names. Keep `REGISTERED_TOOLS`, enabled Tool selection, and granted
permissions conceptually separate. Never infer granted permissions from Tool requirements.

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

## Priority 3 — Production application infrastructure

Status: application and adapter work

- external durable task runner, lease, and process-restart recovery;
- object-storage ArtifactStore with tenant authorization;
- explicit observable Skill selection/router;
- application-level multi-Agent orchestration without domain roles entering the core.

