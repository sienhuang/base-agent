from pathlib import Path

from base_agent.providers import CodexCLIProvider, OpenAIChatProvider

import mtbi_analysis.config as config_module
from mtbi_analysis.config import Settings
from mtbi_analysis.provider import build_provider


def test_settings_default_to_codex_cli_without_api_credentials(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(config_module, "_DOTENV_PATH", tmp_path / "missing.env")
    for name in (
        "AGENT_PROVIDER",
        "AGENT_MODEL",
        "AGENT_CLI_EXECUTABLE",
        "AGENT_CLI_TIMEOUT_SECONDS",
        "AGENT_CLI_MAX_OUTPUT_BYTES",
        "AGENT_MAX_TOOL_OBSERVATION_BYTES",
        "AGENT_MAX_RUN_OBSERVATION_BYTES",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env()
    provider = build_provider(settings)

    assert settings.provider == "codex-cli"
    assert settings.model is None
    assert settings.api_key is None
    assert settings.cli_executable == "codex"
    assert settings.max_tool_observation_bytes == 32 * 1024
    assert settings.max_run_observation_bytes == 512 * 1024
    assert isinstance(provider, CodexCLIProvider)
    assert provider.info.default_model == "codex-cli-default"


def test_openai_compatible_remains_an_explicit_alternative() -> None:
    provider = build_provider(
        Settings(
            provider="openai-compatible",
            model="served-model",
            api_key="test-key",
            base_url="https://example.invalid/v1",
        )
    )

    assert isinstance(provider, OpenAIChatProvider)
    assert provider.info.default_model == "served-model"


def test_observation_budgets_load_from_dotenv(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "AGENT_MAX_TOOL_OBSERVATION_BYTES=65536\n"
        "AGENT_MAX_RUN_OBSERVATION_BYTES=786432\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "_DOTENV_PATH", dotenv)
    monkeypatch.delenv("AGENT_MAX_TOOL_OBSERVATION_BYTES", raising=False)
    monkeypatch.delenv("AGENT_MAX_RUN_OBSERVATION_BYTES", raising=False)

    settings = Settings.from_env()

    assert settings.max_tool_observation_bytes == 65_536
    assert settings.max_run_observation_bytes == 786_432
