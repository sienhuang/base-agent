"""Reusable experience packages selected by an Agent."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from base_agent.governance import Confirmation, SideEffect
from base_agent.prompts import load_prompt
from base_agent.skills.document import parse_skill_document
from base_agent.tools import Tool, Toolkit, flatten_tools

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_PACKAGE_DIRECTORIES = ("scripts", "references", "templates", "assets")


def _validate_names(field_name: str, values: tuple[str, ...]) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"Skill {field_name} must not contain duplicates")
    if any(not value or not _NAME_PATTERN.fullmatch(value) for value in values):
        raise ValueError(f"Skill {field_name} contains an invalid name")


@dataclass(frozen=True, slots=True)
class SkillPackage:
    """Resolved, read-only files that belong to one Skill directory."""

    root: Path
    metadata: Mapping[str, Any]
    playbook: str
    files: tuple[str, ...]
    script_files: tuple[str, ...]
    references: tuple[str, ...]
    templates: tuple[str, ...]
    assets: tuple[str, ...]
    fingerprint: str

    @classmethod
    def load(cls, root: str | Path) -> SkillPackage:
        package_root = Path(root).resolve()
        if not package_root.is_dir():
            raise ValueError(f"Skill package directory does not exist: {root}")
        playbook_path = package_root / "SKILL.md"
        if not playbook_path.is_file():
            raise ValueError(f"Skill package is missing required SKILL.md: {package_root}")

        package_files: list[Path] = []
        for path in sorted(package_root.rglob("*")):
            relative_path = path.relative_to(package_root)
            if "__pycache__" in relative_path.parts or path.suffix == ".pyc":
                continue
            if path.is_symlink():
                raise ValueError(f"Skill package must not contain symbolic links: {relative_path}")
            if path.is_file():
                package_files.append(path)

        relative_files = tuple(path.relative_to(package_root).as_posix() for path in package_files)
        classified = {
            directory: tuple(
                relative
                for relative in relative_files
                if relative.startswith(f"{directory}/")
                and PurePosixPath(relative).name != "__init__.py"
                and not PurePosixPath(relative).name.startswith(".")
            )
            for directory in _PACKAGE_DIRECTORIES
        }
        digest = hashlib.sha256()
        for relative_name, path in zip(
            relative_files,
            package_files,
            strict=True,
        ):
            digest.update(relative_name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")

        metadata, playbook = parse_skill_document(
            load_prompt(playbook_path),
            source=str(playbook_path),
        )
        return cls(
            root=package_root,
            metadata=metadata,
            playbook=playbook,
            files=relative_files,
            script_files=classified["scripts"],
            references=classified["references"],
            templates=classified["templates"],
            assets=classified["assets"],
            fingerprint=digest.hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class SkillDependencies:
    """Capabilities that must or may be supplied outside this Skill."""

    required_tools: tuple[str, ...] = ()
    optional_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_names("required_tools", self.required_tools)
        _validate_names("optional_tools", self.optional_tools)
        overlap = set(self.required_tools).intersection(self.optional_tools)
        if overlap:
            raise ValueError(
                "Skill dependencies cannot be both required and optional: "
                f"{', '.join(sorted(overlap))}"
            )


@dataclass(frozen=True, slots=True)
class SkillContributions:
    """Explicit Tool capabilities owned by this Skill."""

    tools: tuple[Tool | Toolkit, ...] = ()

    def __post_init__(self) -> None:
        flatten_tools(self.tools)


@dataclass(frozen=True, slots=True)
class SkillPermissions:
    """Policy grants needed for full or degraded Skill operation."""

    required: frozenset[str] = frozenset()
    optional: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if "" in self.required or "" in self.optional:
            raise ValueError("Skill permissions must not be blank")
        overlap = self.required.intersection(self.optional)
        if overlap:
            raise ValueError(
                "Skill permissions cannot be both required and optional: "
                f"{', '.join(sorted(overlap))}"
            )


class SkillScriptRuntime(StrEnum):
    PYTHON = "python"


@dataclass(frozen=True, slots=True)
class SkillScriptExecution:
    """Opt-in policy for direct execution of auto-discovered scripts."""

    runtimes: frozenset[SkillScriptRuntime] = frozenset()
    permission: str = "skills:scripts:execute"
    timeout_seconds: float = 10.0
    max_output_bytes: int = 65_536
    side_effect: SideEffect = SideEffect.READ_ONLY
    confirmation: Confirmation = Confirmation.NONE

    def __post_init__(self) -> None:
        if not self.permission.strip():
            raise ValueError("Skill script execution permission must not be blank")
        if self.timeout_seconds <= 0:
            raise ValueError("Skill script execution timeout_seconds must be greater than zero")
        if self.max_output_bytes < 1:
            raise ValueError("Skill script execution max_output_bytes must be at least 1")
        if self.confirmation is Confirmation.REQUIRED and self.side_effect is SideEffect.READ_ONLY:
            raise ValueError("read-only Skill script execution must not require confirmation")

    @property
    def enabled(self) -> bool:
        return bool(self.runtimes)


_EMPTY_DEPENDENCIES = SkillDependencies()
_EMPTY_CONTRIBUTIONS = SkillContributions()
_EMPTY_PERMISSIONS = SkillPermissions()
_DISABLED_SCRIPT_EXECUTION = SkillScriptExecution()


@dataclass(frozen=True, slots=True)
class Skill:
    """A reusable playbook with dependencies and owned contributions."""

    name: str
    playbook: str
    description: str = ""
    version: str = "1.0.0"
    dependencies: SkillDependencies = _EMPTY_DEPENDENCIES
    contributions: SkillContributions = _EMPTY_CONTRIBUTIONS
    permissions: SkillPermissions = _EMPTY_PERMISSIONS
    script_execution: SkillScriptExecution = _DISABLED_SCRIPT_EXECUTION
    package: SkillPackage | None = None

    def __post_init__(self) -> None:
        if not self.name or not _NAME_PATTERN.fullmatch(self.name):
            raise ValueError("skill name must contain only letters, numbers, '.', '_' or '-'")
        if not self.playbook.strip():
            raise ValueError("skill playbook must not be blank")
        provided_tool_names = {tool.spec.name for tool in flatten_tools(self.contributions.tools)}
        dependency_tool_names = {
            *self.dependencies.required_tools,
            *self.dependencies.optional_tools,
        }
        overlap = provided_tool_names.intersection(dependency_tool_names)
        if overlap:
            raise ValueError(
                f"Skill cannot both depend on and contribute Tools: {', '.join(sorted(overlap))}"
            )
        if self.package is not None:
            if self.playbook != self.package.playbook:
                raise ValueError("Skill playbook must match its package SKILL.md")
        if self.script_execution.enabled:
            if self.package is None:
                raise ValueError("Skill script execution requires a loaded Skill package")
            if not self.package.script_files:
                raise ValueError("Skill script execution requires at least one script file")
            if self.script_execution.permission not in self.permissions.required:
                raise ValueError("Skill script execution permission must be required by the Skill")

    @classmethod
    def from_package(
        cls,
        root: str | Path,
        *,
        name: str | None = None,
        description: str | None = None,
        version: str | None = None,
        dependencies: SkillDependencies = _EMPTY_DEPENDENCIES,
        contributions: SkillContributions = _EMPTY_CONTRIBUTIONS,
        permissions: SkillPermissions = _EMPTY_PERMISSIONS,
        script_execution: SkillScriptExecution = _DISABLED_SCRIPT_EXECUTION,
    ) -> Skill:
        package = SkillPackage.load(root)
        manifest_name = _metadata_text(package, "name")
        manifest_description = _metadata_text(package, "description")
        manifest_version = _metadata_text(package, "version")
        _reject_metadata_conflict("name", name, manifest_name, package)
        _reject_metadata_conflict(
            "description",
            description,
            manifest_description,
            package,
        )
        _reject_metadata_conflict(
            "version",
            version,
            manifest_version,
            package,
        )
        return cls(
            name=name or manifest_name or package.root.name,
            description=description or manifest_description or "",
            version=version or manifest_version or "1.0.0",
            playbook=package.playbook,
            dependencies=dependencies,
            contributions=contributions,
            permissions=permissions,
            script_execution=script_execution,
            package=package,
        )


def _metadata_text(package: SkillPackage, key: str) -> str | None:
    value = package.metadata.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"Skill frontmatter '{key}' must be a non-blank string in {package.root / 'SKILL.md'}"
        )
    return value.strip()


def _reject_metadata_conflict(
    field_name: str,
    explicit: str | None,
    manifest: str | None,
    package: SkillPackage,
) -> None:
    if explicit is None or manifest is None:
        return
    if explicit.strip() != manifest:
        raise ValueError(
            f"Skill {field_name} conflicts with SKILL.md frontmatter in {package.root}"
        )
