"""SQLAlchemy Core schema for the optional PostgreSQL Store adapter."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class PostgresTables:
    metadata: MetaData
    conversations: Table
    conversation_turns: Table
    runs: Table
    events: Table
    flow_runs: Table
    flow_events: Table
    flow_leases: Table
    flow_work_items: Table
    flow_work_reviews: Table
    flow_side_effects: Table
    checkpoints: Table
    attachments: Table
    artifacts: Table


def build_tables(schema: str | None = None) -> PostgresTables:
    if schema is not None and not _IDENTIFIER.fullmatch(schema):
        raise ValueError(f"invalid PostgreSQL schema name '{schema}'")
    metadata = MetaData(schema=schema)
    conversation_target = (
        f"{schema + '.' if schema else ''}base_agent_conversations.id"
    )
    run_target = f"{schema + '.' if schema else ''}base_agent_runs.id"
    flow_run_target = f"{schema + '.' if schema else ''}base_agent_flow_runs.id"
    flow_work_target = (
        f"{schema + '.' if schema else ''}base_agent_flow_work_items.id"
    )

    conversations = Table(
        "base_agent_conversations",
        metadata,
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column("profile_id", String(128), nullable=False, index=True),
        Column("version", Integer, nullable=False),
        Column("active_run_id", UUID(as_uuid=True), nullable=True, index=True),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        Column("payload", JSONB, nullable=False),
    )
    conversation_turns = Table(
        "base_agent_conversation_turns",
        metadata,
        Column(
            "conversation_id",
            UUID(as_uuid=True),
            ForeignKey(conversation_target, ondelete="CASCADE"),
            primary_key=True,
        ),
        Column("sequence", Integer, primary_key=True),
        Column("run_id", UUID(as_uuid=True), nullable=False, unique=True),
        Column("status", String(32), nullable=False, index=True),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        Column("payload", JSONB, nullable=False),
    )

    runs = Table(
        "base_agent_runs",
        metadata,
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column("status", String(32), nullable=False, index=True),
        Column("cancel_requested", Boolean, nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        Column("payload", JSONB, nullable=False),
    )
    events = Table(
        "base_agent_events",
        metadata,
        Column(
            "run_id",
            UUID(as_uuid=True),
            ForeignKey(run_target, ondelete="CASCADE"),
            primary_key=True,
        ),
        Column("sequence", Integer, primary_key=True),
        Column("event_id", UUID(as_uuid=True), nullable=False, unique=True),
        Column("event_type", String(64), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("payload", JSONB, nullable=False),
    )
    flow_runs = Table(
        "base_agent_flow_runs",
        metadata,
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column("revision", Integer, nullable=False),
        Column("status", String(32), nullable=False, index=True),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        Column("payload", JSONB, nullable=False),
    )
    flow_events = Table(
        "base_agent_flow_events",
        metadata,
        Column(
            "run_id",
            UUID(as_uuid=True),
            ForeignKey(flow_run_target, ondelete="CASCADE"),
            primary_key=True,
        ),
        Column("sequence", Integer, primary_key=True),
        Column("event_id", UUID(as_uuid=True), nullable=False, unique=True),
        Column("event_type", String(64), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("payload", JSONB, nullable=False),
    )
    flow_leases = Table(
        "base_agent_flow_leases",
        metadata,
        Column(
            "run_id",
            UUID(as_uuid=True),
            ForeignKey(flow_run_target, ondelete="CASCADE"),
            primary_key=True,
        ),
        Column("token", UUID(as_uuid=True), nullable=False, unique=True),
        Column("owner_id", String(256), nullable=False, index=True),
        Column("attempt", Integer, nullable=False),
        Column("active", Boolean, nullable=False),
        Column("acquired_at", DateTime(timezone=True), nullable=False),
        Column("heartbeat_at", DateTime(timezone=True), nullable=False),
        Column("expires_at", DateTime(timezone=True), nullable=False, index=True),
    )
    flow_work_items = Table(
        "base_agent_flow_work_items",
        metadata,
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column(
            "run_id",
            UUID(as_uuid=True),
            ForeignKey(flow_run_target, ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        Column("idempotency_key", String(256), nullable=False, unique=True),
        Column("kind", String(32), nullable=False),
        Column("status", String(32), nullable=False, index=True),
        Column("attempt", Integer, nullable=False),
        Column("available_at", DateTime(timezone=True), nullable=False, index=True),
        Column("owner_id", String(256), nullable=True, index=True),
        Column("delivery_token", UUID(as_uuid=True), nullable=True, unique=True),
        Column("last_delivery_token", UUID(as_uuid=True), nullable=True),
        Column("claimed_at", DateTime(timezone=True), nullable=True),
        Column("lease_expires_at", DateTime(timezone=True), nullable=True, index=True),
        Column("last_error_type", String(256), nullable=True),
        Column("blocked_reason", String(256), nullable=True),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        Column("payload", JSONB, nullable=False),
    )
    flow_work_reviews = Table(
        "base_agent_flow_work_reviews",
        metadata,
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column(
            "work_id",
            UUID(as_uuid=True),
            ForeignKey(flow_work_target, ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        Column("decision", String(32), nullable=False),
        Column("reviewer_id", String(256), nullable=False, index=True),
        Column("reason_code", String(256), nullable=False),
        Column("idempotency_key", String(256), nullable=False, unique=True),
        Column("delay_seconds", Float, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("payload", JSONB, nullable=False),
    )
    flow_side_effects = Table(
        "base_agent_flow_side_effects",
        metadata,
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column(
            "run_id",
            UUID(as_uuid=True),
            ForeignKey(flow_run_target, ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        Column("invocation_id", UUID(as_uuid=True), nullable=False, index=True),
        Column("operation_key", String(256), nullable=False),
        Column("operation_name", String(128), nullable=False, index=True),
        Column("retry_mode", String(32), nullable=False),
        Column("phase", String(32), nullable=False, index=True),
        Column("revision", Integer, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        Column("payload", JSONB, nullable=False),
        UniqueConstraint(
            "run_id",
            "invocation_id",
            "operation_key",
            name="uq_base_agent_flow_side_effect_operation",
        ),
    )
    checkpoints = Table(
        "base_agent_checkpoints",
        metadata,
        Column(
            "run_id",
            UUID(as_uuid=True),
            ForeignKey(run_target, ondelete="CASCADE"),
            primary_key=True,
        ),
        Column("payload", JSONB, nullable=False),
    )
    attachments = Table(
        "base_agent_attachments",
        metadata,
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("payload", JSONB, nullable=False),
        Column("content", LargeBinary, nullable=False),
    )
    artifacts = Table(
        "base_agent_artifacts",
        metadata,
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column(
            "run_id",
            UUID(as_uuid=True),
            ForeignKey(run_target, ondelete="CASCADE"),
            nullable=False,
        ),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("payload", JSONB, nullable=False),
        Column("content", LargeBinary, nullable=False),
    )
    Index("ix_base_agent_events_run_sequence", events.c.run_id, events.c.sequence)
    Index(
        "ix_base_agent_flow_events_run_sequence",
        flow_events.c.run_id,
        flow_events.c.sequence,
    )
    Index(
        "ix_base_agent_flow_work_pending",
        flow_work_items.c.status,
        flow_work_items.c.available_at,
        flow_work_items.c.created_at,
    )
    Index(
        "ix_base_agent_conversation_turns_conversation_sequence",
        conversation_turns.c.conversation_id,
        conversation_turns.c.sequence,
    )
    Index("ix_base_agent_artifacts_run_created", artifacts.c.run_id, artifacts.c.created_at)
    return PostgresTables(
        metadata=metadata,
        conversations=conversations,
        conversation_turns=conversation_turns,
        runs=runs,
        events=events,
        flow_runs=flow_runs,
        flow_events=flow_events,
        flow_leases=flow_leases,
        flow_work_items=flow_work_items,
        flow_work_reviews=flow_work_reviews,
        flow_side_effects=flow_side_effects,
        checkpoints=checkpoints,
        attachments=attachments,
        artifacts=artifacts,
    )
