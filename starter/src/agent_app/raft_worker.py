"""Application entry point for the optional Raft External Agent Worker."""

from __future__ import annotations

import argparse
import asyncio

from base_agent.integrations.raft import (
    RaftDrainResult,
    RaftWorker,
    RaftWorkerConfig,
)

from agent_app.agent import build_agent
from agent_app.config import Settings


def build_raft_worker(
    settings: Settings | None = None,
    *,
    react: bool = False,
) -> RaftWorker:
    """Compose the transport Worker around this application's Agent."""
    resolved = settings or Settings.from_env()
    if resolved.raft_profile is None:
        raise ValueError("RAFT_PROFILE is required for the Raft Worker")
    if resolved.raft_agent_id is None:
        raise ValueError("RAFT_AGENT_ID is required for the Raft Worker")
    return RaftWorker(
        build_agent(resolved, react=react),
        RaftWorkerConfig(
            profile=resolved.raft_profile,
            agent_id=resolved.raft_agent_id,
            handle=resolved.raft_agent_handle or resolved.raft_profile,
            executable=resolved.raft_cli_executable,
            state_dir=resolved.raft_state_dir,
            max_reply_chars=resolved.raft_max_reply_chars,
            bridge_poll_interval_ms=resolved.raft_bridge_poll_interval_ms,
        ),
    )


async def run_raft_worker(
    settings: Settings | None = None,
    *,
    once: bool = False,
    react: bool = False,
) -> RaftDrainResult | None:
    worker = build_raft_worker(settings, react=react)
    if once:
        return await worker.run_once()
    await worker.run_forever()
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run this application as a Raft External Agent.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Drain the current inbox once without starting the wake bridge",
    )
    parser.add_argument(
        "--react",
        action="store_true",
        help="Use the application's observable ReAct strategy",
    )
    arguments = parser.parse_args()
    try:
        result = asyncio.run(
            run_raft_worker(once=arguments.once, react=arguments.react)
        )
    except KeyboardInterrupt:
        return
    if result is not None:
        print(
            f"received={result.received} "
            f"handled={result.handled} skipped={result.skipped}"
        )


if __name__ == "__main__":
    main()
