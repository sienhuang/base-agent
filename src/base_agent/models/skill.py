"""Stable references to Skill versions used by a Run."""

from pydantic import BaseModel, ConfigDict, Field


class SkillReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
