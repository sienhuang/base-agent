import asyncio

import pytest

from base_agent import (
    AgentDefinition,
    FlowAgent,
    FlowDefinition,
    FlowExecutionLease,
    FlowExecutionRunner,
    FlowLeaseLostError,
    FlowLeaseUnavailableError,
    FlowLifecycle,
    InMemoryFlowRepository,
)


def make_flow() -> FlowDefinition:
    return FlowDefinition(
        id="worker-flow",
        version="1.0.0",
        agents=(
            FlowAgent(
                key="worker",
                definition=AgentDefinition(
                    id="worker-agent",
                    version="1.0.0",
                    instructions="Work.",
                ),
            ),
        ),
        strategy="sequential",
    )


class ObservingLeaseRepository(InMemoryFlowRepository):
    def __init__(self) -> None:
        super().__init__()
        self.renewed = asyncio.Event()
        self.renew_count = 0

    async def renew_execution(
        self,
        lease: FlowExecutionLease,
        *,
        ttl_seconds: float,
        now=None,
    ) -> FlowExecutionLease:
        renewed = await super().renew_execution(
            lease,
            ttl_seconds=ttl_seconds,
            now=now,
        )
        self.renew_count += 1
        self.renewed.set()
        return renewed


class LosingLeaseRepository(InMemoryFlowRepository):
    def __init__(self) -> None:
        super().__init__()
        self.renew_attempted = asyncio.Event()

    async def renew_execution(
        self,
        lease: FlowExecutionLease,
        *,
        ttl_seconds: float,
        now=None,
    ) -> FlowExecutionLease:
        self.renew_attempted.set()
        raise FlowLeaseLostError(f"lease for '{lease.run_id}' was lost")


@pytest.mark.asyncio
async def test_runner_claims_binds_and_releases_execution() -> None:
    repository = InMemoryFlowRepository()
    created = await FlowLifecycle(repository).create(make_flow())
    runner = FlowExecutionRunner(
        repository,
        owner_id="worker-a",
        ttl_seconds=1,
        heartbeat_interval=0.1,
    )

    async def execute(lease: FlowExecutionLease) -> str:
        state = await FlowLifecycle(
            repository,
            execution_lease=lease,
        ).start(created.run_id)
        return state.status.value

    assert await runner.execute(created.run_id, execute) == "running"
    next_lease = await repository.acquire_execution(
        created.run_id,
        owner_id="worker-b",
        ttl_seconds=1,
    )
    assert next_lease.attempt == 2


@pytest.mark.asyncio
async def test_runner_heartbeats_while_handler_is_active() -> None:
    repository = ObservingLeaseRepository()
    created = await FlowLifecycle(repository).create(make_flow())
    release = asyncio.Event()
    runner = FlowExecutionRunner(
        repository,
        owner_id="worker-a",
        ttl_seconds=1,
        heartbeat_interval=0.01,
    )

    async def execute(lease: FlowExecutionLease) -> None:
        await repository.renewed.wait()
        with pytest.raises(FlowLeaseUnavailableError):
            await repository.acquire_execution(
                lease.run_id,
                owner_id="worker-b",
                ttl_seconds=1,
            )
        release.set()

    await runner.execute(created.run_id, execute)
    assert repository.renew_count >= 1
    assert release.is_set()


@pytest.mark.asyncio
async def test_runner_cancels_handler_when_heartbeat_loses_ownership() -> None:
    repository = LosingLeaseRepository()
    created = await FlowLifecycle(repository).create(make_flow())
    cancelled = asyncio.Event()
    runner = FlowExecutionRunner(
        repository,
        owner_id="worker-a",
        ttl_seconds=1,
        heartbeat_interval=0.01,
    )

    async def execute(_: FlowExecutionLease) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with pytest.raises(FlowLeaseLostError):
        await runner.execute(created.run_id, execute)

    assert repository.renew_attempted.is_set()
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_runner_releases_after_handler_failure() -> None:
    repository = InMemoryFlowRepository()
    created = await FlowLifecycle(repository).create(make_flow())
    runner = FlowExecutionRunner(
        repository,
        owner_id="worker-a",
        ttl_seconds=1,
        heartbeat_interval=0.1,
    )

    async def fail(_: FlowExecutionLease) -> None:
        raise LookupError("handler failed")

    with pytest.raises(LookupError, match="handler failed"):
        await runner.execute(created.run_id, fail)

    replacement = await repository.acquire_execution(
        created.run_id,
        owner_id="worker-b",
        ttl_seconds=1,
    )
    assert replacement.attempt == 2


@pytest.mark.asyncio
async def test_cancelling_runner_cancels_handler_and_releases() -> None:
    repository = InMemoryFlowRepository()
    created = await FlowLifecycle(repository).create(make_flow())
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    runner = FlowExecutionRunner(
        repository,
        owner_id="worker-a",
        ttl_seconds=1,
        heartbeat_interval=0.1,
    )

    async def execute(_: FlowExecutionLease) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(runner.execute(created.run_id, execute))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled.is_set()
    replacement = await repository.acquire_execution(
        created.run_id,
        owner_id="worker-b",
        ttl_seconds=1,
    )
    assert replacement.attempt == 2
