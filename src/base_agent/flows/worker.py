"""Queue-independent worker runner for fenced Flow execution."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import UUID

from base_agent.flows.lease import (
    FlowExecutionLease,
    FlowLeaseLostError,
    FlowLeaseRepository,
)
from base_agent.flows.work import (
    FlowWorkBlockedError,
    FlowWorkDeliveryLostError,
    FlowWorkItem,
    FlowWorkSource,
)

_logger = logging.getLogger(__name__)


class FlowWorkHandler(Protocol):
    """Application callback that interprets one claimed durable command."""

    async def __call__(
        self,
        item: FlowWorkItem,
        lease: FlowExecutionLease,
    ) -> None: ...


class FlowExecutionRunner:
    """Run one Flow handler while owning and heartbeating its execution lease."""

    def __init__(
        self,
        repository: FlowLeaseRepository,
        *,
        owner_id: str,
        ttl_seconds: float = 30.0,
        heartbeat_interval: float = 10.0,
    ) -> None:
        if not owner_id.strip():
            raise ValueError("Flow worker owner_id must not be blank")
        if len(owner_id) > 256:
            raise ValueError("Flow worker owner_id must not exceed 256 characters")
        if ttl_seconds <= 0:
            raise ValueError("Flow worker ttl_seconds must be greater than zero")
        if heartbeat_interval <= 0:
            raise ValueError(
                "Flow worker heartbeat_interval must be greater than zero"
            )
        if heartbeat_interval >= ttl_seconds:
            raise ValueError(
                "Flow worker heartbeat_interval must be less than ttl_seconds"
            )
        self._repository = repository
        self._owner_id = owner_id
        self._ttl_seconds = ttl_seconds
        self._heartbeat_interval = heartbeat_interval

    async def execute[T](
        self,
        run_id: UUID,
        handler: Callable[[FlowExecutionLease], Awaitable[T]],
    ) -> T:
        """Claim one Flow, execute its handler, and release ownership."""
        lease = await self._repository.acquire_execution(
            run_id,
            owner_id=self._owner_id,
            ttl_seconds=self._ttl_seconds,
        )
        failure: BaseException | None = None
        try:
            return await self._run_owned(lease, handler)
        except BaseException as exc:
            failure = exc
            raise
        finally:
            try:
                await self._repository.release_execution(lease)
            except FlowLeaseLostError:
                if failure is None:
                    raise

    async def _run_owned[T](
        self,
        lease: FlowExecutionLease,
        handler: Callable[[FlowExecutionLease], Awaitable[T]],
    ) -> T:
        operation: asyncio.Task[T] = asyncio.create_task(
            _invoke_handler(handler, lease),
            name=f"flow-execution:{lease.run_id}",
        )
        heartbeat: asyncio.Task[None] = asyncio.create_task(
            self._heartbeat(lease),
            name=f"flow-heartbeat:{lease.run_id}",
        )
        try:
            done, _ = await asyncio.wait(
                (operation, heartbeat),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat in done:
                await heartbeat
                raise RuntimeError("Flow heartbeat stopped without an error")
            heartbeat.cancel()
            await _await_cancelled(heartbeat)
            return await operation
        finally:
            operation.cancel()
            heartbeat.cancel()
            await asyncio.gather(operation, heartbeat, return_exceptions=True)

    async def _heartbeat(self, lease: FlowExecutionLease) -> None:
        current = lease
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            current = await self._repository.renew_execution(
                current,
                ttl_seconds=self._ttl_seconds,
            )


class FlowPollingWorker:
    """Poll and settle one durable Flow work item at a time."""

    def __init__(
        self,
        work_source: FlowWorkSource,
        execution_runner: FlowExecutionRunner,
        handler: FlowWorkHandler,
        *,
        owner_id: str,
        delivery_ttl_seconds: float = 60.0,
        delivery_heartbeat_interval: float = 20.0,
        retry_delay_seconds: float = 1.0,
        poll_interval: float = 0.25,
    ) -> None:
        if not owner_id.strip():
            raise ValueError("Flow polling worker owner_id must not be blank")
        if len(owner_id) > 256:
            raise ValueError(
                "Flow polling worker owner_id must not exceed 256 characters"
            )
        if delivery_ttl_seconds <= 0:
            raise ValueError(
                "Flow polling worker delivery_ttl_seconds must be greater than zero"
            )
        if delivery_heartbeat_interval <= 0:
            raise ValueError(
                "Flow polling worker delivery_heartbeat_interval "
                "must be greater than zero"
            )
        if delivery_heartbeat_interval >= delivery_ttl_seconds:
            raise ValueError(
                "Flow polling worker delivery_heartbeat_interval must be "
                "less than delivery_ttl_seconds"
            )
        if retry_delay_seconds < 0:
            raise ValueError(
                "Flow polling worker retry_delay_seconds must not be negative"
            )
        if poll_interval <= 0:
            raise ValueError(
                "Flow polling worker poll_interval must be greater than zero"
            )
        self._work_source = work_source
        self._execution_runner = execution_runner
        self._handler = handler
        self._owner_id = owner_id
        self._delivery_ttl_seconds = delivery_ttl_seconds
        self._delivery_heartbeat_interval = delivery_heartbeat_interval
        self._retry_delay_seconds = retry_delay_seconds
        self._poll_interval = poll_interval
        self._stop_requested = asyncio.Event()

    def stop(self) -> None:
        """Request graceful stop after the active item, if any, settles."""
        self._stop_requested.set()

    async def run_once(self) -> bool:
        """Process at most one item; return whether any item was claimed."""
        item = await self._work_source.claim(
            owner_id=self._owner_id,
            ttl_seconds=self._delivery_ttl_seconds,
        )
        if item is None:
            return False
        assert item.delivery_token is not None
        try:
            await self._run_with_delivery_heartbeat(item)
        except FlowWorkBlockedError as exc:
            await self._work_source.block(
                item.id,
                delivery_token=item.delivery_token,
                reason_code=exc.reason_code,
            )
            return True
        except asyncio.CancelledError:
            await self._retry_after_failure(
                item,
                error_type="CancelledError",
                preserve_cancellation=True,
            )
            raise
        except Exception as exc:
            await self._retry_after_failure(
                item,
                error_type=type(exc).__name__,
                preserve_cancellation=False,
            )
            raise
        await self._work_source.complete(
            item.id,
            delivery_token=item.delivery_token,
        )
        return True

    async def run_forever(self) -> None:
        """Poll sequentially until `stop()` is requested or the task is cancelled."""
        while not self._stop_requested.is_set():
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _logger.warning(
                    "Flow work item failed and was scheduled for retry",
                    extra={"error_type": type(exc).__name__},
                )
                continue
            if not processed:
                await self._wait_for_work_or_stop()

    async def _run_with_delivery_heartbeat(self, item: FlowWorkItem) -> None:
        assert item.delivery_token is not None

        async def handle(lease: FlowExecutionLease) -> None:
            await self._handler(item, lease)

        operation: asyncio.Task[None] = asyncio.create_task(
            self._execution_runner.execute(item.command.run_id, handle),
            name=f"flow-work:{item.id}",
        )
        heartbeat: asyncio.Task[None] = asyncio.create_task(
            self._delivery_heartbeat(item),
            name=f"flow-work-heartbeat:{item.id}",
        )
        try:
            done, _ = await asyncio.wait(
                (operation, heartbeat),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat in done:
                await heartbeat
                raise RuntimeError("Flow work heartbeat stopped without an error")
            heartbeat.cancel()
            await _await_cancelled(heartbeat)
            await operation
        finally:
            operation.cancel()
            heartbeat.cancel()
            await asyncio.gather(operation, heartbeat, return_exceptions=True)

    async def _delivery_heartbeat(self, item: FlowWorkItem) -> None:
        assert item.delivery_token is not None
        while True:
            await asyncio.sleep(self._delivery_heartbeat_interval)
            item = await self._work_source.renew(
                item.id,
                delivery_token=item.delivery_token,
                ttl_seconds=self._delivery_ttl_seconds,
            )

    async def _retry_after_failure(
        self,
        item: FlowWorkItem,
        *,
        error_type: str,
        preserve_cancellation: bool,
    ) -> None:
        assert item.delivery_token is not None
        try:
            await self._work_source.retry(
                item.id,
                delivery_token=item.delivery_token,
                delay_seconds=self._retry_delay_seconds,
                error_type=error_type,
            )
        except FlowWorkDeliveryLostError:
            _logger.warning(
                "Flow work delivery was lost before retry settlement",
                extra={"work_id": str(item.id), "run_id": str(item.command.run_id)},
            )
        except Exception as exc:
            if preserve_cancellation:
                _logger.warning(
                    "Flow work retry settlement failed during cancellation",
                    extra={"error_type": type(exc).__name__},
                )
            else:
                raise

    async def _wait_for_work_or_stop(self) -> None:
        try:
            await asyncio.wait_for(
                self._stop_requested.wait(),
                timeout=self._poll_interval,
            )
        except TimeoutError:
            pass


async def _invoke_handler[T](
    handler: Callable[[FlowExecutionLease], Awaitable[T]],
    lease: FlowExecutionLease,
) -> T:
    return await handler(lease)


async def _await_cancelled(task: asyncio.Task[None]) -> None:
    try:
        await task
    except asyncio.CancelledError:
        pass
