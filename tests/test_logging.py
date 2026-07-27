import json
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from base_agent import (
    Agent,
    AgentProfile,
    ModelResponse,
    TokenUsage,
    ToolCall,
    tool,
)
from base_agent.server import create_app
from base_agent.testing import FakeModel


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


@tool
async def count_items(value: str) -> int:
    """Count whitespace-separated items."""
    return len(value.split())


@pytest.mark.asyncio
async def test_agent_automatically_writes_correlated_redacted_file_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_file = tmp_path / "agent.jsonl"
    monkeypatch.setenv("BASE_AGENT_LOG_FILE", str(log_file))
    secret_prompt = "never persist AKIAABCDEFGHIJKLMNOP or sk-super-secret-value"
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="count-1",
                        name="count_items",
                        arguments={"value": secret_prompt},
                    ),
                ),
                usage=TokenUsage(input_tokens=11, output_tokens=2),
            ),
            ModelResponse(
                content=f"finished {secret_prompt}",
                usage=TokenUsage(input_tokens=17, output_tokens=3),
            ),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="logged-agent",
            instructions="Work safely.",
            tools=("count_items",),
        ),
        model=model,
        tools=(count_items,),
    )
    daily_handler = next(
        handler
        for handler in logging.getLogger("base_agent").handlers
        if isinstance(handler, TimedRotatingFileHandler)
    )
    conversation = await agent.create_conversation()

    result = await agent.run(secret_prompt, conversation_id=conversation.id)

    records = _records(log_file)
    events = {record["event"] for record in records}
    run_id = result.metadata["run_id"]
    run_records = [record for record in records if record["run_id"] == run_id]

    assert {
        "agent.initialized",
        "conversation.created",
        "conversation.turn.started",
        "run.requested",
        "runtime.execution.started",
        "model.request.started",
        "model.request.completed",
        "tool.execution.started",
        "tool.execution.finished",
        "runtime.execution.finished",
        "run.finished",
    } <= events
    assert run_records
    assert all(record["conversation_id"] == str(conversation.id) for record in run_records)
    assert any(record["turn_sequence"] == 1 for record in run_records)
    model_records = [
        record
        for record in run_records
        if record["event"] == "model.request.completed"
    ]
    runtime_record = next(
        record
        for record in run_records
        if record["event"] == "runtime.execution.finished"
    )
    run_record = next(
        record for record in run_records if record["event"] == "run.finished"
    )
    assert [
        (
            record["input_tokens"],
            record["output_tokens"],
            record["total_tokens"],
        )
        for record in model_records
    ] == [(11, 2, 13), (17, 3, 20)]
    for summary in (runtime_record, run_record):
        assert summary["model_call_count"] == 2
        assert summary["input_tokens"] == 28
        assert summary["output_tokens"] == 5
        assert summary["total_tokens"] == 33
    assert result.usage == TokenUsage(input_tokens=28, output_tokens=5)
    assert result.metadata["model_calls"] == 2
    assert daily_handler.when == "MIDNIGHT"
    assert daily_handler.backupCount == 30
    assert daily_handler.utc is False
    serialized = log_file.read_text()
    assert secret_prompt not in serialized
    assert "AKIAABCDEFGHIJKLMNOP" not in serialized
    assert "sk-super-secret-value" not in serialized


@pytest.mark.asyncio
async def test_http_logs_include_validated_request_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_file = tmp_path / "http.jsonl"
    monkeypatch.setenv("BASE_AGENT_LOG_FILE", str(log_file))
    agent = Agent(
        profile=AgentProfile(id="http-logged-agent", instructions="Work."),
        model=FakeModel([ModelResponse(content="done")]),
    )
    app = create_app(agent)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/v1/runs",
                json={"prompt": "work"},
                headers={"X-Request-ID": "request-123"},
            )
            run_id = UUID(response.json()["run_id"])
            await agent.get_run(run_id)

    request_records = [
        record
        for record in _records(log_file)
        if record.get("event") == "http.request.completed"
    ]
    assert response.headers["X-Request-ID"] == "request-123"
    assert request_records[-1]["request_id"] == "request-123"
    assert request_records[-1]["method"] == "POST"
    assert request_records[-1]["path"] == "/v1/runs"
