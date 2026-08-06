import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from base_agent import (
    AgentDefinition,
    FlowAgent,
    FlowDefinition,
    FlowLeaseLostError,
    FlowLeaseUnavailableError,
    FlowLifecycle,
    InMemoryFlowRepository,
)


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def make_flow() -> FlowDefinition:
    return FlowDefinition(
        id="leased-flow",
        version="1.0.0",
        agents=(
            FlowAgent(
                key="worker",
                definition=AgentDefinition(
                    id="leased-agent",
                    version="1.0.0",
                    instructions="Work.",
                ),
            ),
        ),
        strategy="sequential",
    )


@pytest.mark.asyncio
async def test_only_one_worker_can_claim_and_renew_a_flow() -> None:
    clock = MutableClock()
    repository = InMemoryFlowRepository(clock=clock)
    created = await FlowLifecycle(repository).create(make_flow(), now=clock())

    claims = await asyncio.gather(
        repository.acquire_execution(
            created.run_id,
            owner_id="worker-a",
            ttl_seconds=10,
        ),
        repository.acquire_execution(
            created.run_id,
            owner_id="worker-b",
            ttl_seconds=10,
        ),
        return_exceptions=True,
    )

    lease = next(item for item in claims if not isinstance(item, Exception))
    assert lease.attempt == 1
    assert sum(isinstance(item, FlowLeaseUnavailableError) for item in claims) == 1

    clock.advance(5)
    untrusted_copy = lease.model_copy(
        update={"owner_id": "forged-owner", "attempt": 999}
    )
    renewed = await repository.renew_execution(untrusted_copy, ttl_seconds=10)
    assert renewed.token == lease.token
    assert renewed.owner_id == lease.owner_id
    assert renewed.attempt == 1
    assert renewed.heartbeat_at == clock()
    assert renewed.expires_at == clock() + timedelta(seconds=10)


@pytest.mark.asyncio
async def test_expired_worker_is_fenced_after_another_worker_takes_over() -> None:
    clock = MutableClock()
    repository = InMemoryFlowRepository(clock=clock)
    created = await FlowLifecycle(repository).create(make_flow(), now=clock())
    first = await repository.acquire_execution(
        created.run_id,
        owner_id="worker-a",
        ttl_seconds=10,
    )
    first_lifecycle = FlowLifecycle(repository, execution_lease=first)

    clock.advance(11)
    second = await repository.acquire_execution(
        created.run_id,
        owner_id="worker-b",
        ttl_seconds=10,
    )

    assert second.attempt == 2
    assert second.token != first.token
    with pytest.raises(FlowLeaseLostError, match="not valid"):
        await first_lifecycle.start(created.run_id)

    started = await FlowLifecycle(
        repository,
        execution_lease=second,
    ).start(created.run_id)
    assert started.revision == 2


@pytest.mark.asyncio
async def test_claimed_flow_rejects_unfenced_writes_even_after_release() -> None:
    clock = MutableClock()
    repository = InMemoryFlowRepository(clock=clock)
    lifecycle = FlowLifecycle(repository)
    created = await lifecycle.create(make_flow(), now=clock())
    lease = await repository.acquire_execution(
        created.run_id,
        owner_id="worker-a",
        ttl_seconds=10,
    )

    with pytest.raises(FlowLeaseLostError, match="not valid"):
        await lifecycle.start(created.run_id)

    await repository.release_execution(lease)
    await repository.release_execution(lease)
    with pytest.raises(FlowLeaseLostError, match="not valid"):
        await FlowLifecycle(
            repository,
            execution_lease=lease,
        ).start(created.run_id)

    replacement = await repository.acquire_execution(
        created.run_id,
        owner_id="worker-b",
        ttl_seconds=10,
    )
    assert replacement.attempt == 2
    await FlowLifecycle(
        repository,
        execution_lease=replacement,
    ).start(created.run_id)


@pytest.mark.asyncio
async def test_lease_cannot_be_reused_for_another_flow() -> None:
    repository = InMemoryFlowRepository()
    lifecycle = FlowLifecycle(repository)
    first = await lifecycle.create(make_flow())
    second = await lifecycle.create(make_flow())
    lease = await repository.acquire_execution(
        first.run_id,
        owner_id="worker-a",
        ttl_seconds=10,
    )

    with pytest.raises(ValueError, match="execution lease belongs"):
        await FlowLifecycle(
            repository,
            execution_lease=lease,
        ).start(second.run_id)
