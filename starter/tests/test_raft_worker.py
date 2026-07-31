from pathlib import Path
from uuid import UUID

import pytest

from agent_app.config import Settings
from agent_app.raft_worker import build_raft_worker


def test_build_raft_worker_uses_application_agent(tmp_path: Path) -> None:
    worker = build_raft_worker(
        Settings(
            raft_profile="iris-external-no-1",
            raft_agent_id=UUID("d3a429f5-ecb0-4caf-a209-821f0467a1c5"),
            raft_agent_handle="iris-external-no-1",
            raft_cli_executable="/usr/local/bin/raft",
            raft_state_dir=tmp_path,
        )
    )

    assert worker.config.profile == "iris-external-no-1"
    assert worker.config.handle == "iris-external-no-1"
    assert worker.agent.profile.id == "starter-agent"


def test_build_raft_worker_requires_external_agent_identity() -> None:
    with pytest.raises(ValueError, match="RAFT_PROFILE"):
        build_raft_worker(Settings())
