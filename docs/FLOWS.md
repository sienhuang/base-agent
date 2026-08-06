# Controlled Agent Flows

For the common case, compose definition-backed Agents directly:

```python
from base_agent import Agent, AgentDefinition, Flow

researcher = Agent(
    definition=AgentDefinition(
        id="researcher",
        version="1.0.0",
        instructions="Research the requested topic.",
    ),
    model=research_model,
)
writer = Agent(
    definition=AgentDefinition(
        id="writer",
        version="1.0.0",
        instructions="Write a concise report from the prior Agent's handoff.",
    ),
    model=writer_model,
)

flow = Flow.sequence(
    {"researcher": researcher, "writer": writer},
    id="research-report",
)
run = await flow.run("Research the topic and write a report.")

print(run.status, run.output)
```

`Flow.sequence(...)` supplies the in-process lifecycle, repository, Runtime invoker, and sequential
strategy. `FlowRun` exposes `resume(...)`, `confirm(...)`, `cancel(...)`, and `events(...)`, so an
application does not need to wire those components for a simple single-process Flow.

This facade is intentionally small and currently uses in-memory Flow state. Applications that need
durable workers, PostgreSQL, custom recovery, or another strategy use the advanced contracts below.

## Advanced: define named Agents

```python
from base_agent import AgentDefinition, FlowAgent, FlowBudget, FlowDefinition

researcher = AgentDefinition(
    id="research-agent",
    version="1.0.0",
    instructions="Research the requested topic.",
    tools=("search",),
    permissions=frozenset({"web:search"}),
)
writer = AgentDefinition(
    id="writer-agent",
    version="1.0.0",
    instructions="Write a concise report from an explicit handoff.",
)

flow = FlowDefinition(
    id="research-report",
    version="1.0.0",
    agents=(
        FlowAgent(key="researcher", definition=researcher),
        FlowAgent(key="writer", definition=writer),
    ),
    strategy="sequential",
    budget=FlowBudget(
        max_invocations=4,
        max_total_tokens=40_000,
        max_model_calls=20,
        max_tool_calls=40,
        timeout_seconds=300,
    ),
)
```

`FlowAgent.key` is the stable name used by a Flow strategy. It is separate from
`AgentDefinition.id`, so one definition can be bound to a role without changing the Agent itself.
Keys must be unique inside a Flow.

`FlowDefinition.fingerprint` includes the ordered Agent bindings and each nested
`AgentDefinition.fingerprint`. It is intended for compatibility checks and future Flow/Harness
snapshots.

The strategy value selects a compatible strategy implementation. `SequentialFlowStrategy`
requires `strategy="sequential"`. There is no global strategy registry; applications compose the
strategy and its dependencies explicitly.

## Advanced: enforce one Flow-wide budget

`FlowBudget` is included in the Flow fingerprint and persisted in every `FlowRunState`. Its
versioned schema can constrain:

- Agent invocation count;
- input, output, and total Tokens;
- model and Tool call counts;
- total wall-clock execution time.

Each Agent still enforces its own `AgentDefinition` limits. `FlowBudget` is an additional aggregate
ceiling across all Agents. Usage and call counts come from cumulative `AgentInvocationResult`
records, so WAITING/resume does not double-count them.

The Harness checks aggregate limits after every result and before every new invoke or resume.
Exactly consuming a limit may complete the current Flow, but no further transport call is allowed.
A result that exceeds a limit immediately terminates the Flow as `LIMIT_REACHED`. Wall-clock
budgets wrap Agent transport with an asyncio deadline, so slow work is cancelled rather than only
being detected afterward.

Budget termination emits `flow.limit_reached` with structured `kind`, `limit`, and `actual`
metadata. `FlowResult` exposes cumulative Tokens, model calls, and Tool calls.

For backwards compatibility, `FlowDefinition(max_invocations=...)` is accepted and normalized into
`FlowBudget.max_invocations`. New definitions should use `budget=FlowBudget(...)`.

## Advanced: wire a sequential Flow

```python
from base_agent import (
    FlowInput,
    FlowLifecycle,
    InMemoryFlowRepository,
    SequentialFlowStrategy,
)

repository = InMemoryFlowRepository()
strategy = SequentialFlowStrategy(
    lifecycle=FlowLifecycle(repository),
    invoker=agent_runtime_invoker,
)

result = await strategy.run(
    flow,
    FlowInput(prompt="Research the topic and write a report."),
)
```

The strategy invokes each `FlowDefinition.agents` binding once in declaration order. The original
Flow prompt and explicitly selected attachments are available to each invocation. After the first
Agent, the next invocation also receives an `AgentHandoff` built from only the previous result's
text summary, JSON metadata, and Artifact references.

It returns `FlowResult`, which exposes the top-level status, output, cumulative Usage, Artifacts,
invocation count, and—when waiting—the pending input and invocation identifier.

## Connect configured Agents

`AgentRuntimeInvoker` maps each stable Flow `agent_key` to an already configured `Agent`:

```python
from base_agent import AgentRuntimeInvoker

invoker = AgentRuntimeInvoker(
    flow,
    {
        "researcher": researcher_agent,
        "writer": writer_agent,
    },
)
```

Each Agent retains its own model, Prompt, Tools, Skills, permissions, limits, and Resource
configuration. The adapter rejects missing or extra keys, legacy profile-only Agents, and
mismatched definition fingerprints. Independently constructed Agents can be composed directly;
production applications may still inject shared stores at their composition root when they need
one persistence substrate.

The internal Agent Runtime record uses `invocation_id` as its Run ID and stores `flow_run_id`,
`invocation_id`, `agent_key`, definition identity, and `execution_scope="flow_invocation"` in both
the Run metadata and its `run.created` event. The Flow aggregate remains the orchestration source
of truth; the correlated child record contains the detailed Model/Tool execution history.

`DefaultAgentInvocationPromptBuilder` renders only the original Flow prompt and the explicit
handoff. It does not merge message history. Handoff JSON is clearly delimited and has a default
20,000-character limit. Applications may provide another `AgentInvocationPromptBuilder` when they
need a stricter domain-specific context format.

## Pass explicit handoffs

Agents do not inherit another Agent's full message history or permissions. A Flow passes a bounded
handoff:

```python
from base_agent import AgentHandoff, AgentInvocationInput

writer_input = AgentInvocationInput(
    prompt="Write the final report.",
    handoff=AgentHandoff(
        source_agent_key="researcher",
        summary="Three sources support the conclusion.",
        data={"source_ids": ["source-1", "source-2", "source-3"]},
        artifacts=(),
    ),
)
```

A handoff may contain:

- a human-readable summary;
- JSON-compatible structured data;
- immutable Artifact references.

It does not contain arbitrary Message history, live Resources, Provider clients, Tool
implementations, or inherited permissions.

## AgentInvoker boundary

Flow strategies call Agents only through `AgentInvoker`:

```python
class AgentInvoker(Protocol):
    async def invoke(
        self,
        request: AgentInvocationRequest,
    ) -> AgentInvocationResult: ...

    async def resume(
        self,
        request: AgentInvocationResume,
    ) -> AgentInvocationResult: ...
```

Every invocation request is correlated and definition-pinned by:

- `flow_run_id`;
- `invocation_id`;
- invocation `sequence`;
- `agent_key`;
- Agent definition ID, version, and fingerprint.

`AgentInvocationResult` supports the same bounded outcomes as an Agent Result, including WAITING
with a required `PendingInput`. Resume addresses the same invocation explicitly.

`AgentRuntimeInvoker` is the in-process implementation. Applications can replace it through the
same Protocol, while `ScriptedAgentInvoker` keeps complete Flow tests deterministic and
network-free.

### Optional cancellation capability

Invokers that control a child execution should also implement `CancellableAgentInvoker`:

```python
class CancellableAgentInvoker(Protocol):
    async def cancel(self, request: AgentInvocationCancel) -> None: ...
```

`SequentialFlowStrategy.cancel()` first commits the parent Flow and active AgentInvocation as
CANCELLED, then propagates the bounded cancellation request. This state-before-transport ordering
keeps the Flow terminal even if the child transport is unavailable.

For a WAITING `AgentRuntimeInvoker`, propagation cancels the child Run and deletes its checkpoint.
For a RUNNING child, it sets the existing cooperative cancellation signal; a late child result
reads the already committed Flow terminal state instead of overwriting it. Repeating cancellation
on an already-cancelled Flow is idempotent.

If an Invoker does not implement cancellation or its cancellation call fails, the Flow remains
cancelled and the repository appends
`agent_invocation.cancellation_propagation_failed`. That event records only correlation identity,
Flow status, and error type—not exception text or user data.

## Flow lifecycle state

`FlowRunState` and `AgentInvocation` define the serializable lifecycle that a Flow Runtime will
persist:

```text
FlowRunState CREATED
  → RUNNING
      → begin AgentInvocation RUNNING
          → COMPLETED / FAILED / CANCELLED / INTERRUPTED / LIMIT_REACHED
          → WAITING → resumed result
      → next AgentInvocation
  → COMPLETED / FAILED / CANCELLED / INTERRUPTED / LIMIT_REACHED
```

The state model enforces:

- one active AgentInvocation at a time;
- contiguous invocation sequence numbers;
- FlowDefinition ID, version, and fingerprint matching;
- invocation and result identity matching;
- explicit WAITING with one active waiting invocation;
- no successful Flow completion after an unsuccessful invocation;
- cumulative per-invocation Usage without double-counting WAITING/resume results;
- termination propagation to an active invocation;
- retention of accrued Usage when a waiting Flow is cancelled.

`FlowRunState` is intentionally immutable. Lifecycle operations return a replacement state and
increment its `revision`, making checkpoint serialization, optimistic concurrency control, and
deterministic transition tests straightforward.

## Persist lifecycle facts atomically

`FlowLifecycle` is the only application service that should advance a persisted Flow aggregate:

```python
from base_agent import FlowLifecycle, InMemoryFlowRepository

repository = InMemoryFlowRepository()
lifecycle = FlowLifecycle(repository)

state = await lifecycle.create(flow)
state = await lifecycle.start(state.run_id)
```

Each transition atomically stores the replacement `FlowRunState` and one or more ordered
`RuntimeEvent` records. `FlowRepository.commit()` uses `expected_revision` as a compare-and-swap
guard, so a stale coordinator receives `FlowRevisionConflictError` instead of overwriting newer
state.

The service emits metadata-only Flow and AgentInvocation events. Prompt text, Agent output,
pending-input prompts, and terminal error text remain in the aggregate rather than being copied
into event payloads. A WAITING result stores the waiting snapshot and both
`agent_invocation.waiting` and `flow.waiting` in one commit. Terminating an active Flow records the
child invocation terminal event before the parent Flow terminal event.

Resume first atomically changes both the waiting Invocation and Flow back to RUNNING and records
`agent_invocation.resumed` plus `flow.resumed`. Only then does
`SequentialFlowStrategy` call `AgentInvoker.resume()`. This claim-before-transport ordering avoids
two coordinators both performing the same resume side effect.

`InMemoryFlowRepository` is suitable for deterministic tests and single-process use. It returns
defensive copies and assigns contiguous event sequence numbers under one lock.

For durable deployments, `PostgresFlowRepository` preserves the same contract:

```python
from base_agent import FlowLifecycle
from base_agent.stores.postgres import PostgresFlowRepository, PostgresStore

store = PostgresStore.from_url(database_url)
await store.create_schema()
repository = PostgresFlowRepository(store.engine)
lifecycle = FlowLifecycle(repository)
```

The PostgreSQL adapter locks the Flow row before assigning event sequences. Snapshot replacement
and all events for that transition commit in one transaction, while revision/CAS rejects stale
coordinators. Separate Flow tables deliberately avoid conflating a top-level Flow Run with its
child Agent Runtime Runs.

## Worker execution leases

`InMemoryFlowRepository` and `PostgresFlowRepository` also implement `FlowLeaseRepository`.
A durable worker claims a Flow before advancing it and binds that lease to its lifecycle:

```python
lease = await repository.acquire_execution(
    run_id,
    owner_id=worker_id,
    ttl_seconds=30,
)
leased_lifecycle = FlowLifecycle(repository, execution_lease=lease)

try:
    state = await leased_lifecycle.start(run_id)
    lease = await repository.renew_execution(lease, ttl_seconds=30)
finally:
    await repository.release_execution(lease)
```

The lease contains a random fencing token and a monotonically increasing attempt number. Only one
unexpired lease can exist for a Flow. After expiry, another worker may acquire the next attempt;
the old worker's token can no longer commit a snapshot or append an event even if it resumes later.

Once a Flow has been claimed, all later writes require a current lease token—even after release or
expiry. This prevents trusted internal code from accidentally falling back to an unfenced write.
Unclaimed Flows retain the existing zero-infrastructure embedded behavior.

Lease renewal is deliberately explicit: the worker runner owns the heartbeat schedule and graceful
release policy. `FlowExecutionRunner` provides that queue-independent ownership lifecycle:

```python
from base_agent import FlowExecutionRunner, FlowLifecycle

runner = FlowExecutionRunner(
    repository,
    owner_id=worker_id,
    ttl_seconds=30,
    heartbeat_interval=10,
)

async def handle(lease):
    lifecycle = FlowLifecycle(repository, execution_lease=lease)
    return await advance_claimed_flow(lifecycle, run_id)

result = await runner.execute(run_id, handle)
```

The runner claims before calling the handler, renews in a sibling task, and releases in `finally`.
If renewal reports a lost lease, it cancels the local handler immediately; repository fencing still
protects durable writes if downstream code delays or suppresses cancellation. Cancelling the runner
also cancels the handler and attempts graceful release.

Lease rows are operational coordination state rather than business lifecycle events, so heartbeats
do not inflate the immutable Flow event history. `FlowExecutionRunner` intentionally does not poll
a queue, resolve definitions, or decide whether an interrupted external side effect is safe to
repeat.

## Durable work source

`FlowWorkSource` separates durable task delivery from Flow write ownership:

```python
command = FlowWorkCommand(
    run_id=run_id,
    kind=FlowWorkKind.EXECUTE,
    idempotency_key=request_id,
)
await work_source.enqueue(command)

item = await work_source.claim(
    owner_id=worker_id,
    ttl_seconds=30,
)
```

`InMemoryFlowWorkSource` provides deterministic local semantics;
`PostgresFlowWorkSource` uses row locking with `SKIP LOCKED` so worker processes can claim different
eligible rows concurrently. Enqueue is idempotent: repeating the same key and intent returns the
original item, while reusing a key for another Run, kind, or data raises
`FlowWorkIdempotencyConflictError`.

Every claim increments the delivery attempt and creates a new delivery token. An expired item may
be redelivered, but the old token cannot complete or retry the new delivery. `complete()` and
`retry()` are idempotent for the token that performed the settlement. Long-running handlers renew
the delivery through `renew()` without changing its attempt or token. Retry supports a durable
availability delay and records only a bounded error type.

The two fencing layers solve different races:

```text
FlowWorkSource delivery token   → may this worker settle this queued item?
FlowExecutionLease token        → may this worker mutate this Flow aggregate?
```

A worker must hold both while processing durable work. Work command `data` is durable application
data, not an event payload; resume inputs or other sensitive values placed there require the host
application's normal encryption, authorization, and retention controls.

`FlowPollingWorker` composes both fencing layers into a single-concurrency worker:

```python
async def handle(item, execution_lease):
    lifecycle = FlowLifecycle(
        flow_repository,
        execution_lease=execution_lease,
    )
    await dispatch_command(item.command, lifecycle)

worker = FlowPollingWorker(
    work_source,
    execution_runner,
    handle,
    owner_id=worker_id,
)
await worker.run_forever()
```

For each item it:

1. claims a fenced work delivery;
2. heartbeats the delivery while `FlowExecutionRunner` independently heartbeats Flow ownership;
3. invokes the application handler only after both ownership layers are established;
4. completes successful work or schedules failed work for delayed retry;
5. cancels the handler if either heartbeat loses ownership;
6. requeues an active item when the worker task is cancelled.

`run_once()` supports deterministic tests and externally managed loops. `run_forever()` polls
sequentially and `stop()` requests graceful shutdown after the active item settles. Handler
exception text is not copied into work state or default worker logs; only its bounded type is
recorded.

The generic polling worker delegates Definition resolution and recovery decisions to its handler;
the reusable conservative handler below supplies the default. Automatic side-effect replay is
allowed only when the optional metadata-only side-effect ledger supplies explicit retry evidence.
Authenticated operator transport and configurable worker concurrency remain application work.

## Definition resolution and recovery policy

`FlowDefinitionResolver` resolves the exact definition ID and version recorded by `FlowRunState`.
`InMemoryFlowDefinitionRegistry` is the composition-root implementation for configured
applications and tests. `DefinitionResolvingFlowWorkHandler` additionally compares the resolved
fingerprint with the persisted fingerprint before any dispatcher code runs.

The default `FlowRecoveryPolicy` is intentionally conservative:

| Command and persisted boundary | Decision |
| --- | --- |
| EXECUTE/RECOVER + CREATED | START |
| EXECUTE/RECOVER + RUNNING with no active Invocation | ADVANCE |
| EXECUTE/RECOVER + settled unsuccessful Invocation | FINALIZE |
| RESUME + WAITING + non-empty `data.user_input` | RESUME |
| CANCEL + any non-terminal state | CANCEL |
| Any command + terminal state | NOOP |
| Active RUNNING Invocation, with or without retry-safe evidence | MANUAL_REVIEW |
| Active Invocation + any started/confirmed unsafe effect | MANUAL_REVIEW |
| WAITING without an accepted resume input | MANUAL_REVIEW |
| Definition missing or fingerprint changed | MANUAL_REVIEW |

START, ADVANCE, RESUME, FINALIZE, and CANCEL are passed to the application
`FlowRecoveryDispatcher` as a `FlowRecoveryContext` containing the command, pinned definition,
snapshot, decision, lease-bound `FlowLifecycle`, and bounded side-effect evidence. The application
dispatcher remains responsible for implementing those explicit safe-boundary actions. NOOP
completes the work item without invoking the dispatcher.

MANUAL_REVIEW raises a metadata-only `FlowWorkBlockedError`; `FlowPollingWorker` settles the item as
`BLOCKED` with a bounded reason code. Blocked work is not claimable and is therefore not retried in
a poison loop.

The policy does not assume an active Agent invocation is idempotent. A worker crash between child
transport and result commit is always treated as uncertain by the default policy. Retry-safe ledger
evidence remains useful to an operator or a future application-specific recovery policy, but does
not enable automatic replay.

## Side-effect recovery evidence

`FlowSideEffectLedger` records only correlation and safety metadata:

- Flow Run ID, Invocation ID, stable operation key, and operation name;
- retry mode and a SHA-256 digest of a downstream idempotency key;
- PREPARED, STARTED, CONFIRMED, or ABORTED phase;
- revision and timestamps.

It deliberately excludes Tool arguments, Tool results, the original idempotency key, Prompt data,
and exception text. `FlowSideEffectEvidenceReader` is a narrower read-only port for recovery
workers. In-memory and PostgreSQL implementations share idempotent prepare and revision-CAS
transition semantics.

The lifecycle is conservative: PREPARED may move to STARTED or ABORTED; ABORTED may start on a
later known-safe attempt; STARTED may only move to CONFIRMED. A timeout or crash after STARTED
remains STARTED because the framework cannot infer whether the external system committed.
PREPARED/ABORTED effects are retry-safe because they are known not to have executed.
STARTED/CONFIRMED effects are retry-safe only when they carry an explicit downstream idempotency
guarantee. With no records, the active Invocation remains blocked because absence of evidence is
not evidence of absence.

`ToolSideEffectMode` lets Tool implementations declare UNSPECIFIED, READ_ONLY, UNSAFE, or
IDEMPOTENT behavior without adding governance metadata to the model-facing schema.
`FlowToolSideEffectRecorder` adapts `ToolExecutor` to this ledger. It validates arguments before
recording STARTED, injects a stable downstream idempotency key through `ToolContext` for IDEMPOTENT
Tools, confirms normal returns, and leaves timeout/error outcomes STARTED. Applications opt in by
passing the recorder to each Flow-owned Agent and the same ledger's read-only view to the recovery
handler.

Important effects may additionally declare `ToolConfirmationMode.REQUIRED`. The Agent Runtime
suspends before ledger PREPARED/STARTED and persists a bounded request tied to the original
ToolCall. `Agent.confirm()` and `SequentialFlowStrategy.confirm()` accept a typed
`ToolConfirmation`; free-form `resume()` cannot approve an effect. Approval re-enters the original
suspended call without a second Tool count, while rejection produces a denied observation without
starting the Tool or ledger. Confirmation decisions emit metadata-only requested/decided events.
Operator identity in the decision is audit data; authentication and authorization remain host
responsibilities.

## Operator review for BLOCKED work

`FlowWorkReviewStore` is a trusted operator port deliberately separated from `FlowWorkSource`.
Workers need delivery methods but do not need authority to approve their own blocked work. The
in-memory and PostgreSQL adapters implement both protocols so the composition root can expose only
the minimum interface to each service.

```python
blocked = await review_store.list_blocked(limit=100)

result = await review_store.review(
    FlowWorkReview(
        work_id=blocked_item.id,
        decision=FlowWorkReviewDecision.APPROVE_RETRY,
        reviewer_id=authenticated_subject,
        reason_code="downstream_idempotency_verified",
        idempotency_key=request_id,
        delay_seconds=5,
    )
)
```

Operator decisions are:

- `APPROVE_RETRY`: atomically records the review and returns the item to `PENDING`, optionally after
  a durable delay;
- `REJECT`: atomically records the review and moves the item to terminal `DISCARDED`.

Every review stores reviewer identity, bounded reason code, decision, retry delay, idempotency key,
and timestamp. Review idempotency prevents a retried operator request from applying twice. Two
different concurrent decisions serialize on the WorkItem row; exactly one can move the item out of
BLOCKED. `list_reviews(work_id)` returns the immutable audit history.

These are core library ports, not unauthenticated HTTP endpoints. `reviewer_id` is audit data, not
proof of authorization. The host application must authenticate the operator, enforce tenant and
Run ownership, authorize the requested decision, and apply retention policy before invoking
`FlowWorkReviewStore`. Review reason codes should remain metadata-only and must not contain Prompt,
resume input, or exception text.

## Test Flow strategies offline

`ScriptedAgentInvoker` returns per-Agent outcomes in order and records invocation/resume requests:

```python
from base_agent import AgentResultStatus
from base_agent.testing import ScriptedAgentInvoker, ScriptedAgentOutcome

invoker = ScriptedAgentInvoker(
    {
        "researcher": (
            ScriptedAgentOutcome(
                status=AgentResultStatus.COMPLETED,
                output="Research complete.",
            ),
        ),
    }
)
```

Exhausted scripts fail loudly with `ScriptedAgentInvokerExhaustedError`. This follows the same
testing principle as `FakeModel`: Flow behavior can be verified without a model, network, database,
or application-specific Agent implementation.

For complete scenarios, `FlowTestHarness` removes the manual Repository/Lifecycle/Strategy setup:

```python
from base_agent.testing import FlowTestHarness

harness = FlowTestHarness(flow, scripted_outcomes)
episode = await harness.run("Research and write the report.")

assert episode.agent_keys == ("researcher", "writer")
assert episode.requests_for("writer")[0].input.handoff is not None
assert episode.event_types[-1].value == "flow.completed"
```

`FlowTestRun` includes cumulative result, state, events, invocations, and all invoke/resume/cancel
envelopes for that Flow only. `resume()` and `cancel()` return replacement evidence snapshots for
the same tracked Run.

## Next implementation boundary

The next Flow slice must add:

1. Flow checkpoint and restart integration;
2. authenticated Operator/API transport for Tool confirmation decisions;
3. optional per-Agent sub-budget allocation when production use cases require it;
4. cancellation acknowledgements or leases for remote asynchronous Invokers;
5. RouterFlow and PlannerExecutorFlow after the durable lifecycle boundary is stable.

Planner/Executor routing and parallel invocation should come only after this lifecycle is stable.
