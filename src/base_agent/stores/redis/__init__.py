"""Optional Redis event notifications; install base-agent[redis] before importing."""

from base_agent.stores.redis.store import RedisEventStore

__all__ = ["RedisEventStore"]
