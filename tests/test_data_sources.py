import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from base_agent import (
    Agent,
    AgentProfile,
    AgentResultStatus,
    InMemoryArtifactStore,
    ModelResponse,
    ToolCall,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
)
from base_agent.data_sources import (
    DataCatalogPage,
    DataColumn,
    DataQueryResult,
    DataTable,
    DataTableSchema,
    MtbiCliDataSource,
    ReadOnlyDataSource,
    data_source_bundle,
)
from base_agent.providers import CLIProcessOutput
from base_agent.testing import FakeModel


class FakeReadOnlyDataSource:
    name = "warehouse"

    async def list_tables(
        self,
        *,
        catalog_name: str | None,
        schema_name: str | None,
        limit: int,
    ) -> DataCatalogPage:
        del catalog_name, schema_name, limit
        return DataCatalogPage(tables=(DataTable(name="daily_metrics"),))

    async def describe_table(
        self,
        table_name: str,
        *,
        catalog_name: str | None,
        schema_name: str | None,
    ) -> DataTableSchema:
        del catalog_name, schema_name
        return DataTableSchema(
            table=DataTable(name=table_name),
            columns=(DataColumn(name="value", data_type="BIGINT"),),
        )

    async def query(self, sql: str, *, limit: int) -> DataQueryResult:
        del sql, limit
        return DataQueryResult(
            columns=("metric", "value"),
            rows=(
                {"metric": "daily_active_users", "value": "x" * 200},
                {"metric": "revenue", "value": "y" * 200},
            ),
        )


class FakeMtbiRunner:
    def __init__(self, outputs: list[CLIProcessOutput]) -> None:
        self._outputs = outputs
        self.calls: list[
            tuple[
                tuple[str, ...],
                str,
                Path,
                float,
                int,
                Mapping[str, str] | None,
            ]
        ] = []

    async def __call__(
        self,
        command: tuple[str, ...],
        input_text: str,
        cwd: Path,
        timeout_seconds: float,
        max_output_bytes: int,
        environment: Mapping[str, str] | None,
    ) -> CLIProcessOutput:
        self.calls.append(
            (
                command,
                input_text,
                cwd,
                timeout_seconds,
                max_output_bytes,
                environment,
            )
        )
        return self._outputs.pop(0)


@pytest.mark.asyncio
async def test_data_source_bundle_requires_permission_and_externalizes_large_results() -> None:
    source = FakeReadOnlyDataSource()
    bundle = data_source_bundle(
        source,
        max_inline_bytes=512,
        artifact_sample_rows=1,
    )
    artifact_store = InMemoryArtifactStore()
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="query-1",
                        name="data_query",
                        arguments={"sql": "SELECT metric, value FROM metrics", "limit": 10},
                    ),
                )
            ),
            ModelResponse(content="Query completed."),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="data-source-bundle",
            instructions="Query the read-only warehouse.",
            tools=bundle.tool_names,
            permissions=bundle.required_permissions,
        ),
        model=model,
        tools=bundle.tools,
        artifact_store=artifact_store,
    )

    result = await agent.run("Query the metrics.")
    tool_message = next(message for message in result.messages if message.tool_call_id)
    tool_result = ToolResult.model_validate_json(tool_message.content)
    artifact = result.artifacts[0]
    stored_payload = json.loads(await agent.read_content(artifact.id))

    assert isinstance(source, ReadOnlyDataSource)
    assert result.status is AgentResultStatus.COMPLETED
    assert tool_result.data["externalized"] is True
    assert tool_result.data["artifact_id"] == str(artifact.id)
    assert len(tool_result.data["sample"]) <= 1
    assert len(json.dumps(tool_result.data).encode()) <= 512
    assert len(stored_payload["rows"]) == 2
    assert bundle.required_permissions == frozenset({"data:read"})

    catalog_executor = ToolExecutor(ToolRegistry(bundle.tools))
    denied = await catalog_executor.execute(
        ToolCall(id="list-1", name="data_list_tables", arguments={})
    )
    assert denied.error_code == "permission_denied"


@pytest.mark.asyncio
async def test_mtbi_cli_data_source_uses_meta_and_bounded_onesql(
    tmp_path: Path,
) -> None:
    runner = FakeMtbiRunner(
        [
            CLIProcessOutput(
                returncode=0,
                stdout=json.dumps(
                    {
                        "data": {
                            "list": [
                                {
                                    "tableName": "daily_metrics",
                                    "databaseName": "warehouse",
                                    "schemaName": "public",
                                    "tableType": "TABLE",
                                }
                            ],
                            "total": 1,
                        }
                    }
                ),
                stderr="",
            ),
            CLIProcessOutput(
                returncode=0,
                stdout=json.dumps(
                    {
                        "data": {
                            "tableName": "daily_metrics",
                            "databaseName": "warehouse",
                            "schemaName": "public",
                            "tableType": "TABLE",
                            "columns": [
                                {
                                    "name": "metric",
                                    "type": "VARCHAR",
                                    "nullable": False,
                                },
                                {
                                    "name": "value",
                                    "type": "BIGINT",
                                    "nullable": True,
                                },
                            ],
                            "primaryKeys": ["metric"],
                        }
                    }
                ),
                stderr="",
            ),
            CLIProcessOutput(
                returncode=0,
                stdout=json.dumps(
                    [
                        {"metric": "dau", "value": 100},
                        {"metric": "revenue", "value": 42},
                        {"metric": "retention", "value": 50},
                    ]
                ),
                stderr="",
            ),
        ]
    )
    source = MtbiCliDataSource(
        engine="SPARK",
        region="volc_cn",
        cwd=tmp_path,
        runner=runner,
    )

    catalog = await source.list_tables(
        catalog_name="warehouse",
        schema_name="public",
        limit=10,
    )
    schema = await source.describe_table(
        "daily_metrics",
        catalog_name="warehouse",
        schema_name="public",
    )
    result = await source.query(
        "SELECT metric, value FROM warehouse.public.daily_metrics ORDER BY metric",
        limit=2,
    )

    assert catalog.tables[0].name == "daily_metrics"
    assert [column.name for column in schema.columns] == ["metric", "value"]
    assert schema.primary_key == ("metric",)
    assert result.rows == (
        {"metric": "dau", "value": 100},
        {"metric": "revenue", "value": 42},
    )
    assert result.has_more is True
    query_command, query_input, *_ = runner.calls[2]
    assert query_command[:2] == ("mtbi-cli", "onesql")
    assert "--engine" in query_command
    assert "SPARK" in query_command
    assert query_command[-2:] == ("--region", "volc_cn")
    assert "daily_metrics" not in query_command
    assert "daily_metrics" in query_input
    assert query_input.endswith("LIMIT 3")

    with pytest.raises(PermissionError, match="SELECT/WITH"):
        await source.query("DELETE FROM metrics", limit=10)
    assert len(runner.calls) == 3
