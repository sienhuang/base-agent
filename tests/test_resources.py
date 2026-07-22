import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import pytest

from base_agent import (
    Agent,
    AgentProfile,
    AgentResultStatus,
    AgentRuntime,
    EventType,
    ExecutionState,
    ModelResponse,
    ResourceFactory,
    ResourcePhase,
    ResourceSpec,
    RuntimeContext,
    RuntimeServices,
    ToolCall,
    ToolContext,
    WaitForInput,
    tool,
)
from base_agent.testing import FakeModel


class UsesResourcesStrategy:
    def __init__(self, names: tuple[str, ...]) -> None:
        self.names = names

    async def advance(self, context: RuntimeContext, services: RuntimeServices) -> None:
        values = [await services.resources.get(name, str) for name in self.names]
        context.output = ",".join(values)
        context.state_machine.transition_to(ExecutionState.COMPLETED)


@pytest.mark.asyncio
async def test_resources_are_acquired_once_and_released_in_reverse_in_same_task() -> None:
    lifecycle: list[tuple[str, int]] = []

    def resource(name: str) -> ResourceFactory:
        @asynccontextmanager
        async def factory(context: RuntimeContext) -> AsyncIterator[str]:
            del context
            task = asyncio.current_task()
            assert task is not None
            lifecycle.append((f"enter:{name}", id(task)))
            try:
                yield name
            finally:
                release_task = asyncio.current_task()
                assert release_task is not None
                lifecycle.append((f"exit:{name}", id(release_task)))

        return factory

    agent = Agent(
        profile=AgentProfile(id="resources", instructions="Use resources."),
        model=FakeModel([]),
        runtime=AgentRuntime(strategy=UsesResourcesStrategy(("first", "second", "first"))),
        resources=(
            ResourceSpec("first", resource("first")),
            ResourceSpec("second", resource("second")),
        ),
    )

    result = await agent.run("work")
    events = await agent.events(_run_id(result.metadata["run_id"]))

    assert result.output == "first,second,first"
    assert [item[0] for item in lifecycle] == [
        "enter:first",
        "enter:second",
        "exit:second",
        "exit:first",
    ]
    assert len({task_id for _, task_id in lifecycle}) == 1
    assert [event.type for event in events] == [
        EventType.RUN_CREATED,
        EventType.RUN_STARTED,
        EventType.RESOURCE_ACQUIRED,
        EventType.RESOURCE_ACQUIRED,
        EventType.RESOURCE_RELEASED,
        EventType.RESOURCE_RELEASED,
        EventType.RUN_COMPLETED,
    ]


@pytest.mark.asyncio
async def test_unused_lazy_resource_is_never_acquired() -> None:
    entered = False

    @asynccontextmanager
    async def unused(context: RuntimeContext) -> AsyncIterator[object]:
        nonlocal entered
        del context
        entered = True
        yield object()

    agent = Agent(
        profile=AgentProfile(id="lazy", instructions="Do not use it."),
        model=FakeModel([ModelResponse(content="done")]),
        resources=(ResourceSpec("unused", unused),),
    )

    result = await agent.run("work")

    assert result.status is AgentResultStatus.COMPLETED
    assert entered is False
    assert all(
        not event.type.value.startswith("resource.")
        for event in await agent.events(_run_id(result.metadata["run_id"]))
    )


@pytest.mark.asyncio
async def test_acquisition_failure_releases_prior_resources_and_fails_the_run() -> None:
    lifecycle: list[str] = []

    @asynccontextmanager
    async def healthy(context: RuntimeContext) -> AsyncIterator[str]:
        del context
        lifecycle.append("enter:healthy")
        try:
            yield "healthy"
        finally:
            lifecycle.append("exit:healthy")

    @asynccontextmanager
    async def broken(context: RuntimeContext) -> AsyncIterator[str]:
        del context
        lifecycle.append("enter:broken")
        raise RuntimeError("connection refused")
        yield "unreachable"  # pragma: no cover

    agent = Agent(
        profile=AgentProfile(id="acquire-failure", instructions="Work."),
        model=FakeModel([]),
        resources=(
            ResourceSpec("healthy", healthy, eager=True),
            ResourceSpec("broken", broken, eager=True),
        ),
    )

    result = await agent.run("work")
    events = await agent.events(_run_id(result.metadata["run_id"]))

    assert result.status is AgentResultStatus.FAILED
    assert "failed to acquire resource 'broken'" in (result.error or "")
    assert result.metadata["resource_failures"] == [
        {
            "name": "broken",
            "phase": ResourcePhase.ACQUIRE.value,
            "message": "connection refused",
        }
    ]
    assert lifecycle == ["enter:healthy", "enter:broken", "exit:healthy"]
    assert [event.type for event in events][-3:] == [
        EventType.RESOURCE_FAILED,
        EventType.RESOURCE_RELEASED,
        EventType.RUN_FAILED,
    ]


@pytest.mark.asyncio
async def test_cleanup_failure_is_reported_without_hiding_successful_output() -> None:
    @asynccontextmanager
    async def broken_cleanup(context: RuntimeContext) -> AsyncIterator[str]:
        del context
        yield "resource"
        raise RuntimeError("close failed")

    agent = Agent(
        profile=AgentProfile(id="cleanup-failure", instructions="Work."),
        model=FakeModel([]),
        runtime=AgentRuntime(strategy=UsesResourcesStrategy(("broken",))),
        resources=(ResourceSpec("broken", broken_cleanup),),
    )

    result = await agent.run("work")

    assert result.status is AgentResultStatus.COMPLETED
    assert result.output == "resource"
    assert result.metadata["resource_failures"] == [
        {"name": "broken", "phase": ResourcePhase.RELEASE.value, "message": "close failed"}
    ]
    run = await agent.get_run(_run_id(result.metadata["run_id"]))
    assert run.metadata["resource_failures"] == result.metadata["resource_failures"]


@tool
async def ask_user(question: str) -> WaitForInput:
    """Request information from a user."""
    return WaitForInput(prompt=question)


@pytest.mark.asyncio
async def test_waiting_releases_resources_and_resume_reacquires_them() -> None:
    lifecycle: list[str] = []

    @asynccontextmanager
    async def session(context: RuntimeContext) -> AsyncIterator[int]:
        lifecycle.append(f"enter:{context.step_count}")
        try:
            yield len(lifecycle)
        finally:
            lifecycle.append(f"exit:{context.step_count}")

    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="ask_user",
                        arguments={"question": "Continue?"},
                    ),
                )
            ),
            ModelResponse(content="done"),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="resource-resume",
            instructions="Ask.",
            tools=("ask_user",),
        ),
        model=model,
        tools=(ask_user,),
        resources=(ResourceSpec("session", session, eager=True),),
    )

    waiting = await agent.run("work")
    completed = await agent.resume(_run_id(waiting.metadata["run_id"]), "yes")
    events = await agent.events(_run_id(completed.metadata["run_id"]))

    assert waiting.status is AgentResultStatus.WAITING
    assert completed.status is AgentResultStatus.COMPLETED
    assert lifecycle == ["enter:0", "exit:1", "enter:1", "exit:2"]
    assert [event.type for event in events].count(EventType.RESOURCE_ACQUIRED) == 2
    assert [event.type for event in events].count(EventType.RESOURCE_RELEASED) == 2
    waiting_index = [event.type for event in events].index(EventType.RUN_WAITING)
    assert events[waiting_index - 1].type is EventType.RESOURCE_RELEASED
    assert events[-2].type is EventType.RESOURCE_RELEASED
    assert events[-1].type is EventType.RUN_COMPLETED


@pytest.mark.asyncio
async def test_task_cancellation_still_releases_acquired_resources() -> None:
    entered = asyncio.Event()
    released = asyncio.Event()
    strategy_started = asyncio.Event()

    @asynccontextmanager
    async def session(context: RuntimeContext) -> AsyncIterator[str]:
        del context
        entered.set()
        try:
            yield "session"
        finally:
            released.set()

    class BlockingStrategy:
        async def advance(
            self, context: RuntimeContext, services: RuntimeServices
        ) -> None:
            del context
            await services.resources.get("session", str)
            strategy_started.set()
            await asyncio.Event().wait()

    agent = Agent(
        profile=AgentProfile(id="task-cancel", instructions="Block."),
        model=FakeModel([]),
        runtime=AgentRuntime(strategy=BlockingStrategy()),
        resources=(ResourceSpec("session", session),),
    )
    task = asyncio.create_task(agent.run("work"))
    await strategy_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert entered.is_set()
    assert released.is_set()


def _run_id(value: object) -> UUID:
    return UUID(str(value))


@pytest.mark.asyncio
async def test_tool_context_connects_skills_and_tools_to_scoped_resources() -> None:
    lifecycle: list[str] = []
    task_ids: list[int] = []

    @asynccontextmanager
    async def browser(context: RuntimeContext) -> AsyncIterator[dict[str, str]]:
        del context
        task = asyncio.current_task()
        assert task is not None
        task_ids.append(id(task))
        lifecycle.append("browser:open")
        try:
            yield {"page": "example"}
        finally:
            close_task = asyncio.current_task()
            assert close_task is not None
            task_ids.append(id(close_task))
            lifecycle.append("browser:close")

    @tool
    async def page_title(url: str, context: ToolContext) -> str:
        """Read a page title using the scoped browser."""
        task = asyncio.current_task()
        assert task is not None
        task_ids.append(id(task))
        active_browser = await context.resources.get("browser", dict)
        return f"{active_browser['page']}:{url}"

    assert set(page_title.definition.input_schema["properties"]) == {"url"}
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="page_title",
                        arguments={"url": "https://example.com"},
                    ),
                )
            ),
            ModelResponse(content="finished"),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="resource-tool",
            instructions="Use the browser Tool.",
            tools=("page_title",),
        ),
        model=model,
        tools=(page_title,),
        resources=(ResourceSpec("browser", browser),),
    )

    result = await agent.run("inspect")

    assert result.status is AgentResultStatus.COMPLETED
    assert lifecycle == ["browser:open", "browser:close"]
    assert len(set(task_ids)) == 1
    assert "example:https://example.com" in (model.requests[1].messages[-1].content or "")
