"""Read-only catalog/query Tools and their concrete DataSource bundle."""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import uuid4

from base_agent.data_sources.protocol import ReadOnlyDataSource
from base_agent.tools import FunctionTool, ToolContext, tool


@dataclass(frozen=True, slots=True)
class DataSourceBundle:
    """One configured read-only DataSource Tool set."""

    tools: tuple[FunctionTool, ...]
    tool_names: tuple[str, ...]
    required_permissions: frozenset[str]


def data_source_tools(
    source: ReadOnlyDataSource,
    *,
    max_tables: int = 200,
    max_rows: int = 1_000,
    max_inline_bytes: int = 64_000,
    max_artifact_bytes: int = 10_000_000,
    artifact_sample_rows: int = 20,
    timeout_seconds: float = 30.0,
) -> tuple[FunctionTool, ...]:
    """Build bounded catalog and query Tools over one trusted read-only provider."""
    if max_tables < 1 or max_tables > 1_000:
        raise ValueError("max_tables must be between 1 and 1000")
    if max_rows < 1 or max_rows > 10_000:
        raise ValueError("max_rows must be between 1 and 10000")
    if max_inline_bytes < 512:
        raise ValueError("max_inline_bytes must be at least 512")
    if max_artifact_bytes < max_inline_bytes:
        raise ValueError("max_artifact_bytes must be at least max_inline_bytes")
    if artifact_sample_rows < 0 or artifact_sample_rows > max_rows:
        raise ValueError("artifact_sample_rows must be between 0 and max_rows")
    source_name = source.name.strip()
    if not source_name or len(source_name) > 128:
        raise ValueError("DataSource name must contain between 1 and 128 characters")

    @tool(
        name="data_list_tables",
        permissions=frozenset({"data:read"}),
        timeout_seconds=timeout_seconds,
    )
    async def list_tables(
        catalog_name: str | None = None,
        schema_name: str | None = None,
        limit: int = 100,
    ) -> dict[str, object]:
        """List bounded table and view metadata from the configured read-only source."""
        if limit < 1 or limit > max_tables:
            raise ValueError(f"limit must be between 1 and {max_tables}")
        page = await source.list_tables(
            catalog_name=catalog_name,
            schema_name=schema_name,
            limit=limit,
        )
        selected = page.tables[:limit]
        return {
            "source": source_name,
            "tables": [table.model_dump(mode="json") for table in selected],
            "table_count": len(selected),
            "has_more": page.has_more or len(page.tables) > len(selected),
        }

    @tool(
        name="data_describe_table",
        permissions=frozenset({"data:read"}),
        timeout_seconds=timeout_seconds,
    )
    async def describe_table(
        table_name: str,
        catalog_name: str | None = None,
        schema_name: str | None = None,
    ) -> dict[str, object]:
        """Describe columns and primary keys for one table or view."""
        schema = await source.describe_table(
            table_name,
            catalog_name=catalog_name,
            schema_name=schema_name,
        )
        return {
            "source": source_name,
            **schema.model_dump(mode="json"),
        }

    @tool(
        name="data_query",
        permissions=frozenset({"data:read"}),
        timeout_seconds=timeout_seconds,
    )
    async def query(
        sql: str,
        context: ToolContext,
        limit: int = 200,
    ) -> dict[str, object]:
        """Execute one provider-enforced read-only query with bounded rows and Artifact overflow."""
        if limit < 1 or limit > max_rows:
            raise ValueError(f"limit must be between 1 and {max_rows}")
        if not sql.strip():
            raise ValueError("sql must not be blank")
        result = await source.query(sql, limit=limit)
        selected_rows = result.rows[:limit]
        has_more = result.has_more or len(result.rows) > len(selected_rows)
        payload: dict[str, object] = {
            "source": source_name,
            "columns": list(result.columns),
            "rows": list(selected_rows),
            "returned_row_count": len(selected_rows),
            "total_rows": result.total_rows,
            "has_more": has_more,
            "externalized": False,
        }
        serialized = _json_bytes(payload)
        if len(serialized) <= max_inline_bytes:
            return payload
        if len(serialized) > max_artifact_bytes:
            raise ValueError(
                f"bounded query result exceeds Artifact limit of {max_artifact_bytes} bytes"
            )

        artifact = await context.artifacts.create(
            name=f"data-query-{uuid4().hex[:12]}.json",
            media_type="application/json",
            content=serialized,
            metadata={
                "source": source_name,
                "returned_row_count": len(selected_rows),
                "has_more": has_more,
            },
        )
        reference: dict[str, object] = {
            "source": source_name,
            "columns": [],
            "column_count": len(result.columns),
            "columns_truncated": bool(result.columns),
            "sample": [],
            "sample_truncated": bool(selected_rows),
            "returned_row_count": len(selected_rows),
            "total_rows": result.total_rows,
            "has_more": has_more,
            "externalized": True,
            "artifact_id": str(artifact.id),
            "artifact_name": artifact.name,
            "artifact_size_bytes": artifact.size_bytes,
        }
        _append_bounded_items(
            reference,
            field="columns",
            truncated_field="columns_truncated",
            items=list(result.columns),
            limit=max_inline_bytes,
        )
        _append_bounded_items(
            reference,
            field="sample",
            truncated_field="sample_truncated",
            items=list(selected_rows[:artifact_sample_rows]),
            limit=max_inline_bytes,
            total_items=len(selected_rows),
        )
        if len(_json_bytes(reference)) > max_inline_bytes:
            raise RuntimeError("DataSource Artifact reference exceeded its configured budget")
        return reference

    return list_tables, describe_table, query


def data_source_bundle(
    source: ReadOnlyDataSource,
    *,
    max_tables: int = 200,
    max_rows: int = 1_000,
    max_inline_bytes: int = 64_000,
    max_artifact_bytes: int = 10_000_000,
    artifact_sample_rows: int = 20,
    timeout_seconds: float = 30.0,
) -> DataSourceBundle:
    """Compose read-only DataSource Tools without implicitly granting access."""
    tools = data_source_tools(
        source,
        max_tables=max_tables,
        max_rows=max_rows,
        max_inline_bytes=max_inline_bytes,
        max_artifact_bytes=max_artifact_bytes,
        artifact_sample_rows=artifact_sample_rows,
        timeout_seconds=timeout_seconds,
    )
    return DataSourceBundle(
        tools=tools,
        tool_names=tuple(candidate.definition.name for candidate in tools),
        required_permissions=frozenset().union(
            *(candidate.permissions for candidate in tools)
        ),
    )


def _json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _append_bounded_items(
    payload: dict[str, object],
    *,
    field: str,
    truncated_field: str,
    items: list[object],
    limit: int,
    total_items: int | None = None,
) -> None:
    selected: list[object] = []
    total = len(items) if total_items is None else total_items
    for item in items:
        candidate = [*selected, item]
        payload[field] = candidate
        payload[truncated_field] = len(candidate) < total
        if len(_json_bytes(payload)) > limit:
            break
        selected = candidate
    payload[field] = selected
    payload[truncated_field] = len(selected) < total
