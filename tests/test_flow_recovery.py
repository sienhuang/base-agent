from uuid import UUID

import pytest

from base_agent import (
    AgentDefinition,
    AgentInvocationInput,
    AgentInvocationRequest,
    AgentInvocationResult,
    AgentResultStatus,
    DefinitionResolvingFlowWorkHandler,
    FlowAgent,
    FlowDefinition,
    FlowDefinitionConflictError,
    FlowDefinitionNotFoundError,
    FlowExecutionRunner,
    FlowLifecycle,
    FlowPollingWorker,
    FlowRecoveryAction,
    FlowRecoveryContext,
    FlowRecoveryPolicy,
    FlowRecoveryReason,
    FlowRunState,
    FlowSideEffect,
    FlowSideEffectRetryMode,
    FlowWorkCommand,
    FlowWorkKind,
    FlowWorkStatus,
    InMemoryFlowDefinitionRegistry,
    InMemoryFlowRepository,
    InMemoryFlowSideEffectLedger,
    InMemoryFlowWorkSource,
    PendingInput,
    RunStatus,
    ToolConfirmation,
    ToolConfirmationDecision,
)


def make_flow(
    *,
    version: str = "1.0.0",
    instructions: str = "Work.",
) -> FlowDefinition:
    return FlowDefinition(
        id="recovery-flow",
        version=version,
        agents=(
            FlowAgent(
                key="worker",
                definition=AgentDefinition(
                    id="recovery-agent",
                    version=version,
                    instructions=instructions,
                ),
            ),
        ),
        strategy="sequential",
    )


def invocation_request(
    run_id: UUID,
    definition: FlowDefinition,
) -> AgentInvocationRequest:
    agent = definition.agent("worker")
    return AgentInvocationRequest(
        flow_run_id=run_id,
        sequence=1,
        agent_key="worker",
        definition_id=agent.id,
        definition_version=agent.version,
        definition_fingerprint=agent.fingerprint,
        input=AgentInvocationInput(prompt="work"),
    )


def work_command(
    run_id: UUID,
    *,
    kind: FlowWorkKind = FlowWorkKind.RECOVER,
    data=None,
) -> FlowWorkCommand:
    return FlowWorkCommand(
        run_id=run_id,
        kind=kind,
        idempotency_key=f"{kind.value}:{run_id}",
        data=data or {},
    )


@pytest.mark.asyncio
async def test_definition_registry_resolves_exact_versions_and_rejects_conflicts() -> None:
    first = make_flow(version="1.0.0")
    second = make_flow(version="2.0.0")
    registry = InMemoryFlowDefinitionRegistry((first, second))

    assert await registry.resolve(first.id, first.version) == first
    assert await registry.resolve(second.id, second.version) == second
    with pytest.raises(FlowDefinitionNotFoundError):
        await registry.resolve(first.id, "3.0.0")
    with pytest.raises(FlowDefinitionConflictError, match="conflicting"):
        InMemoryFlowDefinitionRegistry(
            (first, make_flow(version="1.0.0", instructions="Changed."))
        )


def test_recovery_policy_uses_only_safe_automatic_boundaries() -> None:
    definition = make_flow()
    created = FlowRunState.create(definition)
    running = created.start()
    request = invocation_request(created.run_id, definition)
    active = running.begin_invocation(request, definition=definition)
    completed_child = active.settle_invocation(
        AgentInvocationResult(
            flow_run_id=created.run_id,
            invocation_id=request.invocation_id,
            agent_key="worker",
            status=AgentResultStatus.COMPLETED,
            output="done",
        )
    )
    failed_child = active.settle_invocation(
        AgentInvocationResult(
            flow_run_id=created.run_id,
            invocation_id=request.invocation_id,
            agent_key="worker",
            status=AgentResultStatus.FAILED,
            error="failed",
        )
    )
    waiting = active.settle_invocation(
        AgentInvocationResult(
            flow_run_id=created.run_id,
            invocation_id=request.invocation_id,
            agent_key="worker",
            status=AgentResultStatus.WAITING,
            pending_input=PendingInput(
                tool_call_id="call-1",
                tool_name="ask_user",
                prompt="question",
            ),
        )
    )
    terminal = running.fail("terminal")
    policy = FlowRecoveryPolicy()

    assert policy.decide(work_command(created.run_id), created).action is (
        FlowRecoveryAction.START
    )
    assert policy.decide(work_command(created.run_id), running).action is (
        FlowRecoveryAction.ADVANCE
    )
    active_decision = policy.decide(work_command(created.run_id), active)
    assert active_decision.action is FlowRecoveryAction.MANUAL_REVIEW
    assert (
        active_decision.reason
        is FlowRecoveryReason.ACTIVE_INVOCATION_UNCERTAIN
    )
    assert policy.decide(
        work_command(created.run_id), completed_child
    ).action is FlowRecoveryAction.ADVANCE
    assert policy.decide(
        work_command(created.run_id), failed_child
    ).action is FlowRecoveryAction.FINALIZE
    assert policy.decide(
        work_command(created.run_id, kind=FlowWorkKind.RESUME),
        waiting,
    ).action is FlowRecoveryAction.MANUAL_REVIEW
    assert policy.decide(
        work_command(
            created.run_id,
            kind=FlowWorkKind.RESUME,
            data={"user_input": "answer"},
        ),
        waiting,
    ).action is FlowRecoveryAction.RESUME
    confirmation = ToolConfirmation(
        request_id=UUID("4f64c7ba-aa8d-41ad-814f-4f45894cfe4e"),
        decision=ToolConfirmationDecision.APPROVE,
        subject_id="operator-1",
        reason_code="approved",
    )
    confirmation_decision = policy.decide(
        work_command(
            created.run_id,
            kind=FlowWorkKind.RESUME,
            data={
                "confirmation": confirmation.model_dump(mode="json"),
            },
        ),
        waiting,
    )
    assert confirmation_decision.action is FlowRecoveryAction.RESUME
    assert confirmation_decision.reason is (
        FlowRecoveryReason.WAITING_CONFIRMATION_AVAILABLE
    )
    ambiguous_resume = policy.decide(
        work_command(
            created.run_id,
            kind=FlowWorkKind.RESUME,
            data={
                "user_input": "yes",
                "confirmation": confirmation.model_dump(mode="json"),
            },
        ),
        waiting,
    )
    assert ambiguous_resume.action is FlowRecoveryAction.MANUAL_REVIEW
    assert policy.decide(
        work_command(created.run_id, kind=FlowWorkKind.CANCEL),
        running,
    ).action is FlowRecoveryAction.CANCEL
    assert policy.decide(
        work_command(created.run_id), terminal
    ).action is FlowRecoveryAction.NOOP


class CapturingDispatcher:
    def __init__(self) -> None:
        self.contexts: list[FlowRecoveryContext] = []

    async def __call__(self, context: FlowRecoveryContext) -> None:
        self.contexts.append(context)
        if context.decision.action is FlowRecoveryAction.START:
            await context.lifecycle.start(context.state.run_id)


def polling_worker(
    source: InMemoryFlowWorkSource,
    repository: InMemoryFlowRepository,
    handler: DefinitionResolvingFlowWorkHandler,
) -> FlowPollingWorker:
    return FlowPollingWorker(
        source,
        FlowExecutionRunner(
            repository,
            owner_id="recovery-execution",
            ttl_seconds=1,
            heartbeat_interval=0.1,
        ),
        handler,
        owner_id="recovery-delivery",
        delivery_ttl_seconds=1,
        delivery_heartbeat_interval=0.1,
        retry_delay_seconds=0,
        poll_interval=0.01,
    )


@pytest.mark.asyncio
async def test_resolving_handler_dispatches_pinned_safe_start() -> None:
    definition = make_flow()
    repository = InMemoryFlowRepository()
    state = await FlowLifecycle(repository).create(definition)
    source = InMemoryFlowWorkSource()
    item = await source.enqueue(
        work_command(state.run_id, kind=FlowWorkKind.EXECUTE)
    )
    dispatcher = CapturingDispatcher()
    handler = DefinitionResolvingFlowWorkHandler(
        repository,
        InMemoryFlowDefinitionRegistry((definition,)),
        dispatcher,
    )

    await polling_worker(source, repository, handler).run_once()

    assert (await source.get(item.id)).status is FlowWorkStatus.COMPLETED
    assert (await repository.get(state.run_id)).status is RunStatus.RUNNING
    assert dispatcher.contexts[0].definition.fingerprint == definition.fingerprint
    assert dispatcher.contexts[0].decision.action is FlowRecoveryAction.START


@pytest.mark.asyncio
async def test_uncertain_active_invocation_is_blocked_for_manual_review() -> None:
    definition = make_flow()
    repository = InMemoryFlowRepository()
    lifecycle = FlowLifecycle(repository)
    state = await lifecycle.create(definition)
    state = await lifecycle.start(state.run_id)
    state = await lifecycle.begin_invocation(
        state.run_id,
        invocation_request(state.run_id, definition),
        definition=definition,
    )
    source = InMemoryFlowWorkSource()
    item = await source.enqueue(work_command(state.run_id))
    dispatcher = CapturingDispatcher()
    handler = DefinitionResolvingFlowWorkHandler(
        repository,
        InMemoryFlowDefinitionRegistry((definition,)),
        dispatcher,
    )

    await polling_worker(source, repository, handler).run_once()

    blocked = await source.get(item.id)
    assert blocked.status is FlowWorkStatus.BLOCKED
    assert blocked.blocked_reason == "active_invocation_uncertain"
    assert dispatcher.contexts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retry_mode", "start_effect", "expected_action", "blocked_reason"),
    [
        (
            FlowSideEffectRetryMode.UNSAFE,
            False,
            None,
            "active_invocation_uncertain",
        ),
        (
            FlowSideEffectRetryMode.IDEMPOTENT,
            True,
            None,
            "active_invocation_uncertain",
        ),
        (
            FlowSideEffectRetryMode.UNSAFE,
            True,
            None,
            "active_side_effect_retry_unsafe",
        ),
    ],
)
async def test_active_recovery_uses_persisted_side_effect_safety(
    retry_mode: FlowSideEffectRetryMode,
    start_effect: bool,
    expected_action: FlowRecoveryAction | None,
    blocked_reason: str | None,
) -> None:
    definition = make_flow()
    repository = InMemoryFlowRepository()
    lifecycle = FlowLifecycle(repository)
    state = await lifecycle.create(definition)
    state = await lifecycle.start(state.run_id)
    state = await lifecycle.begin_invocation(
        state.run_id,
        invocation_request(state.run_id, definition),
        definition=definition,
    )
    invocation = state.active_invocation
    assert invocation is not None
    ledger = InMemoryFlowSideEffectLedger()
    prepared = await ledger.prepare(
        FlowSideEffect(
            flow_run_id=state.run_id,
            invocation_id=invocation.id,
            operation_key="tool-call-1",
            operation_name="payments.charge",
            retry_mode=retry_mode,
            idempotency_key_digest=(
                "a" * 64
                if retry_mode is FlowSideEffectRetryMode.IDEMPOTENT
                else None
            ),
        )
    )
    if start_effect:
        await ledger.mark_started(
            prepared.id,
            expected_revision=prepared.revision,
        )
    source = InMemoryFlowWorkSource()
    item = await source.enqueue(work_command(state.run_id))
    dispatcher = CapturingDispatcher()
    handler = DefinitionResolvingFlowWorkHandler(
        repository,
        InMemoryFlowDefinitionRegistry((definition,)),
        dispatcher,
        side_effect_evidence=ledger,
    )

    await polling_worker(source, repository, handler).run_once()

    settled = await source.get(item.id)
    if expected_action is None:
        assert settled.status is FlowWorkStatus.BLOCKED
        assert settled.blocked_reason == blocked_reason
        assert dispatcher.contexts == []
    else:
        assert settled.status is FlowWorkStatus.COMPLETED
        assert dispatcher.contexts[0].decision.action is expected_action
        assert dispatcher.contexts[0].side_effects


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("definitions", "reason"),
    [
        ((), "definition_not_found"),
        (
            (make_flow(instructions="Changed."),),
            "definition_fingerprint_mismatch",
        ),
    ],
)
async def test_missing_or_changed_definition_is_blocked(
    definitions: tuple[FlowDefinition, ...],
    reason: str,
) -> None:
    definition = make_flow()
    repository = InMemoryFlowRepository()
    state = await FlowLifecycle(repository).create(definition)
    source = InMemoryFlowWorkSource()
    item = await source.enqueue(work_command(state.run_id))
    handler = DefinitionResolvingFlowWorkHandler(
        repository,
        InMemoryFlowDefinitionRegistry(definitions),
        CapturingDispatcher(),
    )

    await polling_worker(source, repository, handler).run_once()

    blocked = await source.get(item.id)
    assert blocked.status is FlowWorkStatus.BLOCKED
    assert blocked.blocked_reason == reason
