"""Reusable business playbooks compiled into an immutable DSH persona."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import MappingProxyType

import yaml

from dsh_base_agent.governance import Confirmation, SideEffect
from dsh_base_agent.tools import Tool, Toolkit, flatten_tools

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_SUPPORTING_DIRECTORIES = ("references", "templates", "scripts", "assets")


@dataclass(frozen=True, slots=True)
class SkillPackage:
    root: Path
    metadata: Mapping[str, object]
    playbook: str
    files: tuple[str, ...]
    script_files: tuple[str, ...]
    references: tuple[str, ...]
    templates: tuple[str, ...]
    assets: tuple[str, ...]
    fingerprint: str

    @classmethod
    def load(cls, root: str | Path) -> SkillPackage:
        package_root = Path(root).expanduser().resolve()
        document_path = package_root / "SKILL.md"
        if not document_path.is_file():
            raise ValueError(f"Skill Package has no SKILL.md: {package_root}")
        document = document_path.read_text(encoding="utf-8")
        metadata, playbook = _parse_document(document, document_path)
        paths = tuple(
            sorted(
                path.relative_to(package_root).as_posix()
                for path in package_root.rglob("*")
                if path.is_file() and not any(part.startswith(".") for part in path.parts)
            )
        )
        digest = hashlib.sha256()
        for name in paths:
            digest.update(name.encode())
            digest.update(b"\0")
            digest.update((package_root / name).read_bytes())
            digest.update(b"\0")
        classified = {
            directory: tuple(name for name in paths if name.startswith(f"{directory}/"))
            for directory in _SUPPORTING_DIRECTORIES
        }
        return cls(
            root=package_root,
            metadata=MappingProxyType(metadata),
            playbook=playbook,
            files=paths,
            script_files=classified["scripts"],
            references=classified["references"],
            templates=classified["templates"],
            assets=classified["assets"],
            fingerprint=digest.hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class SkillDependencies:
    required_tools: tuple[str, ...] = ()
    optional_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(not name.strip() for name in (*self.required_tools, *self.optional_tools)):
            raise ValueError("Skill dependency names must not be blank")
        if set(self.required_tools) & set(self.optional_tools):
            raise ValueError("Skill dependencies cannot be both required and optional")


@dataclass(frozen=True, slots=True)
class SkillContributions:
    tools: tuple[Tool | Toolkit, ...] = ()

    def __post_init__(self) -> None:
        flatten_tools(self.tools)


@dataclass(frozen=True, slots=True)
class SkillPermissions:
    required: frozenset[str] = frozenset()
    optional: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if "" in self.required or "" in self.optional:
            raise ValueError("Skill permissions must not be blank")
        if self.required & self.optional:
            raise ValueError("Skill permissions cannot be both required and optional")


class SkillScriptRuntime(StrEnum):
    PYTHON = "python"


@dataclass(frozen=True, slots=True)
class SkillScriptExecution:
    runtimes: frozenset[SkillScriptRuntime] = frozenset()
    permission: str = "skills:scripts:execute"
    timeout_seconds: float = 10.0
    max_output_bytes: int = 65_536
    side_effect: SideEffect = SideEffect.READ_ONLY
    confirmation: Confirmation = Confirmation.NONE

    def __post_init__(self) -> None:
        if not self.permission.strip():
            raise ValueError("Skill script permission must not be blank")
        if self.timeout_seconds <= 0 or self.max_output_bytes < 1:
            raise ValueError("Skill script limits must be positive")
        if self.confirmation is Confirmation.REQUIRED and self.side_effect is SideEffect.READ_ONLY:
            raise ValueError("read-only Skill scripts must not require confirmation")

    @property
    def enabled(self) -> bool:
        return bool(self.runtimes)


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    playbook: str
    description: str = ""
    version: str = "1.0.0"
    dependencies: SkillDependencies = SkillDependencies()
    contributions: SkillContributions = SkillContributions()
    permissions: SkillPermissions = SkillPermissions()
    script_execution: SkillScriptExecution = SkillScriptExecution()
    package: SkillPackage | None = None

    def __post_init__(self) -> None:
        if not self.name or not _NAME_PATTERN.fullmatch(self.name):
            raise ValueError("skill name must contain only letters, numbers, '.', '_' or '-'")
        if not self.playbook.strip() or not self.version.strip():
            raise ValueError("skill playbook and version must not be blank")
        provided = {item.spec.name for item in flatten_tools(self.contributions.tools)}
        required = {*self.dependencies.required_tools, *self.dependencies.optional_tools}
        if provided & required:
            raise ValueError("Skill cannot both depend on and contribute the same Tool")
        if self.package is not None and self.playbook != self.package.playbook:
            raise ValueError("Skill playbook must match its Package")
        if self.script_execution.enabled:
            if self.package is None or not self.package.script_files:
                raise ValueError("Skill script execution requires Package scripts")
            if self.script_execution.permission not in self.permissions.required:
                raise ValueError("Skill script execution permission must be required")

    @classmethod
    def from_package(cls, root: str | Path) -> Skill:
        package = SkillPackage.load(root)
        return cls(
            name=_metadata_text(package, "name") or package.root.name,
            description=_metadata_text(package, "description") or "",
            version=_metadata_text(package, "version") or "1.0.0",
            playbook=package.playbook,
            package=package,
        )


@dataclass(frozen=True, slots=True)
class SkillDescriptor:
    name: str
    description: str
    version: str
    source: Path | None = None
    package_fingerprint: str | None = None

    @classmethod
    def from_skill(cls, skill: Skill) -> SkillDescriptor:
        package = skill.package
        return cls(
            name=skill.name,
            description=skill.description,
            version=skill.version,
            source=package.root if package else None,
            package_fingerprint=package.fingerprint if package else None,
        )


@dataclass(frozen=True, slots=True)
class SkillView:
    descriptor: SkillDescriptor
    file_path: str
    content: str
    linked_files: Mapping[str, tuple[str, ...]]


class SkillCatalog:
    def __init__(self, skills: Iterable[Skill] = ()) -> None:
        self._skills = tuple(skills)
        self._by_name = {skill.name: skill for skill in self._skills}
        if len(self._by_name) != len(self._skills):
            raise ValueError("registered Skills must have unique names")

    @classmethod
    def of(cls, *skills: Skill) -> SkillCatalog:
        return cls(skills)

    @classmethod
    def discover(cls, roots: str | Path | Iterable[str | Path]) -> SkillCatalog:
        values = (roots,) if isinstance(roots, (str, Path)) else tuple(roots)
        discovered = {
            path.parent.resolve()
            for root in values
            for path in Path(root).expanduser().resolve().rglob("SKILL.md")
        }
        return cls(Skill.from_package(path) for path in sorted(discovered, key=str))

    @property
    def skills(self) -> tuple[Skill, ...]:
        return self._skills

    @property
    def descriptors(self) -> tuple[SkillDescriptor, ...]:
        return tuple(SkillDescriptor.from_skill(skill) for skill in self._skills)

    def resolve(self, name: str) -> Skill:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise ValueError(f"Skill '{name}' is not available") from exc

    def select(self, names: Iterable[str]) -> SkillCatalog:
        selected = tuple(names)
        missing = set(selected) - self._by_name.keys()
        if missing:
            raise ValueError("unregistered Skills: " + ", ".join(sorted(missing)))
        return SkillCatalog(self._by_name[name] for name in selected)

    def view(self, name: str, file_path: str | None = None) -> SkillView:
        skill = self.resolve(name)
        package = skill.package
        linked = {
            "references": package.references if package else (),
            "templates": package.templates if package else (),
            "scripts": package.script_files if package else (),
            "assets": package.assets if package else (),
        }
        if file_path is None:
            return SkillView(SkillDescriptor.from_skill(skill), "SKILL.md", skill.playbook, linked)
        if package is None:
            raise ValueError(f"Skill '{name}' has no Package")
        normalized = PurePosixPath(file_path.replace("\\", "/"))
        allowed = {item for values in linked.values() for item in values}
        if normalized.as_posix() not in allowed or ".." in normalized.parts:
            raise ValueError("Skill supporting file is not exposed")
        return SkillView(
            SkillDescriptor.from_skill(skill),
            normalized.as_posix(),
            (package.root / normalized).read_text(encoding="utf-8"),
            linked,
        )


def _parse_document(document: str, source: Path) -> tuple[dict[str, object], str]:
    if not document.startswith("---\n"):
        return {}, document.strip()
    try:
        _, frontmatter, body = document.split("---", 2)
        parsed = yaml.safe_load(frontmatter) or {}
    except (ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"invalid Skill frontmatter in {source}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"Skill frontmatter must be a mapping in {source}")
    playbook = body.strip()
    if not playbook:
        raise ValueError(f"Skill playbook must not be blank in {source}")
    return parsed, playbook


def _metadata_text(package: SkillPackage, key: str) -> str | None:
    value = package.metadata.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Skill metadata '{key}' must be a non-blank string")
    return value.strip()


__all__ = [
    "Skill",
    "SkillCatalog",
    "SkillContributions",
    "SkillDependencies",
    "SkillDescriptor",
    "SkillPackage",
    "SkillPermissions",
    "SkillScriptExecution",
    "SkillScriptRuntime",
    "SkillView",
]
