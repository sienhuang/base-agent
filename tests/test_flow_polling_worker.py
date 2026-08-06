import asyncio

import pytest

from base_agent import (
    AgentDefinition,
    FlowAgent,
    FlowDefinition,
    FlowExecutionLease,
    FlowExecutionRunner,
    FlowLifecycle,
    FlowPollingWorker,
    FlowWorkBlockedError,
    FlowWorkCommand,
    FlowWorkDeliveryLostError,
    FlowWorkItem,
    FlowWorkStatus,
    InMemoryFlowRepository,
    InMemoryFlowWorkSource,
)


def make_flow() -> FlowDefinition:
    return FlowDefinition(
        id="polling-flow",
        version="1.0.0",
        agents=(
            FlowAgent(
                key="worker",
                definition=AgentDefinition(
                    id="polling-agent",
                    version="1.0.0",
                    instructions="Work.",
                ),
            ),
        ),
        strategy="sequential",
    )


def make_worker(
    source: InMemoryFlowWorkSource,
    repository: InMemoryFlowRepository,
    handler,
    *,
    retry_delay_seconds: float = 0,
) -> FlowPollingWorker:
    return FlowPollingWorker(
        source,
        FlowExecutionRunner(
            repository,
            owner_id="execution-worker",
            ttl_seconds=1,
            heartbeat_interval=0.05,
        ),
        handler,
        owner_id="delivery-worker",
        delivery_ttl_seconds=1,
        delivery_heartbeat_interval=0.05,
        retry_delay_seconds=retry_delay_seconds,
        poll_interval=0.01,
    )


async def enqueue_flow(
    source: InMemoryFlowWorkSource,
    repository: InMemoryFlowRepository,
) -> FlowWorkItem:
    state = await FlowLifecycle(repository).create(make_flow())
    return await source.enqueue(
        FlowWorkCommand(
            run_id=state.run_id,
            idempotency_key=f"execute:{state.run_id}",
        )
    )


class ObservingWorkSource(InMemoryFlowWorkSource):
    def __init__(self) -> None:
        super().__init__()
        self.renewed = asyncio.Event()

    async def renew(
        self,
        work_id,
        *,
        delivery_token,
        ttl_seconds,
        now=None,
    ):
        item = await super().renew(
            work_id,
            delivery_token=delivery_token,
            ttl_seconds=ttl_seconds,
            now=now,
        )
        self.renewed.set()
        return item


class LosingWorkSource(InMemoryFlowWorkSource):
    def __init__(self) -> None:
        super().__init__()
        self.renew_attempted = asyncio.Event()

    async def renew(
        self,
        work_id,
        *,
        delivery_token,
        ttl_seconds,
        now=None,
    ):
        self.renew_attempted.set()
        raise FlowWorkDeliveryLostError(f"delivery for '{work_id}' was lost")


@pytest.mark.asyncio
async def test_worker_processes_and_completes_one_item() -> None:
    source = InMemoryFlowWorkSource()
    repository = InMemoryFlowRepository()
    queued = await enqueue_flow(source, repository)

    async def handle(
        item: FlowWorkItem,
        lease: FlowExecutionLease,
    ) -> None:
        await FlowLifecycle(
            repository,
            execution_lease=lease,
        ).start(item.command.run_id)

    worker = make_worker(source, repository, handle)

    assert await worker.run_once() is True
    assert (await source.get(queued.id)).status is FlowWorkStatus.COMPLETED
    assert (await repository.get(queued.command.run_id)).status.value == "running"
    assert await worker.run_once() is False


@pytest.mark.asyncio
async def test_handler_failure_is_retried_with_bounded_error_type() -> None:
    source = InMemoryFlowWorkSource()
    repository = InMemoryFlowRepository()
    queued = await enqueue_flow(source, repository)

    async def fail(_: FlowWorkItem, __: FlowExecutionLease) -> None:
        raise LookupError("private failure detail")

    worker = make_worker(source, repository, fail)
    with pytest.raises(LookupError, match="private failure detail"):
        await worker.run_once()

    pending = await source.get(queued.id)
    assert pending.status is FlowWorkStatus.PENDING
    assert pending.last_error_type == "LookupError"
    assert pending.attempt == 1


@pytest.mark.asyncio
async def test_worker_renews_delivery_while_handler_runs() -> None:
    source = ObservingWorkSource()
    repository = InMemoryFlowRepository()
    queued = await enqueue_flow(source, repository)

    async def wait_for_renewal(
        _: FlowWorkItem,
        __: FlowExecutionLease,
    ) -> None:
        await source.renewed.wait()

    worker = make_worker(source, repository, wait_for_renewal)
    await worker.run_once()

    assert source.renewed.is_set()
    assert (await source.get(queued.id)).status is FlowWorkStatus.COMPLETED


@pytest.mark.asyncio
async def test_lost_delivery_cancels_handler_and_requeues_when_still_owned() -> None:
    source = LosingWorkSource()
    repository = InMemoryFlowRepository()
    queued = await enqueue_flow(source, repository)
    cancelled = asyncio.Event()

    async def wait_forever(
        _: FlowWorkItem,
        __: FlowExecutionLease,
    ) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    worker = make_worker(source, repository, wait_forever)
    with pytest.raises(FlowWorkDeliveryLostError):
        await worker.run_once()

    assert source.renew_attempted.is_set()
    assert cancelled.is_set()
    assert (await source.get(queued.id)).status is FlowWorkStatus.PENDING


@pytest.mark.asyncio
async def test_cancelling_active_worker_requeues_and_preserves_cancellation() -> None:
    source = InMemoryFlowWorkSource()
    repository = InMemoryFlowRepository()
    queued = await enqueue_flow(source, repository)
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def wait_forever(
        _: FlowWorkItem,
        __: FlowExecutionLease,
    ) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    worker = make_worker(source, repository, wait_forever)
    task = asyncio.create_task(worker.run_once())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    pending = await source.get(queued.id)
    assert cancelled.is_set()
    assert pending.status is FlowWorkStatus.PENDING
    assert pending.last_error_type == "CancelledError"


@pytest.mark.asyncio
async def test_run_forever_stops_while_idle() -> None:
    source = InMemoryFlowWorkSource()
    repository = InMemoryFlowRepository()

    async def unused(_: FlowWorkItem, __: FlowExecutionLease) -> None:
        raise AssertionError("no work should be processed")

    worker = make_worker(source, repository, unused)
    task = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(0)
    worker.stop()
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_run_forever_logs_only_error_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = InMemoryFlowWorkSource()
    repository = InMemoryFlowRepository()
    await enqueue_flow(source, repository)

    async def fail_once(_: FlowWorkItem, __: FlowExecutionLease) -> None:
        worker.stop()
        raise ValueError("private prompt fragment")

    worker = make_worker(source, repository, fail_once)
    await worker.run_forever()

    assert "private prompt fragment" not in caplog.text
    record = next(
        item
        for item in caplog.records
        if item.message == "Flow work item failed and was scheduled for retry"
    )
    assert record.error_type == "ValueError"


@pytest.mark.asyncio
async def test_manual_review_signal_blocks_instead_of_retrying() -> None:
    source = InMemoryFlowWorkSource()
    repository = InMemoryFlowRepository()
    queued = await enqueue_flow(source, repository)

    async def block(_: FlowWorkItem, __: FlowExecutionLease) -> None:
        raise FlowWorkBlockedError("active_invocation_uncertain")

    worker = make_worker(source, repository, block)
    assert await worker.run_once() is True

    item = await source.get(queued.id)
    assert item.status is FlowWorkStatus.BLOCKED
    assert item.blocked_reason == "active_invocation_uncertain"
    assert item.last_error_type is None
