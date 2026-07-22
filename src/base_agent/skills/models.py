"""Validated Skill manifest and loaded instructions."""

from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from base_agent.models import SkillReference


class SkillManifest(BaseModel):
    """Machine-readable front matter from one SKILL.md."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        populate_by_name=True,
    )

    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    version: str = Field(
        pattern=r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$",
    )
    description: str = Field(min_length=1)
    argument_hint: str | None = Field(default=None, alias="argument-hint")
    allowed_tools: tuple[str, ...] = Field(default=(), alias="allowed-tools")
    required_tools: tuple[str, ...] = Field(default=(), alias="required-tools")
    required_permissions: frozenset[str] = Field(
        default=frozenset(),
        alias="required-permissions",
    )
    input_schema: dict[str, Any] | None = Field(default=None, alias="input-schema")
    output_schema: dict[str, Any] | None = Field(default=None, alias="output-schema")

    @model_validator(mode="after")
    def validate_tool_contract(self) -> Self:
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError("allowed-tools must not contain duplicates")
        if len(set(self.required_tools)) != len(self.required_tools):
            raise ValueError("required-tools must not contain duplicates")
        missing = set(self.required_tools) - set(self.allowed_tools)
        if missing:
            raise ValueError(
                f"required-tools must also appear in allowed-tools: {', '.join(sorted(missing))}"
            )
        return self

    def reference(self) -> SkillReference:
        return SkillReference(name=self.name, version=self.version)


class Skill(BaseModel):
    """A fully loaded Skill ready to enter one RuntimeContext."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    manifest: SkillManifest
    instructions: str = Field(min_length=1)
    source: Path
