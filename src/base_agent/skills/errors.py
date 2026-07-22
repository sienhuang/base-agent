"""Typed failures raised before a Skill reaches model execution."""


class InvalidSkillError(ValueError):
    """Raised when a SKILL.md package does not satisfy the format contract."""


class DuplicateSkillError(ValueError):
    """Raised when a registry contains the same Skill name more than once."""


class SkillNotFoundError(LookupError):
    """Raised when an Agent requests a Skill that is not registered."""


class SkillNotEnabledError(PermissionError):
    """Raised when a Run selects a Skill outside its AgentProfile."""


class SkillRequirementsError(ValueError):
    """Raised when tools or permissions required by a selected Skill are unavailable."""
