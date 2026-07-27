"""SQLAlchemy Core schema for the optional PostgreSQL stores."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
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
        checkpoints=checkpoints,
        attachments=attachments,
        artifacts=artifacts,
    )
