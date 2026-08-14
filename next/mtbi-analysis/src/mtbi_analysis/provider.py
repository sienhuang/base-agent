"""Application-owned Provider composition."""

from base_agent.providers import CodexCLIProvider, ModelProvider, OpenAIChatProvider

from mtbi_analysis.config import Settings


def build_provider(settings: Settings) -> ModelProvider:
    if settings.provider == "codex-cli":
        return CodexCLIProvider(
            model=settings.model,
            executable=settings.cli_executable,
            timeout_seconds=settings.cli_timeout_seconds,
            max_output_bytes=settings.cli_max_output_bytes,
        )
    if settings.model is None:
        raise ValueError("OpenAI-compatible mode requires AGENT_MODEL or OPENAI_MODEL")
    if settings.api_key is None:
        raise ValueError(
            "OPENAI_API_KEY is required; use a placeholder for endpoints without authentication"
        )
    return OpenAIChatProvider(
        default_model=settings.model,
        api_key=settings.api_key,
        base_url=settings.base_url,
        timeout_seconds=60,
        structured_output_mode=settings.structured_output_mode,
    )
