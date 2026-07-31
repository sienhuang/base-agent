"""Read-only DataSource adapter for the local ``mtbi-cli`` command."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any

from base_agent.data_sources.models import (
    DataCatalogPage,
    DataColumn,
    DataObjectKind,
    DataQueryResult,
    DataTable,
    DataTableSchema,
)
from base_agent.providers import (
    CLIProcessRunner,
    run_cli_process,
)

_FORBIDDEN_QUERY_WORDS = frozenset(
    {
        "alter",
        "attach",
        "cache",
        "call",
        "comment",
        "copy",
        "create",
        "delete",
        "detach",
        "drop",
        "execute",
        "grant",
        "insert",
        "load",
        "merge",
        "msck",
        "optimize",
        "refresh",
        "repair",
        "replace",
        "revoke",
        "set",
        "truncate",
        "uncache",
        "unload",
        "update",
        "use",
        "vacuum",
    }
)


class MtbiEngine(StrEnum):
    """OneSQL execution engines supported by ``mtbi-cli``."""

    PRESTO = "PRESTO"
    SPARK = "SPARK"
    DORIS = "DORIS"


class MtbiCliError(RuntimeError):
    """Base failure raised by the MTBI DataSource adapter."""


class InvalidMtbiCliResponseError(MtbiCliError):
    """The CLI returned JSON that cannot enter the DataSource contract."""


class MtbiCliDataSource:
    """Use ``mtbi-cli meta`` for metadata and ``onesql`` for bounded queries."""

    def __init__(
        self,
        *,
        executable: str = "mtbi-cli",
        engine: MtbiEngine | str = MtbiEngine.PRESTO,
        region: str | None = None,
        cwd: str | Path | None = None,
        timeout_seconds: float = 300.0,
        max_output_bytes: int = 10_000_000,
        max_sql_characters: int = 100_000,
        environment: Mapping[str, str] | None = None,
        runner: CLIProcessRunner | None = None,
    ) -> None:
        normalized_executable = executable.strip()
        if not normalized_executable:
            raise ValueError("MTBI CLI executable must not be blank")
        try:
            normalized_engine = (
                engine
                if isinstance(engine, MtbiEngine)
                else MtbiEngine(engine.strip().upper())
            )
        except ValueError as exc:
            raise ValueError("MTBI engine must be PRESTO, SPARK, or DORIS") from exc
        normalized_region = region.strip() if region is not None else None
        if region is not None and not normalized_region:
            raise ValueError("MTBI region must not be blank")
        if timeout_seconds <= 0:
            raise ValueError("MTBI timeout_seconds must be greater than zero")
        if max_output_bytes < 1:
            raise ValueError("MTBI max_output_bytes must be greater than zero")
        if max_sql_characters < 1:
            raise ValueError("MTBI max_sql_characters must be greater than zero")
        resolved_cwd = Path.cwd() if cwd is None else Path(cwd)
        if not resolved_cwd.is_dir():
            raise ValueError("MTBI CLI working directory must be an existing directory")

        self._executable = normalized_executable
        self._engine = normalized_engine
        self._region = normalized_region
        self._cwd = resolved_cwd
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes
        self._max_sql_characters = max_sql_characters
        self._environment = dict(environment) if environment is not None else None
        self._runner = runner or run_cli_process

    @property
    def name(self) -> str:
        return "mtbi-cli-onesql"

    async def list_tables(
        self,
        *,
        catalog_name: str | None,
        schema_name: str | None,
        limit: int,
    ) -> DataCatalogPage:
        command = [
            "meta",
            "search",
            "--format",
            "json",
            "--page-size",
            str(limit),
        ]
        if catalog_name is not None:
            command.extend(("--db", _required_text(catalog_name, "catalog_name")))
        if schema_name is not None:
            command.extend(("--schema", _required_text(schema_name, "schema_name")))
        payload = await self._run_json(*command)
        raw_tables = _find_sequence(payload, ("list", "records", "tables"))
        if raw_tables is None:
            raise InvalidMtbiCliResponseError(
                "mtbi-cli meta search did not return a table list"
            )
        tables = tuple(
            _parse_table(
                item,
                fallback_catalog=catalog_name,
                fallback_schema=schema_name,
            )
            for item in raw_tables[:limit]
        )
        total = _find_integer(payload, ("total", "totalCount", "count"))
        return DataCatalogPage(
            tables=tables,
            has_more=(total > len(tables)) if total is not None else len(raw_tables) > limit,
        )

    async def describe_table(
        self,
        table_name: str,
        *,
        catalog_name: str | None,
        schema_name: str | None,
    ) -> DataTableSchema:
        qualified_name = _qualified_table_name(
            table_name,
            catalog_name=catalog_name,
            schema_name=schema_name,
        )
        payload = await self._run_json(
            "meta",
            "info",
            qualified_name,
            "--format",
            "json",
        )
        info = _find_mapping(payload)
        raw_columns = _find_sequence(info, ("columns",))
        if raw_columns is None:
            raise InvalidMtbiCliResponseError(
                "mtbi-cli meta info did not return a column list"
            )
        columns = tuple(_parse_column(item) for item in raw_columns)
        raw_primary_keys = _find_sequence(info, ("primaryKeys", "primary_keys")) or []
        primary_keys = tuple(_parse_key_name(item) for item in raw_primary_keys)
        return DataTableSchema(
            table=_parse_table(
                info,
                fallback_name=table_name.rsplit(".", maxsplit=1)[-1],
                fallback_catalog=catalog_name,
                fallback_schema=schema_name,
            ),
            columns=columns,
            primary_key=primary_keys,
        )

    async def query(self, sql: str, *, limit: int) -> DataQueryResult:
        bounded_sql = _bounded_select(
            sql,
            limit=limit,
            max_characters=self._max_sql_characters,
        )
        payload = await self._run_json(
            "onesql",
            "--file",
            "-",
            "--engine",
            self._engine.value,
            "--format",
            "json",
            "--no-progress",
            input_text=bounded_sql,
        )
        columns, rows = _parse_query_rows(payload)
        selected_rows = rows[:limit]
        return DataQueryResult(
            columns=columns,
            rows=tuple(selected_rows),
            has_more=len(rows) > limit,
        )

    async def _run_json(
        self,
        *arguments: str,
        input_text: str = "",
    ) -> Any:
        command = (self._executable, *arguments)
        if self._region is not None:
            command = (*command, "--region", self._region)
        output = await self._runner(
            command,
            input_text,
            self._cwd,
            self._timeout_seconds,
            self._max_output_bytes,
            self._environment,
        )
        if output.returncode != 0:
            detail = (output.stderr.strip() or output.stdout.strip())[:2_000]
            raise MtbiCliError(
                f"mtbi-cli exited with status {output.returncode}: "
                f"{detail or 'no error detail'}"
            )
        try:
            return json.loads(output.stdout)
        except json.JSONDecodeError as exc:
            raise InvalidMtbiCliResponseError(
                "mtbi-cli did not return valid JSON"
            ) from exc


def _required_text(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be blank")
    return normalized


def _qualified_table_name(
    table_name: str,
    *,
    catalog_name: str | None,
    schema_name: str | None,
) -> str:
    normalized_table = _required_text(table_name, "table_name")
    if "." in normalized_table:
        if catalog_name is not None or schema_name is not None:
            raise ValueError(
                "catalog_name/schema_name cannot accompany a qualified table_name"
            )
        return normalized_table
    parts = []
    if catalog_name is not None:
        parts.append(_required_text(catalog_name, "catalog_name"))
    if schema_name is not None:
        parts.append(_required_text(schema_name, "schema_name"))
    parts.append(normalized_table)
    return ".".join(parts)


def _parse_table(
    value: object,
    *,
    fallback_name: str | None = None,
    fallback_catalog: str | None,
    fallback_schema: str | None,
) -> DataTable:
    item = _as_mapping(value, "table")
    full_name = _optional_string(item, ("fullTableName", "full_name"))
    table_name = (
        _optional_string(item, ("tableName", "table_name", "name"))
        or fallback_name
        or (full_name.rsplit(".", maxsplit=1)[-1] if full_name else None)
    )
    if table_name is None:
        raise InvalidMtbiCliResponseError("MTBI table metadata is missing tableName")
    table_type = (
        _optional_string(item, ("tableType", "table_type", "type")) or "table"
    ).casefold()
    kind = DataObjectKind.VIEW if "view" in table_type else DataObjectKind.TABLE
    catalog = (
        _optional_string(item, ("databaseName", "catalogName", "database"))
        or fallback_catalog
    )
    schema = _optional_string(item, ("schemaName", "schema_name")) or fallback_schema
    if full_name:
        parts = full_name.split(".")
        if catalog is None and len(parts) >= 2:
            catalog = parts[0]
        if schema is None and len(parts) >= 3:
            schema = parts[-2]
    return DataTable(
        name=table_name,
        catalog_name=catalog,
        schema_name=schema,
        kind=kind,
        description=_optional_string(item, ("description", "comment")),
    )


def _parse_column(value: object) -> DataColumn:
    item = _as_mapping(value, "column")
    name = _optional_string(item, ("name", "columnName", "column_name"))
    data_type = _optional_string(item, ("type", "dataType", "data_type"))
    if name is None or data_type is None:
        raise InvalidMtbiCliResponseError(
            "MTBI column metadata requires name and type"
        )
    return DataColumn(
        name=name,
        data_type=data_type,
        nullable=_optional_boolean(item, ("nullable",), default=True),
        description=_optional_string(item, ("description", "comment")),
    )


def _parse_key_name(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    item = _as_mapping(value, "primary key")
    name = _optional_string(item, ("name", "columnName", "column_name"))
    if name is None:
        raise InvalidMtbiCliResponseError("MTBI primary key is missing its name")
    return name


def _parse_query_rows(payload: object) -> tuple[tuple[str, ...], list[dict[str, Any]]]:
    container = _find_query_container(payload)
    declared_columns = _parse_declared_columns(container.get("columns"))
    raw_rows = container.get("records", container.get("rows"))
    if raw_rows is None and isinstance(payload, list):
        raw_rows = payload
    if not isinstance(raw_rows, list):
        raise InvalidMtbiCliResponseError(
            "mtbi-cli onesql did not return a JSON row list"
        )
    if not raw_rows:
        return declared_columns, []

    if all(isinstance(row, Mapping) for row in raw_rows):
        first = _as_mapping(raw_rows[0], "query row")
        columns = declared_columns or tuple(str(key) for key in first)
        expected = set(columns)
        rows: list[dict[str, Any]] = []
        for raw_row in raw_rows:
            row = _as_mapping(raw_row, "query row")
            if set(row) != expected:
                raise InvalidMtbiCliResponseError(
                    "MTBI query rows do not share one column schema"
                )
            rows.append({column: row[column] for column in columns})
        return columns, rows

    if not declared_columns:
        raise InvalidMtbiCliResponseError(
            "array-shaped MTBI rows require declared columns"
        )
    rows = []
    for raw_row in raw_rows:
        if (
            not isinstance(raw_row, Sequence)
            or isinstance(raw_row, (str, bytes, bytearray))
            or len(raw_row) != len(declared_columns)
        ):
            raise InvalidMtbiCliResponseError(
                "array-shaped MTBI row does not match declared columns"
            )
        rows.append(dict(zip(declared_columns, raw_row, strict=True)))
    return declared_columns, rows


def _find_query_container(payload: object) -> Mapping[str, Any]:
    if isinstance(payload, list):
        return {}
    item = _as_mapping(payload, "query response")
    if "records" in item or "rows" in item:
        return item
    for key in ("data", "result"):
        nested = item.get(key)
        if isinstance(nested, Mapping):
            return _find_query_container(nested)
        if isinstance(nested, list):
            return {"records": nested}
    return item


def _parse_declared_columns(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise InvalidMtbiCliResponseError("MTBI query columns must be a list")
    columns = []
    for raw_column in value:
        if isinstance(raw_column, str):
            name = raw_column.strip()
        else:
            item = _as_mapping(raw_column, "query column")
            name = _optional_string(item, ("name", "columnName")) or ""
        if not name:
            raise InvalidMtbiCliResponseError("MTBI query column is missing its name")
        columns.append(name)
    if len(set(columns)) != len(columns):
        raise InvalidMtbiCliResponseError("MTBI query columns must be unique")
    return tuple(columns)


def _find_mapping(payload: object) -> Mapping[str, Any]:
    item = _as_mapping(payload, "response")
    for key in ("data", "result"):
        nested = item.get(key)
        if isinstance(nested, Mapping):
            return _find_mapping(nested)
    return item


def _find_sequence(
    payload: object,
    keys: tuple[str, ...],
) -> list[object] | None:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, Mapping):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    for key in ("data", "result"):
        nested = payload.get(key)
        found = _find_sequence(nested, keys)
        if found is not None:
            return found
    return None


def _find_integer(payload: object, keys: tuple[str, ...]) -> int | None:
    if not isinstance(payload, Mapping):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    for key in ("data", "result", "page"):
        found = _find_integer(payload.get(key), keys)
        if found is not None:
            return found
    return None


def _as_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidMtbiCliResponseError(f"MTBI {label} must be a JSON object")
    return value


def _optional_string(
    item: Mapping[str, Any],
    keys: tuple[str, ...],
) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _optional_boolean(
    item: Mapping[str, Any],
    keys: tuple[str, ...],
    *,
    default: bool,
) -> bool:
    for key in keys:
        value = item.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().casefold()
            if normalized in {"true", "yes", "1"}:
                return True
            if normalized in {"false", "no", "0"}:
                return False
    return default


def _bounded_select(sql: str, *, limit: int, max_characters: int) -> str:
    normalized = sql.strip()
    if not normalized:
        raise ValueError("SQL must not be blank")
    if len(normalized) > max_characters:
        raise ValueError(f"SQL exceeds {max_characters} characters")
    if "\x00" in normalized:
        raise ValueError("SQL must not contain NUL characters")
    if normalized.endswith(";"):
        normalized = normalized[:-1].rstrip()
    words = _scan_sql_words(normalized)
    if not words or words[0] not in {"select", "with"}:
        raise PermissionError("MTBI DataSource accepts SELECT/WITH queries only")
    forbidden = _FORBIDDEN_QUERY_WORDS.intersection(words)
    if forbidden:
        blocked = ", ".join(sorted(forbidden))
        raise PermissionError(f"MTBI DataSource rejected SQL keyword(s): {blocked}")
    return (
        "SELECT * FROM (\n"
        f"{normalized}\n"
        f") AS base_agent_bounded_query LIMIT {limit + 1}"
    )


def _scan_sql_words(sql: str) -> list[str]:
    words = []
    index = 0
    quote: str | None = None
    while index < len(sql):
        character = sql[index]
        if quote is not None:
            if character == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            elif character == "\\" and index + 1 < len(sql):
                index += 2
                continue
            index += 1
            continue
        if character in {"'", '"', "`"}:
            quote = character
            index += 1
            continue
        if character == ";":
            raise PermissionError("MTBI DataSource accepts one SQL statement only")
        if (
            (character == "-" and index + 1 < len(sql) and sql[index + 1] == "-")
            or (character == "/" and index + 1 < len(sql) and sql[index + 1] == "*")
        ):
            raise PermissionError("SQL comments are not accepted by MTBI DataSource")
        if character.isalpha() or character == "_":
            end = index + 1
            while end < len(sql) and (
                sql[end].isalnum() or sql[end] in {"_", "$"}
            ):
                end += 1
            words.append(sql[index:end].casefold())
            index = end
            continue
        index += 1
    if quote is not None:
        raise ValueError("SQL contains an unterminated quoted value")
    return words
