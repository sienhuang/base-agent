import pytest

from agent_app import Settings


def test_settings_reads_explicit_data_capability_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_ENABLE_CODING", "true")
    monkeypatch.setenv("AGENT_SANDBOX_IMAGE", "python:3.12")
    monkeypatch.setenv("AGENT_ENABLE_WEB_SEARCH", "1")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "search-key")
    monkeypatch.setenv("AGENT_ENABLE_MTBI", "yes")
    monkeypatch.setenv("AGENT_MTBI_CLI_EXECUTABLE", "/usr/local/bin/mtbi-cli")
    monkeypatch.setenv("AGENT_MTBI_ENGINE", "spark")
    monkeypatch.setenv("AGENT_MTBI_REGION", "volc_cn")

    settings = Settings.from_env()

    assert settings.enable_coding is True
    assert settings.sandbox_image == "python:3.12"
    assert settings.enable_web_search is True
    assert settings.brave_search_api_key == "search-key"
    assert settings.enable_mtbi is True
    assert settings.mtbi_cli_executable == "/usr/local/bin/mtbi-cli"
    assert settings.mtbi_engine == "SPARK"
    assert settings.mtbi_region == "volc_cn"


def test_settings_rejects_invalid_boolean_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_ENABLE_CODING", "sometimes")

    with pytest.raises(ValueError, match="must be true or false"):
        Settings.from_env()


def test_settings_reads_raft_worker_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAFT_PROFILE", "iris-external-no-1")
    monkeypatch.setenv(
        "RAFT_AGENT_ID",
        "d3a429f5-ecb0-4caf-a209-821f0467a1c5",
    )
    monkeypatch.setenv(
        "RAFT_CLI_EXECUTABLE",
        "/Users/example/.local/bin/raft",
    )

    settings = Settings.from_env()

    assert settings.raft_profile == "iris-external-no-1"
    assert str(settings.raft_agent_id) == (
        "d3a429f5-ecb0-4caf-a209-821f0467a1c5"
    )
    assert settings.raft_cli_executable == "/Users/example/.local/bin/raft"
