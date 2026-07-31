"""Provider-neutral catalog and read-only query values."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DataObjectKind(StrEnum):
    """Portable catalog object kinds exposed to the model."""

    TABLE = "table"
    VIEW = "view"


class DataTable(BaseModel):
    """One discoverable table or view."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=512)
    schema_name: str | None = Field(default=None, max_length=512)
    catalog_name: str | None = Field(default=None, max_length=512)
    kind: DataObjectKind = DataObjectKind.TABLE
    description: str | None = Field(default=None, max_length=4_000)


class DataColumn(BaseModel):
    """One column in a table schema."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=512)
    data_type: str = Field(min_length=1, max_length=512)
    nullable: bool = True
    description: str | None = Field(default=None, max_length=4_000)


class DataTableSchema(BaseModel):
    """Bounded schema metadata for one table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    table: DataTable
    columns: tuple[DataColumn, ...] = Field(default_factory=tuple, max_length=2_000)
    primary_key: tuple[str, ...] = Field(default_factory=tuple, max_length=128)


class DataCatalogPage(BaseModel):
    """One bounded page of table metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tables: tuple[DataTable, ...] = Field(default_factory=tuple, max_length=1_000)
    has_more: bool = False


class DataQueryResult(BaseModel):
    """A provider-bounded tabular query result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    columns: tuple[str, ...] = Field(default_factory=tuple, max_length=2_000)
    rows: tuple[dict[str, Any], ...] = Field(default_factory=tuple, max_length=10_001)
    total_rows: int | None = Field(default=None, ge=0)
    has_more: bool = False

    @model_validator(mode="after")
    def validate_rows(self) -> Self:
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("query result column names must be unique")
        expected = set(self.columns)
        for row in self.rows:
            if set(row) != expected:
                raise ValueError("every query result row must match the declared columns")
        if self.total_rows is not None and self.total_rows < len(self.rows):
            raise ValueError("total_rows cannot be smaller than returned rows")
        return self
