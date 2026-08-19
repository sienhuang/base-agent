# ADR 0001: Artifact and Resource boundary

Status: accepted

## Decision

`Resource` and `Artifact` are different runtime concepts.

- A Resource is a live capability such as a workspace, database connection, browser,
  or authenticated client. The Harness acquires it for a Run and releases it on every
  terminal path, including cancellation.
- An Artifact is immutable content committed to an `ArtifactStore`. Its metadata
  includes a content hash and producer provenance. Runs only read their own Artifacts
  or Artifact IDs explicitly granted by their parent Flow.
- `ArtifactSpec` is the output contract declared before execution. It is not an
  Artifact. A Stage verifier binds a successful Run's produced Artifacts to its specs.
- `ToolContext` is runtime-owned and hidden from the model-visible JSON schema. It is
  the only Tool API for Artifact and Resource access.
- A workspace path is a Resource address, not durable evidence. Applications may use
  one while executing, but verified bytes are promoted into an ArtifactStore.

The first implementation provides an in-memory ArtifactStore and Run-scoped
ResourceManager. Durable metadata/content adapters, tenant policy, retention, signed
delivery URLs, and garbage collection are separate adapters and policy layers; they do
not change these protocols.

## Trust boundary

An Artifact hash proves byte identity, not semantic truth. Production confidence is a
chain: trusted producer/tool execution, immutable bytes and provenance, deterministic
verification evidence, and explicit Stage acceptance. Rejected-attempt Artifacts stay
auditable but are not granted to downstream Stages.

## Consequences

- Agent Runs own live Resources and produced Artifacts.
- SequentialFlow grants only accepted upstream Artifacts to a dependent child Run.
- A Tool-created Artifact is a candidate output. The Harness exposes compact
  `artifacts_created` receipts in that Tool's observation; the model never invents a
  path or discovers the Store directly.
- A PlannerReAct Executor selects official Step outputs with
  `StepOutcome.artifact_ids`. The Host accepts only IDs created by that same Executor
  Run for the current Flow and Step attempt, and emits `artifact.selected`.
- PlannerReAct aggregates only selected Artifact references without copying content.
  Review Planners receive selected metadata in their prompt, not raw read grants.
  Later Executor Runs are granted the selected outputs from earlier Steps.
- Created but unselected Artifacts remain in the producer `RunResult` and Store for
  audit, but are not Flow outputs and are not granted downstream.
- Applications remain responsible for domain verification (for example SQL safety,
  CSV shape and report/source consistency).
