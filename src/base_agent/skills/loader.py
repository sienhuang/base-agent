"""Safe parser for versioned SKILL.md packages."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from base_agent.skills.errors import InvalidSkillError
from base_agent.skills.models import Skill, SkillManifest


class SkillLoader:
    """Read only front matter during discovery and full instructions after selection."""

    filename = "SKILL.md"

    def load_manifest(self, path: Path) -> SkillManifest:
        skill_file = self._resolve_file(path)
        front_matter = self._read_front_matter(skill_file)
        try:
            payload: Any = yaml.safe_load(front_matter)
            if not isinstance(payload, dict):
                raise InvalidSkillError(f"Skill manifest in '{skill_file}' must be a mapping")
            return SkillManifest.model_validate(payload)
        except yaml.YAMLError as exc:
            raise InvalidSkillError(f"invalid YAML in '{skill_file}': {exc}") from exc
        except ValidationError as exc:
            raise InvalidSkillError(f"invalid Skill manifest in '{skill_file}': {exc}") from exc

    def load(self, path: Path) -> Skill:
        skill_file = self._resolve_file(path)
        content = skill_file.read_text(encoding="utf-8")
        _, instructions = self._split_document(content, skill_file)
        normalized = instructions.strip()
        if not normalized:
            raise InvalidSkillError(f"Skill instructions in '{skill_file}' must not be empty")
        return Skill(
            manifest=self.load_manifest(skill_file),
            instructions=normalized,
            source=skill_file,
        )

    def _read_front_matter(self, skill_file: Path) -> str:
        lines: list[str] = []
        with skill_file.open(encoding="utf-8") as handle:
            if handle.readline().strip() != "---":
                raise InvalidSkillError(f"'{skill_file}' must start with YAML front matter")
            for line in handle:
                if line.strip() == "---":
                    return "".join(lines)
                lines.append(line)
        raise InvalidSkillError(f"'{skill_file}' has no closing front matter delimiter")

    @staticmethod
    def _split_document(content: str, skill_file: Path) -> tuple[str, str]:
        lines = content.splitlines()
        if not lines or lines[0].strip() != "---":
            raise InvalidSkillError(f"'{skill_file}' must start with YAML front matter")
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
        raise InvalidSkillError(f"'{skill_file}' has no closing front matter delimiter")

    def _resolve_file(self, path: Path) -> Path:
        skill_file = path / self.filename if path.is_dir() else path
        if not skill_file.is_file():
            raise InvalidSkillError(f"Skill file '{skill_file}' does not exist")
        return skill_file.resolve()
