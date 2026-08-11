"""Opt-in Python loading for application-owned Skill directories."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from base_agent.skills.models import SkillPackage
from base_agent.tools import FunctionTool, Tool, Toolkit, flatten_tools

_TOOLS_FILE = "tools.py"
_EXPLICIT_TOOLS_EXPORT = "TOOLS"


def load_trusted_skill_tools(package: SkillPackage) -> tuple[Tool | Toolkit, ...]:
    """Load Tools from one explicitly trusted Skill Package.

    A local ``@tool`` declared in ``tools.py`` is discovered automatically.
    Advanced packages may export ``TOOLS`` to choose exact Tool/Toolkit
    objects. No other Python file is imported unless ``tools.py`` imports it.
    """
    tools_path = package.root / _TOOLS_FILE
    if not tools_path.is_file():
        return ()

    module = _load_tools_module(package, tools_path)
    if _EXPLICIT_TOOLS_EXPORT in module.__dict__:
        items = _validate_explicit_tools(
            module.__dict__[_EXPLICIT_TOOLS_EXPORT],
            tools_path=tools_path,
        )
    else:
        items = _locally_declared_function_tools(module)
        if not items:
            raise ValueError(
                f"trusted Skill tools module declares no local @tool functions: {tools_path}"
            )

    flatten_tools(items)
    return items


def _load_tools_module(package: SkillPackage, tools_path: Path) -> ModuleType:
    resolved_tools_path = tools_path.resolve()
    digest = hashlib.sha256(f"{package.root}\0{package.fingerprint}".encode()).hexdigest()[:24]
    package_name = f"_base_agent_trusted_skill_{digest}"
    module_name = f"{package_name}.tools"
    existing = sys.modules.get(module_name)
    if isinstance(existing, ModuleType):
        return existing

    dynamic_package = ModuleType(package_name)
    dynamic_package.__file__ = str(package.root)
    dynamic_package.__package__ = package_name
    dynamic_package.__path__ = [str(package.root)]
    sys.modules[package_name] = dynamic_package

    spec = importlib.util.spec_from_file_location(module_name, resolved_tools_path)
    if spec is None or spec.loader is None:
        _remove_dynamic_package(package_name)
        raise ValueError(f"cannot load trusted Skill tools module: {resolved_tools_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        _remove_dynamic_package(package_name)
        raise ValueError(
            f"failed to load trusted Skill tools module '{resolved_tools_path}': "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return module


def _locally_declared_function_tools(module: ModuleType) -> tuple[FunctionTool, ...]:
    discovered: list[FunctionTool] = []
    seen: set[int] = set()
    for _, value in sorted(module.__dict__.items()):
        if (
            isinstance(value, FunctionTool)
            and value.source_module == module.__name__
            and id(value) not in seen
        ):
            discovered.append(value)
            seen.add(id(value))
    return tuple(discovered)


def _validate_explicit_tools(
    exported: object,
    *,
    tools_path: Path,
) -> tuple[Tool | Toolkit, ...]:
    if not isinstance(exported, tuple | list):
        raise ValueError(f"TOOLS must be a tuple or list in {tools_path}")
    items: list[Tool | Toolkit] = []
    for item in exported:
        if not isinstance(item, (Tool, Toolkit)):
            raise ValueError(f"TOOLS contains an invalid Tool or Toolkit in {tools_path}")
        items.append(item)
    return tuple(items)


def _remove_dynamic_package(package_name: str) -> None:
    for name in tuple(sys.modules):
        if name == package_name or name.startswith(f"{package_name}."):
            sys.modules.pop(name, None)
