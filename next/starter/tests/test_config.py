from __future__ import annotations

import os

from agent_app import config
from agent_app.config import Settings

_CONFIG_NAMES = (
    "AGENT_PROVIDER",
    "AGENT_MODEL",
    "AGENT_CLI_EXECUTABLE",
    "AGENT_CLI_TIMEOUT_SECONDS",
    "AGENT_CLI_MAX_OUTPUT_BYTES",
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
                "OPENAI_API_KEY=dotenv-key",
                "OPENAI_BASE_URL=http://localhost:8000/v1",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_DOTENV_PATH", dotenv_path)

    settings = Settings.from_env()

    assert settings.provider == "openai-compatible"
    assert settings.model == "dotenv-model"
    assert settings.api_key == "dotenv-key"
    assert settings.base_url == "http://localhost:8000/v1"
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
