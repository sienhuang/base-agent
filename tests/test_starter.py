import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


def test_starter_is_path_independent_copyable_and_offline_runnable(tmp_path: Path) -> None:
    repository = Path(__file__).parents[1]
    source = repository / "starter"
    copied = tmp_path / "my-agent"
    shutil.copytree(source, copied)

    configuration = tomllib.loads((copied / "pyproject.toml").read_text())
    dependencies = configuration["project"]["dependencies"]
    assert dependencies == ["base-agent>=0.1,<0.2"]
    assert all(
        "path" not in dependency and "file:" not in dependency
        for dependency in dependencies
    )

    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(repository / "src"), str(copied / "src")]
    )
    cli = subprocess.run(
        [sys.executable, "-m", "agent_app", "hello", "reusable", "agent"],
        cwd=copied,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    tests = subprocess.run(
        [sys.executable, "-m", "pytest"],
        cwd=copied,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert cli.stdout.strip() == (
        "Offline starter completed the Tool loop: 3 words, 20 characters."
    )
    assert "3 passed" in tests.stdout
