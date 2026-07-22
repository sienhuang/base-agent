"""Registry that discovers manifests and loads instructions on demand."""

from dataclasses import dataclass
from pathlib import Path

from base_agent.skills.errors import DuplicateSkillError, SkillNotFoundError
from base_agent.skills.loader import SkillLoader
from base_agent.skills.models import Skill, SkillManifest


@dataclass(frozen=True, slots=True)
class _Registration:
    manifest: SkillManifest
    path: Path


class SkillRegistry:
    def __init__(self, *, loader: SkillLoader | None = None) -> None:
        self.loader = loader or SkillLoader()
        self._skills: dict[str, _Registration] = {}

    @classmethod
    def from_directory(cls, root: Path, *, loader: SkillLoader | None = None) -> "SkillRegistry":
        registry = cls(loader=loader)
        if not root.is_dir():
            raise ValueError(f"Skill directory '{root}' does not exist")
        for skill_file in sorted(root.rglob(SkillLoader.filename)):
            registry.register_path(skill_file)
        return registry

    def register_path(self, path: Path) -> SkillManifest:
        manifest = self.loader.load_manifest(path)
        if manifest.name in self._skills:
            existing = self._skills[manifest.name]
            raise DuplicateSkillError(
                f"Skill '{manifest.name}' is already registered from '{existing.path}'"
            )
        skill_file = path / SkillLoader.filename if path.is_dir() else path
        self._skills[manifest.name] = _Registration(manifest, skill_file.resolve())
        return manifest

    def manifest(self, name: str) -> SkillManifest:
        try:
            return self._skills[name].manifest
        except KeyError as exc:
            raise SkillNotFoundError(f"Skill '{name}' is not registered") from exc

    def load(self, name: str) -> Skill:
        try:
            registration = self._skills[name]
        except KeyError as exc:
            raise SkillNotFoundError(f"Skill '{name}' is not registered") from exc
        return self.loader.load(registration.path)

    def select(self, names: tuple[str, ...]) -> tuple[Skill, ...]:
        return tuple(self.load(name) for name in names)

    def __len__(self) -> int:
        return len(self._skills)
