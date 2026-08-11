"""Application-owned Skill catalog."""

from __future__ import annotations

from pathlib import Path

from base_agent import SkillCatalog

PYTHON_CALCULATOR_SKILL_NAME = "python-calculator"
VALUE_RECORDING_SKILL_NAME = "value-recording"

SKILL_CATALOG = SkillCatalog.discover_trusted(Path(__file__).parent)

__all__ = [
    "PYTHON_CALCULATOR_SKILL_NAME",
    "SKILL_CATALOG",
    "VALUE_RECORDING_SKILL_NAME",
]
