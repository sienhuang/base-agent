from pathlib import Path

from base_agent import load_prompt

_PACKAGE_ROOT = Path(__file__).resolve().parent


def prompt(name: str) -> str:
    return load_prompt(_PACKAGE_ROOT / "prompts" / name)
