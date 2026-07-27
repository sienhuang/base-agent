"""Optional PostgreSQL stores; install base-agent[postgres] before importing."""

from base_agent.stores.postgres.store import PostgresStore

__all__ = ["PostgresStore"]
