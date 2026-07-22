"""Redis notifications layered over a durable EventStore."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any, Self
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from base_agent.models import EventType, RuntimeEvent
from base_agent.stores import EventStore

logger = logging.getLogger(__name__)

_STREAM_BOUNDARIES = {
    EventType.RUN_COMPLETED,
    EventType.RUN_FAILED,
    EventType.RUN_CANCELLED,
    EventType.RUN_LIMIT_REACHED,
    EventType.RUN_WAITING,
}
_PERMANENT_BOUNDARIES = _STREAM_BOUNDARIES - {EventType.RUN_WAITING}


class RedisEventStore:
    """Add low-latency Redis Pub/Sub notifications to a durable EventStore.

    Redis is deliberately notification-only. Events are written to and replayed from
    ``durable_store`` so an offline subscriber cannot lose Run history.
    """

    def __init__(
        self,
        durable_store: EventStore,
        client: Redis,
        *,
        channel_prefix: str = "base-agent:events",
        poll_interval: float = 1.0,
    ) -> None:
        if not channel_prefix or channel_prefix.isspace():
            raise ValueError("channel_prefix must not be blank")
        if any(character.isspace() for character in channel_prefix):
            raise ValueError("channel_prefix must not contain whitespace")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be greater than zero")
        self.durable_store = durable_store
        self.client = client
        self.channel_prefix = channel_prefix
        self.poll_interval = poll_interval
        self._owns_client = False

    @classmethod
    def from_url(
        cls,
        url: str,
        durable_store: EventStore,
        *,
        channel_prefix: str = "base-agent:events",
        poll_interval: float = 1.0,
        **client_options: Any,
    ) -> Self:
        client: Redis = Redis.from_url(
            url,
            decode_responses=True,
            **client_options,
        )
        store = cls(
            durable_store,
            client,
            channel_prefix=channel_prefix,
            poll_interval=poll_interval,
        )
        store._owns_client = True
        return store

    async def close(self) -> None:
        """Close a client created by ``from_url``; the durable store is not owned."""

        if self._owns_client:
            await self.client.aclose()

    async def emit(
        self,
        run_id: UUID,
        event_type: EventType,
        data: dict[str, Any] | None = None,
    ) -> RuntimeEvent:
        event = await self.durable_store.emit(run_id, event_type, data)
        try:
            await self.client.publish(self._channel(run_id), str(event.sequence))
        except RedisError:
            logger.warning(
                "Redis event notification failed; event remains durable",
                exc_info=True,
                extra={"run_id": str(run_id), "sequence": event.sequence},
            )
        return event

    async def list(self, run_id: UUID) -> tuple[RuntimeEvent, ...]:
        return await self.durable_store.list(run_id)

    async def subscribe(
        self,
        run_id: UUID,
        *,
        after_sequence: int = 0,
    ) -> AsyncIterator[RuntimeEvent]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        next_sequence = after_sequence + 1
        redis_unavailable = False

        while True:
            events = await self.durable_store.list(run_id)
            batch = tuple(event for event in events if event.sequence >= next_sequence)
            for event in batch:
                yield event
                next_sequence = event.sequence + 1
                if event.type in _STREAM_BOUNDARIES:
                    return
            if any(event.type in _PERMANENT_BOUNDARIES for event in events):
                return

            try:
                async with self.client.pubsub() as pubsub:
                    await pubsub.subscribe(self._channel(run_id))
                    if redis_unavailable:
                        logger.info(
                            "Redis event subscription recovered",
                            extra={"run_id": str(run_id)},
                        )
                        redis_unavailable = False
                    while True:
                        events = await self.durable_store.list(run_id)
                        batch = tuple(
                            event for event in events if event.sequence >= next_sequence
                        )
                        for event in batch:
                            yield event
                            next_sequence = event.sequence + 1
                            if event.type in _STREAM_BOUNDARIES:
                                return
                        if any(event.type in _PERMANENT_BOUNDARIES for event in events):
                            return
                        await pubsub.get_message(
                            ignore_subscribe_messages=True,
                            timeout=self.poll_interval,
                        )
            except RedisError:
                if not redis_unavailable:
                    logger.warning(
                        "Redis event subscription unavailable; polling durable store",
                        exc_info=True,
                        extra={"run_id": str(run_id)},
                    )
                    redis_unavailable = True
                await asyncio.sleep(self.poll_interval)

    def _channel(self, run_id: UUID) -> str:
        return f"{self.channel_prefix}:{run_id}"
