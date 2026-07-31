"""Read-only DataSource contract used by model-facing tools."""

from typing import Protocol, runtime_checkable

from base_agent.data_sources.models import (
    DataCatalogPage,
    DataQueryResult,
    DataTableSchema,
)


@runtime_checkable
class ReadOnlyDataSource(Protocol):
    @property
    def name(self) -> str: ...

    async def list_tables(
        self,
        *,
        catalog_name: str | None,
        schema_name: str | None,
        limit: int,
    ) -> DataCatalogPage: ...

    async def describe_table(
        self,
        table_name: str,
        *,
        catalog_name: str | None,
        schema_name: str | None,
    ) -> DataTableSchema: ...

    async def query(self, sql: str, *, limit: int) -> DataQueryResult: ...
