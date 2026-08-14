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
