"""Small Prompt asset helper; Prompt remains plain text, not a DSL."""

from collections.abc import Mapping
from pathlib import Path


def load_prompt(
    path: str | Path,
    *,
    variables: Mapping[str, object] | None = None,
) -> str:
    prompt = Path(path).read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError(f"prompt file is empty: {path}")
    if variables:
        return prompt.format_map(dict(variables))
    return prompt
