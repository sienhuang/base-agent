from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from base_agent.models import RunStatus
from base_agent.run_store import (
    AgentRunRecord,
    InMemoryRunStore,
    RunLeaseConflictError,
    RunLeaseLostError,
    RunTransitionError,
)


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 20, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _record(run_id: str = "run-1") -> AgentRunRecord:
    created_at = datetime(2026, 8, 20, tzinfo=UTC)
    return AgentRunRecord(
        run_id=run_id,
        agent_id="assistant",
        agent_version="1.2.3",
        snapshot_fingerprint="snapshot-1",
        runtime_build_id="build-7",
        created_at=created_at,
        updated_at=created_at,
    )


async def test_in_memory_run_store_fences_the_full_success_lifecycle() -> None:
    clock = MutableClock()
    store = InMemoryRunStore(clock=clock)
    created = await store.create(_record())

    lease = await store.claim(
        created.run_id,
        owner_id="worker-a",
        lease_seconds=30,
    )
    running = await store.get(created.run_id)
    clock.advance(seconds=5)
    renewed = await store.renew(lease, lease_seconds=30)
    succeeded = await store.settle(
        renewed,
        status=RunStatus.SUCCEEDED,
        output={"answer": 42},
        text="done",
    )

    assert created.status is RunStatus.CREATED
    assert running.status is RunStatus.RUNNING
    assert running.lease_token == lease.token
    assert renewed.expires_at > lease.expires_at
    assert succeeded.status is RunStatus.SUCCEEDED
    assert succeeded.output == {"answer": 42}
    assert succeeded.lease_token is None
    assert succeeded.revision == 4
    succeeded.output["answer"] = 0
    assert (await store.get(created.run_id)).output == {"answer": 42}


async def test_in_memory_run_store_allows_only_one_claim() -> None:
    store = InMemoryRunStore()
    await store.create(_record())
    await store.claim("run-1", owner_id="worker-a", lease_seconds=30)

    with pytest.raises(RunLeaseConflictError, match="cannot be claimed"):
        await store.claim("run-1", owner_id="worker-b", lease_seconds=30)


async def test_create_claimed_publishes_no_unowned_created_window() -> None:
    store = InMemoryRunStore()

    lease = await store.create_claimed(
        _record(),
        owner_id="worker-a",
        lease_seconds=30,
    )

    persisted = await store.get("run-1")
    assert persisted.status is RunStatus.RUNNING
    assert persisted.revision == 2
    assert persisted.lease_token == lease.token


async def test_expired_running_run_becomes_blocked_and_is_not_replayed() -> None:
    clock = MutableClock()
    store = InMemoryRunStore(clock=clock)
    await store.create(_record())
    lease = await store.claim("run-1", owner_id="lost-worker", lease_seconds=10)
    clock.advance(seconds=11)

    blocked = await store.block_expired()

    assert len(blocked) == 1
    assert blocked[0].status is RunStatus.BLOCKED
    assert blocked[0].lease_token is None
    assert await store.block_expired() == ()
    with pytest.raises(RunLeaseConflictError, match="blocked.*cannot be claimed"):
        await store.claim("run-1", owner_id="replacement", lease_seconds=10)
    with pytest.raises(RunLeaseLostError, match="no longer owned"):
        await store.settle(lease, status=RunStatus.SUCCEEDED)


async def test_expired_reconciliation_can_be_scoped_to_one_agent() -> None:
    clock = MutableClock()
    store = InMemoryRunStore(clock=clock)
    await store.create(_record("run-a"))
    await store.create(_record("run-b").model_copy(update={"agent_id": "other-agent"}))
    await store.claim("run-a", owner_id="worker-a", lease_seconds=10)
    await store.claim("run-b", owner_id="worker-b", lease_seconds=10)
    clock.advance(seconds=11)

    blocked = await store.block_expired(agent_id="assistant")

    assert [record.run_id for record in blocked] == ["run-a"]
    assert (await store.get("run-b")).status is RunStatus.RUNNING


async def test_waiting_run_can_be_claimed_for_one_continuation() -> None:
    store = InMemoryRunStore()
    await store.create(_record())
    first = await store.claim("run-1", owner_id="worker-a", lease_seconds=30)
    waiting = await store.settle(
        first,
        status=RunStatus.WAITING,
        checkpoint_revision=3,
        pending_input={
            "request_id": "request-1",
            "kind": "tool_input",
            "tool_call_id": "call-1",
            "tool_name": "ask_user",
            "prompt": "Which region?",
            "metadata": {"field": "region"},
        },
    )
    continuation = await store.claim(
        "run-1",
        owner_id="worker-b",
        lease_seconds=30,
    )

    assert waiting.status is RunStatus.WAITING
    assert waiting.checkpoint_revision == 3
    assert (await store.get("run-1")).status is RunStatus.RUNNING
    assert continuation.owner_id == "worker-b"


async def test_run_store_rejects_invalid_settlement_and_terminal_rewrite() -> None:
    store = InMemoryRunStore()
    await store.create(_record())
    lease = await store.claim("run-1", owner_id="worker", lease_seconds=30)

    with pytest.raises(RunTransitionError, match="cannot settle"):
        await store.settle(lease, status=RunStatus.CREATED)

    await store.settle(lease, status=RunStatus.FAILED, error="failed")
    with pytest.raises(RunTransitionError, match="already reached"):
        await store.cancel_unowned("run-1", reason="too late")
