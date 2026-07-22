import json
from uuid import UUID

import pytest

from base_agent import (
    Agent,
    AgentProfile,
    AgentResultStatus,
    EventType,
    InMemoryMemoryStore,
    MemoryFailureMode,
    MemoryMatch,
    MemoryQuery,
    MemoryRecord,
    MemoryRetriever,
    Message,
    ModelRequest,
    ModelResponse,
    OpenAIChatProvider,
    RuntimeCheckpoint,
    ToolCall,
    ToolContext,
    UnsupportedMemoryError,
    WaitForInput,
    tool,
)
from base_agent.testing import FakeModel


@pytest.mark.asyncio
async def test_in_memory_store_retrieves_deterministically_with_namespace_and_filters() -> None:
    records = (
        MemoryRecord(
            content="Refund policy allows thirty days",
            namespace="support",
            metadata={"region": "global"},
        ),
        MemoryRecord(
            content="Refund requests require an order number",
            namespace="support",
            metadata={"region": "global"},
        ),
        MemoryRecord(content="Refund policy for EU", namespace="legal"),
    )
    store = InMemoryMemoryStore(records)

    matches = await store.search(
        MemoryQuery(
            text="refund policy thirty days",
            namespace="support",
            filters={"region": "global"},
            limit=1,
        )
    )

    assert isinstance(store, MemoryRetriever)
    assert len(matches) == 1
    assert matches[0].record == records[0]
    assert matches[0].score == 1


@pytest.mark.asyncio
async def test_initial_memory_is_structured_in_model_request_but_redacted_from_events() -> None:
    secret = "Customer prefers APAC and internal code BLUE-42"
    record = MemoryRecord(content=secret, namespace="customer")
    model = FakeModel([ModelResponse(content="done")])
    agent = Agent(
        profile=AgentProfile(id="memory", instructions="Use relevant memory."),
        model=model,
        memory_retriever=InMemoryMemoryStore((record,)),
        memory_namespace="customer",
    )

    result = await agent.run("Which region does the customer prefer?")
    run_id = UUID(str(result.metadata["run_id"]))
    events = await agent.events(run_id)
    run = await agent.get_run(run_id)

    assert model.requests[0].memories[0].record == record
    assert result.metadata["memory"]["matches"][0]["id"] == str(record.id)
    assert run.metadata["memory"] == result.metadata["memory"]
    retrieved = next(event for event in events if event.type is EventType.MEMORY_RETRIEVED)
    assert retrieved.data["matches"] == [{"id": str(record.id), "score": 0.16666666666666666}]
    requested = next(event for event in events if event.type is EventType.MODEL_REQUESTED)
    assert requested.data["request"]["memories"] == retrieved.data["matches"]
    assert secret not in json.dumps(
        [event.model_dump(mode="json") for event in events], ensure_ascii=False
    )


class FailingRetriever:
    async def search(self, query: MemoryQuery) -> tuple[MemoryMatch, ...]:
        del query
        raise RuntimeError("memory backend unavailable")


@pytest.mark.asyncio
async def test_best_effort_memory_failure_is_observable_and_run_continues() -> None:
    model = FakeModel([ModelResponse(content="continued")])
    agent = Agent(
        profile=AgentProfile(id="best-effort", instructions="Continue."),
        model=model,
        memory_retriever=FailingRetriever(),
    )

    result = await agent.run("work")
    events = await agent.events(UUID(str(result.metadata["run_id"])))

    assert result.status is AgentResultStatus.COMPLETED
    assert result.output == "continued"
    assert result.metadata["memory"]["error"] == "memory backend unavailable"
    assert EventType.MEMORY_FAILED in [event.type for event in events]
    assert model.requests[0].memories == ()


@pytest.mark.asyncio
async def test_required_memory_failure_fails_before_model_execution() -> None:
    model = FakeModel([])
    agent = Agent(
        profile=AgentProfile(id="required", instructions="Require memory."),
        model=model,
        memory_retriever=FailingRetriever(),
        memory_failure_mode=MemoryFailureMode.REQUIRED,
    )

    result = await agent.run("work")
    events = await agent.events(UUID(str(result.metadata["run_id"])))

    assert result.status is AgentResultStatus.FAILED
    assert "memory backend unavailable" in (result.error or "")
    assert model.requests == ()
    assert [event.type for event in events][-2:] == [
        EventType.MEMORY_FAILED,
        EventType.RUN_FAILED,
    ]


class CountingRetriever:
    def __init__(self, match: MemoryMatch) -> None:
        self.match = match
        self.queries: list[MemoryQuery] = []

    async def search(self, query: MemoryQuery) -> tuple[MemoryMatch, ...]:
        self.queries.append(query)
        return (self.match,)


@tool
async def ask_user(question: str) -> WaitForInput:
    """Ask for required input."""
    return WaitForInput(prompt=question)


@pytest.mark.asyncio
async def test_waiting_checkpoint_retains_memory_without_retrieving_again_on_resume() -> None:
    match = MemoryMatch(
        record=MemoryRecord(content="Preferred region is APAC"),
        score=0.8,
    )
    retriever = CountingRetriever(match)
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
            id="memory-resume",
            instructions="Ask.",
            tools=("ask_user",),
        ),
        model=model,
        tools=(ask_user,),
        memory_retriever=retriever,
    )

    waiting = await agent.run("prepare report")
    run_id = UUID(str(waiting.metadata["run_id"]))
    checkpoint = await agent.checkpoint_store.load(run_id)
    restored = RuntimeCheckpoint.model_validate_json(checkpoint.model_dump_json())
    completed = await agent.resume(run_id, "yes")

    assert restored.memories == (match,)
    assert restored.memory_initialized is True
    assert len(retriever.queries) == 1
    assert model.requests[0].memories == model.requests[1].memories == (match,)
    assert completed.status is AgentResultStatus.COMPLETED


@tool
async def search_memory(query: str, context: ToolContext) -> tuple[MemoryMatch, ...]:
    """Search long-term memory on demand."""
    return await context.memories.search(MemoryQuery(text=query, limit=3))


@pytest.mark.asyncio
async def test_tool_can_search_memory_on_demand_through_hidden_context() -> None:
    record = MemoryRecord(content="Refund policy requires an order number")
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="search_memory",
                        arguments={"query": "refund order"},
                    ),
                )
            ),
            ModelResponse(content="Use the order number."),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="memory-tool",
            instructions="Search memory.",
            tools=("search_memory",),
        ),
        model=model,
        tools=(search_memory,),
        memory_retriever=InMemoryMemoryStore((record,)),
    )

    result = await agent.run("unrelated initial question")

    assert set(search_memory.definition.input_schema["properties"]) == {"query"}
    assert result.status is AgentResultStatus.COMPLETED
    assert "Refund policy" in (model.requests[1].messages[-1].content or "")
    events = await agent.events(UUID(str(result.metadata["run_id"])))
    assert [event.type for event in events].count(EventType.MEMORY_RETRIEVED) == 2


@pytest.mark.asyncio
async def test_openai_chat_adapter_rejects_unmapped_memories_explicitly() -> None:
    class UnusedClient:
        pass

    match = MemoryMatch(record=MemoryRecord(content="remember this"), score=1)
    provider = OpenAIChatProvider(model="test", client=UnusedClient())  # type: ignore[arg-type]

    with pytest.raises(UnsupportedMemoryError, match="does not map retrieved memories"):
        await provider.complete(
            ModelRequest(messages=(Message.user("work"),), memories=(match,))
        )
