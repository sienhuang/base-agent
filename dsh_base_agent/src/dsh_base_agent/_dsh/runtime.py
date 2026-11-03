"""Official SDK adapter with explicit full-profile patch ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

from dsh_base_agent._dsh.protocol import (
    DshEventHandler,
    DshInput,
    DshRunResult,
    DshRuntime,
    RuntimeSpec,
)
from dsh_base_agent._dsh.version import PINNED_DSH_VERSION, verify_dsh_versions

ProfilePatchResolver = Callable[[RuntimeSpec, Path], tuple[str | Path, ...]]


@dataclass(frozen=True, slots=True)
class SdkRuntimeConfig:
    workspace: Path
    reasoning_effort: str | None = None
    max_tokens: int | None = None
    expected_version: str = PINNED_DSH_VERSION
    dsh_bin: str | None = None
    initialize_timeout_seconds: float = 30.0
    request_timeout_seconds: float | None = None
    shutdown_timeout_seconds: float | None = 5.0

    def __post_init__(self) -> None:
        if self.max_tokens is not None and self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if self.initialize_timeout_seconds <= 0:
            raise ValueError("initialize_timeout_seconds must be positive")
        if self.request_timeout_seconds is not None and self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if self.shutdown_timeout_seconds is not None and self.shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")


class SdkRuntimeFactory:
    """Build one Runtime per immutable Agent fingerprint.

    A patch resolver is intentionally required. Falling back to the SDK's
    implicit bundled composition would violate D-002 and could expose tools or
    omit the companion context seam.
    """

    def __init__(
        self,
        config: SdkRuntimeConfig,
        *,
        patch_resolver: ProfilePatchResolver,
    ) -> None:
        self._config = config
        self._patch_resolver = patch_resolver

    def create(self, spec: RuntimeSpec, *, dsh_home: Path) -> DshRuntime:
        verify_dsh_versions(self._config.expected_version)
        patches = tuple(
            str(Path(item).expanduser().resolve()) for item in self._patch_resolver(spec, dsh_home)
        )
        if not patches:
            raise RuntimeError(
                "the full sdk Profile requires an explicit Base Agent composition patch"
            )
        missing = [path for path in patches if not Path(path).is_file()]
        if missing:
            raise FileNotFoundError(f"DSH profile patches do not exist: {', '.join(missing)}")
        module = import_module("deepseek_harness")
        harness_type = module.DeepSeekHarness
        harness = harness_type(
            dsh_home=str(dsh_home),
            cwd=str(self._config.workspace.expanduser().resolve()),
            runtime_cwd=str(dsh_home.expanduser().resolve()),
            profile="sdk",
            patches=patches,
            provider=spec.provider,
            model=spec.model,
            reasoning_effort=spec.reasoning_effort or self._config.reasoning_effort,
            max_tokens=spec.max_tokens or self._config.max_tokens,
            base_url=spec.base_url,
            api_key=spec.api_key,
            dsh_bin=self._config.dsh_bin,
            initialize_timeout_seconds=self._config.initialize_timeout_seconds,
            request_timeout_seconds=self._config.request_timeout_seconds,
            shutdown_timeout_seconds=self._config.shutdown_timeout_seconds,
        )
        return _SdkRuntime(harness)


class _SdkRuntime:
    def __init__(self, harness: Any) -> None:
        self._harness = harness

    async def start(self) -> None:
        await asyncio.to_thread(self._harness.start)

    async def run(
        self,
        input: DshInput,
        *,
        session_id: str,
        on_event: DshEventHandler | None = None,
    ) -> DshRunResult:
        loop = asyncio.get_running_loop()
        notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        def receive(notification: Any) -> None:
            if notification.method != "session.event":
                return
            payload = notification.payload
            if payload.get("sessionId") != session_id:
                return
            event = payload.get("event")
            if isinstance(event, dict):
                loop.call_soon_threadsafe(notifications.put_nowait, dict(event))

        worker = asyncio.create_task(
            asyncio.to_thread(
                self._harness.run,
                input,
                session_id=session_id,
                on_notification=receive,
            )
        )
        try:
            while not worker.done():
                try:
                    event = await asyncio.wait_for(notifications.get(), timeout=0.05)
                except TimeoutError:
                    continue
                if on_event is not None:
                    await on_event(event)
            result = await worker
            while not notifications.empty():
                event = notifications.get_nowait()
                if on_event is not None:
                    await on_event(event)
            return DshRunResult(
                session_id=result.session_id,
                final_response=result.final_response,
                finish_reason=result.finish_reason,
                events=tuple(dict(item) for item in result.events),
            )
        finally:
            if not worker.done():
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker

    async def cancel(self, session_id: str) -> None:
        # The pinned SDK wire has no prompt-cancel method.  A Runtime is owned by
        # one immutable Agent and serializes Runs, so process teardown is the
        # only honest cancellation boundary at this pin.  BaseAgentApp evicts
        # this Runtime and creates a fresh one for a subsequent Run.
        del session_id
        await self.close()

    async def close(self) -> None:
        await asyncio.to_thread(self._harness.close)
