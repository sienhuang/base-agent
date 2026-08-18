import asyncio
import json
from pathlib import Path
from uuid import UUID

import httpx
from base_agent import FlowStatus
from base_agent.models import FinishReason, ModelResponse, ToolCall
from base_agent.testing import FakeModel

import mtbi_analysis.flow as flow_module
from mtbi_analysis.app import build_application
from mtbi_analysis.asgi import create_app
from mtbi_analysis.config import Settings
from mtbi_analysis.runner import CommandResult

RUN_TOKEN = "12345678-1234-5678-1234-567812345678"
SQL_PATH = f"runs/{RUN_TOKEN}/query.sql"
CSV_PATH = f"runs/{RUN_TOKEN}/dataset.csv"
REPORT_PATH = f"runs/{RUN_TOKEN}/report.md"


class FakeMTBIRunner:
    def __init__(
        self,
        *,
        create_csv: bool = True,
        empty_query_attempts: int = 0,
    ) -> None:
        self.create_csv = create_csv
        self.empty_query_attempts = empty_query_attempts
        self.onesql_calls = 0
        self.calls: list[list[str]] = []

    def readiness(self) -> tuple[bool, str]:
        return True, ""

    async def run(self, args: list[str], *, cwd: Path) -> CommandResult:
        self.calls.append(list(args))
        if args[0] == "onesql" and self.create_csv:
            self.onesql_calls += 1
            output_index = args.index("--output") + 1
            output = cwd / args[output_index]
            output.parent.mkdir(parents=True, exist_ok=True)
            content = (
                "date,active_users\n"
                if self.onesql_calls <= self.empty_query_attempts
                else "date,active_users\n2026-08-12,120\n2026-08-13,150\n"
            )
            output.write_text(content, encoding="utf-8")
        return CommandResult(returncode=0, stdout="ok", stderr="")


def _tool_response(call_id: str, name: str, arguments: dict[str, object]) -> ModelResponse:
    return ModelResponse(
        tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments),),
        finish_reason=FinishReason.TOOL_CALLS,
    )


def _json_response(payload: object) -> ModelResponse:
    return ModelResponse(content=json.dumps(payload, ensure_ascii=False))


def _successful_model() -> FakeModel:
    return FakeModel(
        [
            _tool_response(
                "query-1",
                "write_workspace_text",
                {
                    "path": SQL_PATH,
                    "content": (
                        "SELECT event_date AS date, count(DISTINCT user_id) "
                        "AS active_users FROM verified.login_events "
                        "WHERE event_date >= current_date - interval '1' day "
                        "GROUP BY event_date"
                    ),
                },
            ),
            _tool_response(
                "query-2",
                "mtbi_cli",
                {
                    "args": [
                        "onesql",
                        "SELECT event_date, count(DISTINCT user_id) FROM "
                        "verified.login_events GROUP BY event_date",
                        "--engine",
                        "PRESTO",
                        "--format",
                        "csv",
                        "--output",
                        CSV_PATH,
                    ]
                },
            ),
            _json_response(
                {
                    "success": True,
                    "selected_tables": ["verified.login_events"],
                    "confirmed_columns": ["event_date", "user_id"],
                    "summary": "Queried verified daily active-user data.",
                    "error": None,
                }
            ),
            _tool_response(
                "analysis-1",
                "summarize_csv",
                {"path": CSV_PATH},
            ),
            _tool_response(
                "analysis-2",
                "write_workspace_text",
                {
                    "path": REPORT_PATH,
                    "content": (
                        "# Active-user analysis\n\n"
                        "Daily active users increased from 120 to 150."
                    ),
                },
            ),
            _json_response(
                {
                    "summary": "Daily active users increased by 25%.",
                }
            ),
        ]
    )


async def test_mtbi_analysis_runs_two_verified_agent_stages(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(flow_module, "uuid4", lambda: UUID(RUN_TOKEN))
    model = _successful_model()
    runner = FakeMTBIRunner()
    application = build_application(
        Settings(workspace=tmp_path),
        provider=model,
        runner=runner,
    )

    result = await application.run("Analyze daily active users")

    assert result.status is FlowStatus.SUCCEEDED
    assert result.error is None
    assert result.query is not None
    assert result.analysis is not None
    assert result.dataset is not None
    assert result.dataset.row_count == 2
    assert result.dataset.columns == ("date", "active_users")
    assert result.report is not None
    assert result.report.report_path == tmp_path / REPORT_PATH
    assert result.flow.flow_run_id == RUN_TOKEN
    assert [artifact.location for artifact in result.flow.artifacts] == [
        SQL_PATH,
        CSV_PATH,
        REPORT_PATH,
    ]
    assert [stage.name for stage in result.flow.stages] == ["query", "analysis"]
    assert [stage.evidence[0].status for stage in result.flow.stages] == [
        "passed",
        "passed",
    ]
    assert [event.kind for event in result.flow.events] == [
        "flow.started",
        "stage.started",
        "stage.succeeded",
        "stage.started",
        "stage.succeeded",
        "flow.succeeded",
    ]
    assert runner.calls[:2] == [["auth", "status"], ["config", "show"]]
    assert runner.calls[2][-2:] == ["--output", CSV_PATH]
    assert len(model.requests) == 6


async def test_empty_query_result_is_repaired_with_verifier_feedback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(flow_module, "uuid4", lambda: UUID(RUN_TOKEN))
    first_sql = "SELECT user_id FROM verified.login_events WHERE event_date = '2024-03-29'"
    repaired_sql = (
        "SELECT user_id FROM verified.login_events "
        "WHERE event_date = (SELECT max(event_date) FROM verified.login_events)"
    )
    model = FakeModel(
        [
            _tool_response(
                "query-1-write",
                "write_workspace_text",
                {"path": SQL_PATH, "content": first_sql},
            ),
            _tool_response(
                "query-1-run",
                "mtbi_cli",
                {
                    "args": [
                        "onesql",
                        first_sql,
                        "--engine",
                        "PRESTO",
                        "--format",
                        "csv",
                        "--output",
                        CSV_PATH,
                    ]
                },
            ),
            _json_response(
                {
                    "success": True,
                    "selected_tables": ["verified.login_events"],
                    "confirmed_columns": ["event_date", "user_id"],
                    "summary": "Queried one candidate partition.",
                    "error": None,
                }
            ),
            _tool_response(
                "query-2-write",
                "write_workspace_text",
                {"path": SQL_PATH, "content": repaired_sql},
            ),
            _tool_response(
                "query-2-run",
                "mtbi_cli",
                {
                    "args": [
                        "onesql",
                        repaired_sql,
                        "--engine",
                        "PRESTO",
                        "--format",
                        "csv",
                        "--output",
                        CSV_PATH,
                    ]
                },
            ),
            _json_response(
                {
                    "success": True,
                    "selected_tables": ["verified.login_events"],
                    "confirmed_columns": ["event_date", "user_id"],
                    "summary": "Requeried the latest available partition.",
                    "error": None,
                }
            ),
            _tool_response("analysis-1", "summarize_csv", {"path": CSV_PATH}),
            _tool_response(
                "analysis-2",
                "write_workspace_text",
                {
                    "path": REPORT_PATH,
                    "content": "# Active-user analysis\n\nLatest partition contains users.",
                },
            ),
            _json_response({"summary": "Analyzed the latest available partition."}),
        ]
    )
    runner = FakeMTBIRunner(empty_query_attempts=1)
    application = build_application(
        Settings(workspace=tmp_path),
        provider=model,
        runner=runner,
    )

    result = await application.run("Analyze the most recent day")

    assert result.status is FlowStatus.SUCCEEDED
    assert result.dataset is not None
    assert result.dataset.row_count == 2
    assert (tmp_path / SQL_PATH).read_text(encoding="utf-8") == repaired_sql
    assert [(item.name, item.attempt) for item in result.flow.stage_attempts] == [
        ("query", 1),
        ("query", 2),
        ("analysis", 1),
    ]
    assert "CSV must contain at least one data row" in (
        model.requests[3].messages[0].content
    )
    assert "stage.retrying" in {event.kind for event in result.flow.events}


async def test_fastapi_exposes_query_and_analysis_child_run_streams(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(flow_module, "uuid4", lambda: UUID(RUN_TOKEN))
    runner = FakeMTBIRunner()
    app = create_app(
        Settings(workspace=tmp_path),
        provider=_successful_model(),
        runner=runner,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            started = await client.post(
                "/v1/flows/mtbi-analysis",
                json={"prompt": "Analyze daily active users", "stream": False},
            )
            run_route = await client.post("/v1/runs", json={"prompt": "raw query"})
            flow_run_id = started.json()["flow_run_id"]
            for _ in range(100):
                response = await client.get(f"/v1/flows/{flow_run_id}")
                if response.json()["status"] != "running":
                    break
                await asyncio.sleep(0)
            events = await client.get(f"/v1/flows/{flow_run_id}/events")
            stream = await client.get(f"/v1/flows/{flow_run_id}/events/stream")

    assert response.json()["status"] == "succeeded"
    assert flow_run_id == RUN_TOKEN
    assert run_route.status_code == 404
    assert [item["location"] for item in response.json()["artifacts"]] == [
        SQL_PATH,
        CSV_PATH,
        REPORT_PATH,
    ]
    assert runner.calls[:2] == [["auth", "status"], ["config", "show"]]
    assert [(item["role"], item["phase"]) for item in response.json()["child_runs"]] == [
        ("stage", "query"),
        ("stage", "analysis"),
    ]
    child_events = [
        event
        for event in events.json()
        if event["payload"].get("event_scope") == "child_run"
    ]
    assert {event["payload"]["stage"] for event in child_events} == {
        "query",
        "analysis",
    }
    assert "tool.started" in {event["kind"] for event in child_events}
    assert "tool.completed" in {event["kind"] for event in child_events}
    stream_text = stream.text.replace("\r\n", "\n")
    assert "event: child.run.started" in stream_text
    assert "event: model.requested" in stream_text
    assert "event: tool.completed" in stream_text
    assert '"stage":"query"' in stream_text
    assert '"stage":"analysis"' in stream_text
    assert "event: flow.succeeded" in stream_text


async def test_query_artifact_verification_blocks_analysis_stage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(flow_module, "uuid4", lambda: UUID(RUN_TOKEN))
    responses: list[ModelResponse] = []
    for attempt in range(1, 4):
        responses.extend(
            (
            _tool_response(
                f"query-{attempt}",
                "mtbi_cli",
                {"args": ["help"]},
            ),
            _json_response(
                {
                    "success": True,
                    "selected_tables": ["verified.login_events"],
                    "confirmed_columns": ["event_date", "user_id"],
                    "summary": "Claimed output without creating artifacts.",
                    "error": None,
                }
            ),
            )
        )
    model = FakeModel(responses)
    application = build_application(
        Settings(workspace=tmp_path),
        provider=model,
        runner=FakeMTBIRunner(create_csv=False),
    )

    result = await application.run("Analyze daily active users")

    assert result.status is FlowStatus.FAILED
    assert result.analysis is None
    assert result.dataset is None
    assert len(result.flow.stages) == 1
    assert result.flow.stages[0].evidence[0].status == "failed"
    assert [attempt.attempt for attempt in result.flow.stage_attempts] == [1, 2, 3]
    assert "failed verification" in (result.error or "")
    assert len(model.requests) == 6
    assert "Host verification rejected the previous attempt" in (
        model.requests[2].messages[0].content
    )
