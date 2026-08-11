"""Immutable Skill metadata, selection, and progressive content loading."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from base_agent.skills.models import (
    Skill,
    SkillContributions,
    SkillPackage,
)
from base_agent.skills.trusted import load_trusted_skill_tools

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_SUPPORTING_DIRECTORIES = frozenset({"assets", "references", "scripts", "templates"})


@dataclass(frozen=True, slots=True)
class SkillDescriptor:
    """Lightweight Skill metadata safe to expose without its playbook."""

    name: str
    description: str
    version: str
    source: Path | None = None
    package_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not _NAME_PATTERN.fullmatch(self.name):
            raise ValueError(
                "skill descriptor name must contain only letters, numbers, '.', '_' or '-'"
            )
        if not self.version.strip():
            raise ValueError("skill descriptor version must not be blank")
        if self.source is not None:
            object.__setattr__(self, "source", self.source.resolve())

    @classmethod
    def from_skill(cls, skill: Skill) -> SkillDescriptor:
        package = skill.package
        return cls(
            name=skill.name,
            description=skill.description.strip(),
            version=skill.version,
            source=package.root if package is not None else None,
            package_fingerprint=(package.fingerprint if package is not None else None),
        )


@dataclass(frozen=True, slots=True)
class SkillView:
    """One resolved playbook or supporting file returned by ``skill_view``."""

    descriptor: SkillDescriptor
    file_path: str
    content: str
    linked_files: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "linked_files",
            MappingProxyType(dict(self.linked_files)),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.descriptor.name,
            "description": self.descriptor.description,
            "version": self.descriptor.version,
            "package_fingerprint": self.descriptor.package_fingerprint,
            "file": self.file_path,
            "content": self.content,
            "linked_files": {
                kind: list(paths) for kind, paths in self.linked_files.items() if paths
            },
        }


class SkillCatalog:
    """Application-owned immutable catalog of registered Skills."""

    def __init__(self, skills: Iterable[Skill] = ()) -> None:
        registered = tuple(skills)
        by_name = {skill.name: skill for skill in registered}
        if len(by_name) != len(registered):
            raise ValueError("registered Skills must have unique names")
        self._skills = registered
        self._skills_by_name = MappingProxyType(by_name)
        self._descriptors = tuple(SkillDescriptor.from_skill(skill) for skill in registered)

    @classmethod
    def of(cls, *skills: Skill) -> SkillCatalog:
        return cls(skills)

    @classmethod
    def discover(
        cls,
        roots: str | Path | Iterable[str | Path],
        *,
        definitions: Iterable[Skill] = (),
    ) -> SkillCatalog:
        """Discover Skill Packages without importing code from scanned paths.

        Instruction-only Skills are created directly from ``SKILL.md``. Skills
        that contribute Python Tools or execution policies must be supplied as
        explicit ``definitions`` and are matched by their Package root.
        """
        return cls._discover(
            roots,
            definitions=definitions,
            load_trusted_code=False,
        )

    @classmethod
    def discover_trusted(
        cls,
        roots: str | Path | Iterable[str | Path],
        *,
        definitions: Iterable[Skill] = (),
    ) -> SkillCatalog:
        """Discover Skills and load ``tools.py`` from application-owned roots.

        Calling this method is an explicit trust decision: Python in each
        discovered Package may execute during Catalog construction.
        """
        return cls._discover(
            roots,
            definitions=definitions,
            load_trusted_code=True,
        )

    @classmethod
    def _discover(
        cls,
        roots: str | Path | Iterable[str | Path],
        *,
        definitions: Iterable[Skill],
        load_trusted_code: bool,
    ) -> SkillCatalog:
        search_roots = _normalize_search_roots(roots)
        skill_roots = _discover_skill_roots(search_roots)
        _reject_nested_skills(skill_roots)

        definitions_by_root: dict[Path, Skill] = {}
        for skill in definitions:
            if skill.package is None:
                raise ValueError(f"discovered Skill definition '{skill.name}' must have a Package")
            package_root = skill.package.root.resolve()
            if package_root in definitions_by_root:
                raise ValueError(f"multiple Skill definitions target Package '{package_root}'")
            definitions_by_root[package_root] = skill

        discovered: list[Skill] = []
        for skill_root in skill_roots:
            supplied_skill = definitions_by_root.pop(skill_root, None)
            if supplied_skill is not None:
                discovered.append(supplied_skill)
                continue
            skill = Skill.from_package(skill_root)
            package = skill.package
            if package is None:  # from_package always resolves one.
                raise RuntimeError("discovered Skill has no Package")
            missing_fields = [
                field for field in ("name", "description") if not package.metadata.get(field)
            ]
            if missing_fields:
                raise ValueError(
                    f"auto-discovered Skill '{skill_root}' requires frontmatter "
                    f"fields: {', '.join(missing_fields)}"
                )
            if load_trusted_code:
                trusted_tools = load_trusted_skill_tools(package)
                if trusted_tools:
                    skill = replace(
                        skill,
                        contributions=SkillContributions(tools=trusted_tools),
                    )
            discovered.append(skill)

        if definitions_by_root:
            missing_paths = ", ".join(str(path) for path in sorted(definitions_by_root))
            raise ValueError(f"Skill definitions are outside the discovered roots: {missing_paths}")
        return cls(discovered)

    @property
    def skills(self) -> tuple[Skill, ...]:
        """Return registered runtime Skill definitions in catalog order."""
        return self._skills

    @property
    def descriptors(self) -> tuple[SkillDescriptor, ...]:
        """Return lightweight metadata without loading playbooks."""
        return self._descriptors

    def get(self, name: str) -> SkillDescriptor | None:
        skill = self._skills_by_name.get(name)
        return SkillDescriptor.from_skill(skill) if skill is not None else None

    def resolve(self, name: str) -> Skill:
        try:
            return self._skills_by_name[name]
        except KeyError as exc:
            raise ValueError(f"Skill '{name}' is not available") from exc

    def select(self, names: Iterable[str]) -> SkillCatalog:
        """Create a catalog restricted to explicitly selected Skill names."""
        selected_names = tuple(names)
        missing = set(selected_names) - self._skills_by_name.keys()
        if missing:
            raise ValueError("unregistered Skills: " + ", ".join(sorted(missing)))
        return SkillCatalog(self._skills_by_name[name] for name in selected_names)

    def view(self, name: str, file_path: str | None = None) -> SkillView:
        """Resolve one selected Skill playbook or supporting text file."""
        skill = self.resolve(name)
        descriptor = SkillDescriptor.from_skill(skill)
        package = skill.package
        linked_files = _linked_files(package)
        if file_path is None:
            return SkillView(
                descriptor=descriptor,
                file_path="SKILL.md",
                content=skill.playbook,
                linked_files=linked_files,
            )
        if package is None:
            raise ValueError(f"Skill '{name}' has no Package supporting files")
        relative_path = _validate_supporting_path(file_path)
        allowed = {path for paths in linked_files.values() for path in paths}
        if relative_path not in allowed:
            raise ValueError(
                f"File '{relative_path}' is not an exposed supporting file of Skill '{name}'"
            )
        current_package = SkillPackage.load(package.root)
        if current_package.fingerprint != package.fingerprint:
            raise ValueError("Skill Package changed after it was selected; rebuild the Agent")
        path = package.root / relative_path
        try:
            content = path.read_text(encoding="utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ValueError(f"Skill supporting file '{relative_path}' is not UTF-8 text") from exc
        return SkillView(
            descriptor=descriptor,
            file_path=relative_path,
            content=content,
            linked_files=linked_files,
        )


def _linked_files(
    package: SkillPackage | None,
) -> Mapping[str, tuple[str, ...]]:
    if package is None:
        return MappingProxyType({})
    return MappingProxyType(
        {
            "references": package.references,
            "templates": package.templates,
            "scripts": package.script_files,
            "assets": package.assets,
        }
    )


def _validate_supporting_path(file_path: str) -> str:
    normalized = file_path.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or path.parts[0] not in _SUPPORTING_DIRECTORIES
    ):
        raise ValueError(
            "Skill supporting file must be a relative path under assets/, "
            "references/, scripts/, or templates/"
        )
    return path.as_posix()


def _normalize_search_roots(
    roots: str | Path | Iterable[str | Path],
) -> tuple[Path, ...]:
    values = (roots,) if isinstance(roots, (str, Path)) else tuple(roots)
    normalized: list[Path] = []
    for value in values:
        root = Path(value).resolve()
        if not root.is_dir():
            raise ValueError(f"Skill discovery root does not exist: {value}")
        normalized.append(root)
    return tuple(normalized)


def _discover_skill_roots(search_roots: tuple[Path, ...]) -> tuple[Path, ...]:
    discovered: set[Path] = set()
    for root in search_roots:
        if (root / "SKILL.md").is_file():
            discovered.add(root)
        discovered.update(
            path.parent.resolve() for path in root.rglob("SKILL.md") if path.is_file()
        )
    return tuple(sorted(discovered, key=str))


def _reject_nested_skills(skill_roots: tuple[Path, ...]) -> None:
    roots = set(skill_roots)
    for root in skill_roots:
        nested_under = next(
            (parent for parent in root.parents if parent in roots),
            None,
        )
        if nested_under is not None:
            raise ValueError(
                f"Skill Package '{root}' is nested inside Skill Package '{nested_under}'"
            )
