from __future__ import annotations

import os

from agent_app import config
from agent_app.config import Settings

_CONFIG_NAMES = (
    "AGENT_PROVIDER",
    "AGENT_MODEL",
    "PLAN_REACT_PLANNER_MODEL",
    "PLAN_REACT_EXECUTOR_MODEL",
    "PLAN_REACT_PLANNER_API_KEY",
    "PLAN_REACT_EXECUTOR_API_KEY",
    "PLAN_REACT_PLANNER_BASE_URL",
    "PLAN_REACT_EXECUTOR_BASE_URL",
    "PLAN_REACT_PLANNER_STRUCTURED_OUTPUT_MODE",
    "PLAN_REACT_EXECUTOR_STRUCTURED_OUTPUT_MODE",
    "PLAN_REACT_PLANNER_CONTEXT_WINDOW_TOKENS",
    "PLAN_REACT_PLANNER_CONTEXT_RESERVED_OUTPUT_TOKENS",
    "PLAN_REACT_PLANNER_CONTEXT_SAFETY_MARGIN_TOKENS",
    "PLAN_REACT_EXECUTOR_CONTEXT_WINDOW_TOKENS",
    "PLAN_REACT_EXECUTOR_CONTEXT_RESERVED_OUTPUT_TOKENS",
    "PLAN_REACT_EXECUTOR_CONTEXT_SAFETY_MARGIN_TOKENS",
    "PLAN_REACT_EXECUTOR_CONTEXT_MAX_TOOL_GROUPS",
    "AGENT_STRUCTURED_OUTPUT_MODE",
    "AGENT_CLI_EXECUTABLE",
    "AGENT_CLI_TIMEOUT_SECONDS",
    "AGENT_CLI_MAX_OUTPUT_BYTES",
    "AGENT_ARTIFACT_ROOT",
    "AGENT_EVENT_STORE",
    "AGENT_POSTGRES_DSN",
    "AGENT_POSTGRES_SCHEMA",
    "AGENT_EVENT_POLL_INTERVAL_SECONDS",
    "AGENT_RUN_LEASE_SECONDS",
    "AGENT_RUN_HEARTBEAT_SECONDS",
    "AGENT_RUNTIME_BUILD_ID",
    "AGENT_EXECUTION_MODE",
    "AGENT_RUNTIME_ROUTE",
    "AGENT_WORKER_POLL_SECONDS",
    "AGENT_WORKER_RECONCILE_SECONDS",
    "AGENT_WORKER_MAX_CONCURRENCY",
    "AGENT_OPERATOR_TOKEN",
    "AGENT_MCP_CONFIG_PATH",
    "AGENT_MCP_STATE_ROOT",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
)


def test_settings_read_starter_dotenv_without_mutating_process_environment(
    monkeypatch,
    tmp_path,
) -> None:
    for name in _CONFIG_NAMES:
        monkeypatch.delenv(name, raising=False)
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "\n".join(
            (
                "AGENT_PROVIDER=openai-compatible",
                "AGENT_MODEL=dotenv-model",
                "PLAN_REACT_PLANNER_MODEL=dotenv-planner-model",
                "PLAN_REACT_EXECUTOR_MODEL=dotenv-executor-model",
                "PLAN_REACT_PLANNER_API_KEY=dotenv-planner-key",
                "PLAN_REACT_EXECUTOR_API_KEY=dotenv-executor-key",
                "PLAN_REACT_PLANNER_BASE_URL=http://planner.local/v1",
                "PLAN_REACT_EXECUTOR_BASE_URL=http://executor.local/v1",
                "PLAN_REACT_PLANNER_STRUCTURED_OUTPUT_MODE=json-schema",
                "PLAN_REACT_EXECUTOR_STRUCTURED_OUTPUT_MODE=prompt",
                "PLAN_REACT_PLANNER_CONTEXT_WINDOW_TOKENS=32768",
                "PLAN_REACT_PLANNER_CONTEXT_RESERVED_OUTPUT_TOKENS=4096",
                "PLAN_REACT_PLANNER_CONTEXT_SAFETY_MARGIN_TOKENS=512",
                "PLAN_REACT_EXECUTOR_CONTEXT_WINDOW_TOKENS=65536",
                "PLAN_REACT_EXECUTOR_CONTEXT_RESERVED_OUTPUT_TOKENS=8192",
                "PLAN_REACT_EXECUTOR_CONTEXT_SAFETY_MARGIN_TOKENS=1024",
                "PLAN_REACT_EXECUTOR_CONTEXT_MAX_TOOL_GROUPS=2",
                "OPENAI_API_KEY=dotenv-key",
                "OPENAI_BASE_URL=http://localhost:8000/v1",
                "AGENT_STRUCTURED_OUTPUT_MODE=json-object",
                f"AGENT_ARTIFACT_ROOT={tmp_path / 'artifacts'}",
                "AGENT_EVENT_STORE=postgres",
                "AGENT_POSTGRES_DSN=postgresql://agent:password@db/agent",
                "AGENT_POSTGRES_SCHEMA=starter_events",
                "AGENT_EVENT_POLL_INTERVAL_SECONDS=0.25",
                "AGENT_RUN_LEASE_SECONDS=45",
                "AGENT_RUN_HEARTBEAT_SECONDS=15",
                "AGENT_RUNTIME_BUILD_ID=starter-test-build",
                "AGENT_WORKER_MAX_CONCURRENCY=7",
                "AGENT_MCP_CONFIG_PATH=config/mcp.yaml",
                "AGENT_MCP_STATE_ROOT=.mcp-test",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_DOTENV_PATH", dotenv_path)

    settings = Settings.from_env()

    assert settings.provider == "openai-compatible"
    assert settings.model == "dotenv-model"
    assert settings.plan_react_planner_model == "dotenv-planner-model"
    assert settings.plan_react_executor_model == "dotenv-executor-model"
    planner = settings.for_plan_react_role("planner")
    executor = settings.for_plan_react_role("executor")
    assert planner.api_key == "dotenv-planner-key"
    assert planner.base_url == "http://planner.local/v1"
    assert planner.structured_output_mode == "json-schema"
    assert executor.api_key == "dotenv-executor-key"
    assert executor.base_url == "http://executor.local/v1"
    assert executor.structured_output_mode == "prompt"
    assert settings.plan_react_planner_context_window_tokens == 32_768
    assert settings.plan_react_planner_context_reserved_output_tokens == 4_096
    assert settings.plan_react_planner_context_safety_margin_tokens == 512
    assert settings.plan_react_executor_context_window_tokens == 65_536
    assert settings.plan_react_executor_context_reserved_output_tokens == 8_192
    assert settings.plan_react_executor_context_safety_margin_tokens == 1_024
    assert settings.plan_react_executor_context_max_tool_groups == 2
    assert settings.api_key == "dotenv-key"
    assert settings.base_url == "http://localhost:8000/v1"
    assert settings.structured_output_mode == "json-object"
    assert settings.artifact_root == (tmp_path / "artifacts").resolve()
    assert settings.event_store == "postgres"
    assert settings.postgres_dsn == "postgresql://agent:password@db/agent"
    assert settings.postgres_schema == "starter_events"
    assert settings.event_poll_interval_seconds == 0.25
    assert settings.run_lease_seconds == 45
    assert settings.run_heartbeat_interval_seconds == 15
    assert settings.runtime_build_id == "starter-test-build"
    assert settings.worker_max_concurrency == 7
    assert settings.mcp_config_path == (config._APPLICATION_ROOT / "config/mcp.yaml").resolve()
    assert settings.mcp_state_root == (config._APPLICATION_ROOT / ".mcp-test").resolve()
    assert os.getenv("AGENT_PROVIDER") is None


def test_process_environment_overrides_starter_dotenv(
    monkeypatch,
    tmp_path,
) -> None:
    for name in _CONFIG_NAMES:
        monkeypatch.delenv(name, raising=False)
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "AGENT_PROVIDER=offline\nAGENT_MODEL=dotenv-model\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_DOTENV_PATH", dotenv_path)
    monkeypatch.setenv("AGENT_PROVIDER", "codex-cli")
    monkeypatch.setenv("AGENT_MODEL", "process-model")

    settings = Settings.from_env()

    assert settings.provider == "codex-cli"
    assert settings.model == "process-model"
    assert settings.plan_react_planner_model is None
    assert settings.plan_react_executor_model is None
    assert settings.structured_output_mode == "prompt"


def test_process_environment_overrides_plan_react_role_models(
    monkeypatch,
    tmp_path,
) -> None:
    for name in _CONFIG_NAMES:
        monkeypatch.delenv(name, raising=False)
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "\n".join(
            (
                "PLAN_REACT_PLANNER_MODEL=dotenv-planner",
                "PLAN_REACT_EXECUTOR_MODEL=dotenv-executor",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_DOTENV_PATH", dotenv_path)
    monkeypatch.setenv("PLAN_REACT_PLANNER_MODEL", "process-planner")
    monkeypatch.setenv("PLAN_REACT_EXECUTOR_MODEL", "process-executor")

    settings = Settings.from_env()

    assert settings.plan_react_planner_model == "process-planner"
    assert settings.plan_react_executor_model == "process-executor"


def test_settings_reject_unknown_structured_output_mode(
    monkeypatch,
    tmp_path,
) -> None:
    for name in _CONFIG_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "_DOTENV_PATH", tmp_path / ".env")
    monkeypatch.setenv("AGENT_STRUCTURED_OUTPUT_MODE", "xml")

    try:
        Settings.from_env()
    except ValueError as exc:
        assert str(exc) == (
            "AGENT_STRUCTURED_OUTPUT_MODE must be json-schema, json-object, or prompt"
        )
    else:
        raise AssertionError("unknown structured output mode must fail")


def test_settings_reject_unknown_plan_react_structured_output_mode(
    monkeypatch,
    tmp_path,
) -> None:
    for name in _CONFIG_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "_DOTENV_PATH", tmp_path / ".env")
    monkeypatch.setenv("PLAN_REACT_PLANNER_STRUCTURED_OUTPUT_MODE", "xml")

    try:
        Settings.from_env()
    except ValueError as exc:
        assert str(exc) == (
            "PLAN_REACT_PLANNER_STRUCTURED_OUTPUT_MODE must be "
            "json-schema, json-object, or prompt"
        )
    else:
        raise AssertionError("unknown Planner structured output mode must fail")


def test_settings_default_to_process_local_event_store(
    monkeypatch,
    tmp_path,
) -> None:
    for name in _CONFIG_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "_DOTENV_PATH", tmp_path / ".env")

    settings = Settings.from_env()

    assert settings.event_store == "memory"
    assert settings.postgres_dsn is None
    assert settings.postgres_schema == "base_agent"
    assert settings.event_poll_interval_seconds == 0.1
    assert settings.run_lease_seconds == 30
    assert settings.run_heartbeat_interval_seconds == 10
    assert settings.runtime_build_id is None
    assert settings.execution_mode == "local"
    assert settings.runtime_route == "default"
    assert settings.worker_max_concurrency == 10
    assert settings.operator_token is None
    assert settings.plan_react_planner_context_window_tokens is None
    assert settings.plan_react_executor_context_window_tokens is None
    assert settings.plan_react_executor_context_max_tool_groups == 4


def test_settings_reject_invalid_plan_react_context_configuration(
    monkeypatch,
    tmp_path,
) -> None:
    for name in _CONFIG_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "_DOTENV_PATH", tmp_path / ".env")
    monkeypatch.setenv("PLAN_REACT_EXECUTOR_CONTEXT_WINDOW_TOKENS", "4096")
    monkeypatch.setenv("PLAN_REACT_EXECUTOR_CONTEXT_RESERVED_OUTPUT_TOKENS", "4096")

    try:
        Settings.from_env()
    except ValueError as exc:
        assert str(exc) == (
            "invalid Plan-ReAct Executor context budget: reserved_output_tokens plus "
            "safety_margin_tokens must leave input capacity"
        )
    else:
        raise AssertionError("invalid Executor context budget must fail")


def test_postgres_worker_settings_can_be_used_by_a_non_api_process(
    monkeypatch,
    tmp_path,
) -> None:
    for name in _CONFIG_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "_DOTENV_PATH", tmp_path / ".env")
    monkeypatch.setenv("AGENT_EVENT_STORE", "postgres")
    monkeypatch.setenv(
        "AGENT_POSTGRES_DSN",
        "postgresql://agent:password@db/agent",
    )
    monkeypatch.setenv("AGENT_EXECUTION_MODE", "postgres-worker")

    settings = Settings.from_env()

    assert settings.execution_mode == "postgres-worker"
    assert settings.operator_token is None


def test_settings_require_postgres_dsn(
    monkeypatch,
    tmp_path,
) -> None:
    for name in _CONFIG_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "_DOTENV_PATH", tmp_path / ".env")
    monkeypatch.setenv("AGENT_EVENT_STORE", "postgres")

    try:
        Settings.from_env()
    except ValueError as exc:
        assert str(exc) == (
            "AGENT_POSTGRES_DSN is required when AGENT_EVENT_STORE=postgres"
        )
    else:
        raise AssertionError("PostgreSQL EventStore without a DSN must fail")


def test_settings_reject_unknown_event_store(
    monkeypatch,
    tmp_path,
) -> None:
    for name in _CONFIG_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "_DOTENV_PATH", tmp_path / ".env")
    monkeypatch.setenv("AGENT_EVENT_STORE", "sqlite")

    try:
        Settings.from_env()
    except ValueError as exc:
        assert str(exc) == "AGENT_EVENT_STORE must be memory or postgres"
    else:
        raise AssertionError("unknown EventStore must fail")


def test_settings_reject_invalid_event_poll_interval(
    monkeypatch,
    tmp_path,
) -> None:
    for name in _CONFIG_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "_DOTENV_PATH", tmp_path / ".env")
    monkeypatch.setenv("AGENT_EVENT_POLL_INTERVAL_SECONDS", "0")

    try:
        Settings.from_env()
    except ValueError as exc:
        assert str(exc) == (
            "AGENT_EVENT_POLL_INTERVAL_SECONDS must be greater than zero"
        )
    else:
        raise AssertionError("non-positive EventStore poll interval must fail")


def test_settings_reject_run_heartbeat_not_shorter_than_lease(
    monkeypatch,
    tmp_path,
) -> None:
    for name in _CONFIG_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "_DOTENV_PATH", tmp_path / ".env")
    monkeypatch.setenv("AGENT_RUN_LEASE_SECONDS", "10")
    monkeypatch.setenv("AGENT_RUN_HEARTBEAT_SECONDS", "10")

    try:
        Settings.from_env()
    except ValueError as exc:
        assert str(exc) == (
            "AGENT_RUN_HEARTBEAT_SECONDS must be positive and shorter than "
            "AGENT_RUN_LEASE_SECONDS"
        )
    else:
        raise AssertionError("Run heartbeat must be shorter than its lease")


def test_settings_reject_non_positive_worker_concurrency(
    monkeypatch,
    tmp_path,
) -> None:
    for name in _CONFIG_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "_DOTENV_PATH", tmp_path / ".env")
    monkeypatch.setenv("AGENT_WORKER_MAX_CONCURRENCY", "0")

    try:
        Settings.from_env()
    except ValueError as exc:
        assert str(exc) == (
            "AGENT_WORKER_MAX_CONCURRENCY must be greater than zero"
        )
    else:
        raise AssertionError("Worker concurrency must be positive")
