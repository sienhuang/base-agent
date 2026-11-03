"""Local ownership of DSH homes and the shared service event stream."""

from __future__ import annotations

from pathlib import Path

from dsh_base_agent.events import JsonlEventStore


class LocalState:
    """Filesystem state used by the local prototype.

    DSH owns Session persistence below ``dsh-home``.  The Host owns only the
    Run/Flow control-plane Event stream below ``events``.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.event_store = JsonlEventStore(self.root / "events")

    def runtime_home(self, agent_fingerprint: str) -> Path:
        if not agent_fingerprint.strip():
            raise ValueError("agent_fingerprint must not be blank")
        path = self.root / "dsh-home" / agent_fingerprint
        path.mkdir(parents=True, exist_ok=True)
        return path


__all__ = ["LocalState"]
