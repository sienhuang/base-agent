import asyncio
from uuid import UUID

import pytest

from base_agent import (
    Agent,
    AgentProfile,
    AgentResultStatus,
    ConversationStore,
    InMemoryConversationStore,
    MessageRole,
    ModelRequest,
    ModelResponse,
    RunStatus,
    ToolCall,
    WaitForInput,
    tool,
)
from base_agent.stores import ConversationBusyError, ConversationProfileMismatchError
from base_agent.testing import FakeModel


@pytest.mark.asyncio
async def test_runs_without_conversation_remain_independent() -> None:
    model = FakeModel([ModelResponse(content="first"), ModelResponse(content="second")])
    agent = Agent(
        profile=AgentProfile(id="standalone", instructions="Work."),
        model=model,
    )

    first = await agent.run("one")
    second = await agent.run("two")

    assert first.metadata["conversation_id"] is None
    assert second.metadata["conversation_id"] is None
    assert [message.content for message in model.requests[0].messages] == ["Work.", "one"]
    assert [message.content for message in model.requests[1].messages] == ["Work.", "two"]


@pytest.mark.asyncio
async def test_each_conversation_turn_is_a_linked_run_with_prior_history() -> None:
    model = FakeModel(
        [
            ModelResponse(content="Hello Xiao Ming."),
            ModelResponse(content="Your name is Xiao Ming."),
            ModelResponse(content="You prefer APAC."),
        ]
    )
    agent = Agent(
        profile=AgentProfile(id="chat", instructions="Remember prior turns."),
        model=model,
    )
    conversation = await agent.create_conversation(metadata={"tenant": "demo"})

    first = await agent.run("My name is Xiao Ming.", conversation_id=conversation.id)
    second = await agent.run("What is my name?", conversation_id=conversation.id)
    third = await agent.run("I prefer APAC.", conversation_id=conversation.id)

    turns = await agent.conversation_turns(conversation.id)
    messages = await agent.conversation_messages(conversation.id)
    stored = await agent.get_conversation(conversation.id)
    second_run = await agent.get_run(UUID(second.metadata["run_id"]))

    assert [turn.sequence for turn in turns] == [1, 2, 3]
    assert [turn.run_id for turn in turns] == [
        UUID(first.metadata["run_id"]),
        UUID(second.metadata["run_id"]),
        UUID(third.metadata["run_id"]),
    ]
    assert all(turn.status is RunStatus.COMPLETED for turn in turns)
    assert stored.version == 3
    assert stored.active_run_id is None
    assert second_run.conversation_id == conversation.id
    assert second_run.turn_sequence == 2
    assert second.metadata["conversation_id"] == str(conversation.id)
    assert second.metadata["turn_sequence"] == 2
    assert [message.role for message in model.requests[1].messages] == [
        MessageRole.SYSTEM,
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.USER,
    ]
    assert [message.content for message in model.requests[1].messages] == [
        "Remember prior turns.",
        "My name is Xiao Ming.",
        "Hello Xiao Ming.",
        "What is my name?",
    ]
    assert len(messages) == 6


class ControlledModel:
    name = "conversation-controlled"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        self.started.set()
        await self.release.wait()
        return ModelResponse(content="done")


@pytest.mark.asyncio
async def test_conversation_rejects_overlapping_active_runs() -> None:
    model = ControlledModel()
    agent = Agent(
        profile=AgentProfile(id="serial", instructions="Work."),
        model=model,
    )
    conversation = await agent.create_conversation()
    first = asyncio.create_task(agent.run("first", conversation_id=conversation.id))
    await model.started.wait()

    with pytest.raises(ConversationBusyError, match="already has active run"):
        await agent.run("overlap", conversation_id=conversation.id)

    model.release.set()
    assert (await first).status is AgentResultStatus.COMPLETED
    assert (await agent.get_conversation(conversation.id)).active_run_id is None


@pytest.mark.asyncio
async def test_task_interruption_releases_the_conversation_turn() -> None:
    model = ControlledModel()
    agent = Agent(
        profile=AgentProfile(id="interrupted-chat", instructions="Work."),
        model=model,
    )
    conversation = await agent.create_conversation()
    task = asyncio.create_task(
        agent.run("work", conversation_id=conversation.id)
    )
    await asyncio.wait_for(model.started.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    turns = await agent.conversation_turns(conversation.id)
    assert turns[0].status is RunStatus.INTERRUPTED
    assert (await agent.get_conversation(conversation.id)).active_run_id is None


@tool
async def ask_user(question: str) -> WaitForInput:
    """Request required information."""
    return WaitForInput(prompt=question)


@pytest.mark.asyncio
async def test_waiting_and_resume_stay_in_the_same_conversation_turn() -> None:
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="ask-1",
                        name="ask_user",
                        arguments={"question": "Which region?"},
                    ),
                )
            ),
            ModelResponse(content="Using APAC."),
            ModelResponse(content="Next turn completed."),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="interactive-chat",
            instructions="Ask when required.",
            tools=("ask_user",),
        ),
        model=model,
        tools=(ask_user,),
    )
    conversation = await agent.create_conversation()

    waiting = await agent.run("Build report", conversation_id=conversation.id)
    run_id = UUID(waiting.metadata["run_id"])
    active = await agent.get_conversation(conversation.id)

    assert waiting.status is AgentResultStatus.WAITING
    assert active.active_run_id == run_id
    with pytest.raises(ConversationBusyError):
        await agent.run("must wait", conversation_id=conversation.id)

    completed = await agent.resume(run_id, "APAC")
    next_result = await agent.run("Continue", conversation_id=conversation.id)
    turns = await agent.conversation_turns(conversation.id)

    assert completed.status is AgentResultStatus.COMPLETED
    assert next_result.status is AgentResultStatus.COMPLETED
    assert [turn.sequence for turn in turns] == [1, 2]
    assert turns[0].run_id == run_id
    assert turns[0].assistant_message == "Using APAC."
    assert (await agent.get_conversation(conversation.id)).active_run_id is None


@pytest.mark.asyncio
async def test_cancelling_a_waiting_run_releases_the_conversation() -> None:
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="ask-1",
                        name="ask_user",
                        arguments={"question": "Continue?"},
                    ),
                )
            ),
            ModelResponse(content="new turn"),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="cancel-chat",
            instructions="Ask.",
            tools=("ask_user",),
        ),
        model=model,
        tools=(ask_user,),
    )
    conversation = await agent.create_conversation()
    waiting = await agent.run("wait", conversation_id=conversation.id)
    run_id = UUID(waiting.metadata["run_id"])

    cancelled = await agent.cancel(run_id)
    next_result = await agent.run("new", conversation_id=conversation.id)
    turns = await agent.conversation_turns(conversation.id)

    assert cancelled.status is RunStatus.CANCELLED
    assert turns[0].status is RunStatus.CANCELLED
    assert next_result.status is AgentResultStatus.COMPLETED


@pytest.mark.asyncio
async def test_conversation_profile_and_history_limit_are_enforced() -> None:
    store = InMemoryConversationStore()
    owner_model = FakeModel(
        [
            ModelResponse(content="a1"),
            ModelResponse(content="a2"),
            ModelResponse(content="a3"),
        ]
    )
    owner = Agent(
        profile=AgentProfile(id="owner", instructions="Owner."),
        model=owner_model,
        conversation_store=store,
        conversation_history_limit=2,
    )
    conversation = await owner.create_conversation()
    await owner.run("u1", conversation_id=conversation.id)
    await owner.run("u2", conversation_id=conversation.id)
    await owner.run("u3", conversation_id=conversation.id)

    assert isinstance(store, ConversationStore)
    assert [message.content for message in owner_model.requests[2].messages] == [
        "Owner.",
        "u2",
        "a2",
        "u3",
    ]

    other = Agent(
        profile=AgentProfile(id="other", instructions="Other."),
        model=FakeModel([ModelResponse(content="unused")]),
        conversation_store=store,
    )
    with pytest.raises(ConversationProfileMismatchError):
        await other.run("intrude", conversation_id=conversation.id)
