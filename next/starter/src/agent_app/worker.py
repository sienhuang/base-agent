"""Independent PostgreSQL Worker for queued Agent and Flow executions."""

from __future__ import annotations

import argparse
import asyncio
import signal
from typing import cast

from base_agent import AwaitHumanConfirmations, LocalArtifactStore
from base_agent.server.worker import (
    AgentExecutionHandler,
    ExecutionHandler,
    ExecutionWorker,
    FlowExecutionHandler,
    default_python_child_command,
)

from agent_app.app import build_agent
from agent_app.config import Settings
from agent_app.events import build_event_store_runtime
from agent_app.provider import build_provider


async def run_worker(settings: Settings) -> None:
    if settings.execution_mode != "postgres-worker":
        raise ValueError("execution-worker requires AGENT_EXECUTION_MODE=postgres-worker")
    runtime = build_event_store_runtime(settings)
    await runtime.start()
    agent = build_agent(
        build_provider(settings),
        artifact_store=LocalArtifactStore(settings.artifact_root),
        event_store=runtime.store,
        checkpoint_store=runtime.checkpoints,
        confirmation_policy=AwaitHumanConfirmations(),
    )
    worker = ExecutionWorker(
        runtime.executions,
        handlers=(
            cast(
                ExecutionHandler,
                AgentExecutionHandler(
                    default_python_child_command("agent_app.execution_child")
                ),
            ),
            cast(
                ExecutionHandler,
                FlowExecutionHandler(
                    default_python_child_command("agent_app.flow_execution_child")
                ),
            ),
        ),
        runtime_route=settings.runtime_route,
        runtime_build_id=settings.runtime_build_id or agent.snapshot.fingerprint,
        lease_seconds=settings.run_lease_seconds,
        heartbeat_interval_seconds=settings.run_heartbeat_interval_seconds,
        poll_interval_seconds=settings.worker_poll_interval_seconds,
        reconcile_interval_seconds=settings.worker_reconcile_interval_seconds,
        max_concurrent_attempts=settings.worker_max_concurrency,
        child_env_remove=("AGENT_OPERATOR_TOKEN",),
        child_env_overrides={"AGENT_OPERATOR_TOKEN": "execution-child-no-operator-authority"},
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    task = asyncio.create_task(worker.run_forever(), name="base-agent-worker")
    stop_task = asyncio.create_task(stop.wait(), name="base-agent-worker-stop")
    try:
        done, _ = await asyncio.wait((task, stop_task), return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
        else:
            await worker.close()
            await task
    finally:
        if not stop_task.done():
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
        await worker.close()
        await runtime.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Starter Execution Worker.")
    parser.parse_args()
    asyncio.run(run_worker(Settings.from_env()))


if __name__ == "__main__":
    main()
