import ast
from pathlib import Path

STARTER_SRC = Path(__file__).resolve().parents[1] / "src" / "agent_app"


def test_starter_does_not_import_harness_internals() -> None:
    forbidden = (
        "base_agent.runtime",
        "base_agent.harness",
    )
    violations: list[str] = []
    for path in STARTER_SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                if module.startswith(forbidden):
                    violations.append(f"{path.relative_to(STARTER_SRC)}: {module}")

    assert violations == []


def test_step_03_uses_self_contained_trusted_skill_packages() -> None:
    assert (STARTER_SRC / "tools.py").is_file()
    assert (STARTER_SRC / "skills" / "__init__.py").is_file()
    skill_root = STARTER_SRC / "skills" / "value_recording"
    assert (skill_root / "__init__.py").is_file()
    assert (skill_root / "SKILL.md").is_file()
    assert (skill_root / "tools.py").is_file()
    assert not (skill_root / "definition.py").exists()
    script_skill_root = STARTER_SRC / "skills" / "python_calculator"
    assert (script_skill_root / "SKILL.md").is_file()
    assert (script_skill_root / "tools.py").is_file()
    assert (script_skill_root / "scripts" / "calculate.py").is_file()
    assert not (script_skill_root / "definition.py").exists()
    assert not (STARTER_SRC / "tool_limit_demo.py").exists()
    for module_name in ("flows", "scenarios"):
        assert not tuple((STARTER_SRC / module_name).glob("*.py"))


def test_starter_only_owns_the_asgi_composition_root() -> None:
    assert not (STARTER_SRC / "runs.py").exists()
    assert not (STARTER_SRC / "server.py").exists()
    asgi_source = (STARTER_SRC / "asgi.py").read_text(encoding="utf-8")
    assert "from base_agent.server import AgentApplication" in asgi_source
    assert ").fastapi()" in asgi_source
    assert "@app." not in asgi_source


def test_cli_and_http_do_not_rebuild_the_agent_capability_policy() -> None:
    app_source = (STARTER_SRC / "app.py").read_text(encoding="utf-8")
    cli_source = (STARTER_SRC / "cli.py").read_text(encoding="utf-8")
    asgi_source = (STARTER_SRC / "asgi.py").read_text(encoding="utf-8")

    assert "STARTER_AGENT_DEFINITION = AgentDefinition(" in app_source
    assert "definition: AgentDefinition = STARTER_AGENT_DEFINITION" in app_source
    for source in (cli_source, asgi_source):
        assert "AGENT_SKILLS" not in source
        assert "AGENT_PERMISSIONS" not in source
        assert "--grant-permission" not in source
        assert "--enable-" not in source
