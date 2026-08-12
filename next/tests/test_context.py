from __future__ import annotations

import asyncio

import pytest

from base_agent import Agent, AgentDefinition, RunStatus, tool
from base_agent.context import (
    ContextBudget,
    ContextInput,
    ContextPolicy,
    ContextTrace,
    ContextView,
    KeepAllContextPolicy,
    SystemContext,
    SystemContextBlock,
)
from base_agent.memory import (
    MemoryKind,
    MemoryObservation,
    MemoryRecallRequest,
    MemoryRecord,
    MemoryScope,
    MemorySnapshot,
    MemoryTrust,
)
from base_agent.models import Message, ModelResponse, ToolCall
from base_agent.testing import FakeModel


def test_context_budget_calculates_request_input_capacity() -> None:
    budget = ContextBudget(
        context_window_tokens=10_000,
        reserved_output_tokens=2_000,
        safety_margin_tokens=500,
    )

    assert budget.max_input_tokens == 7_500
    assert ContextBudget().max_input_tokens is None


@pytest.mark.parametrize(
    "options",
    [
        {"context_window_tokens": 0},
        {"reserved_output_tokens": -1},
        {"safety_margin_tokens": -1},
        {"context_window_tokens": 100, "reserved_output_tokens": 100},
    ],
)
def test_context_budget_rejects_invalid_capacity(options: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        ContextBudget(**options)


def test_agent_rejects_unstable_context_policy_name() -> None:
    class InvalidNamePolicy:
        @property
        def name(self) -> str:
            return " unstable "

        def select(self, context: ContextInput, /) -> ContextView:
            raise AssertionError("must not run")

    with pytest.raises(ValueError, match="surrounding whitespace"):
        Agent(
            AgentDefinition(
                id="invalid-context-policy",
                version="1.0.0",
                instructions="Answer.",
            ),
            model=FakeModel.text("unused"),
            context_policy=InvalidNamePolicy(),
        )


def test_keep_all_policy_returns_the_source_transcript_unchanged() -> None:
    policy = KeepAllContextPolicy()
    transcript = (Message.user("hello"),)
    context = ContextInput(
        agent_id="test-agent",
        run_id="test-run",
        model="test-model",
        system_context=SystemContext("Answer."),
        transcript=transcript,
        tools=(),
        response_schema=None,
        turn=1,
        budget=ContextBudget(),
        memory=MemorySnapshot.disabled(),
    )

    view = policy.select(context)

    assert isinstance(policy, ContextPolicy)
    assert view.messages is transcript
    assert view.system_prompt == "Answer."
    assert view.trace == ContextTrace(
        policy="keep_all",
        source_system_prompt_fingerprint=context.system_prompt_fingerprint,
        selected_system_prompt_fingerprint=context.system_prompt_fingerprint,
        selected_system_block_ids=(),
        source_message_count=1,
        selected_message_count=1,
    )


def test_message_identity_preserves_existing_positional_constructor_order() -> None:
    call = ToolCall(id="call-1", name="lookup", arguments={})

    message = Message("assistant", "", (call,))

    assert message.tool_calls == (call,)
    assert message.message_id is None


def test_system_context_requires_declared_required_blocks() -> None:
    system = SystemContext(
        instructions="Core instructions.",
        blocks=(
            SystemContextBlock("required", "Required context.", required=True),
            SystemContextBlock("optional", "Optional context."),
        ),
    )

    assert system.render(("required",)) == "Core instructions.\n\nRequired context."
    with pytest.raises(ValueError, match="required system context blocks were omitted"):
        system.render(("optional",))
    with pytest.raises(ValueError, match="unknown system context blocks"):
        system.render(("required", "memory"))


async def test_context_policy_selects_declared_system_blocks_for_model_request() -> None:
    class RequiredOnlyPolicy:
        @property
        def name(self) -> str:
            return "required_system_only"

        def select(self, context: ContextInput, /) -> ContextView:
            selected_block_ids = ("required",)
            selected_system_prompt = context.system_context.render(selected_block_ids)
            return ContextView(
                system_prompt=selected_system_prompt,
                messages=context.transcript,
                trace=ContextTrace(
                    policy=self.name,
                    source_system_prompt_fingerprint=context.system_prompt_fingerprint,
                    selected_system_prompt_fingerprint=(
                        context.system_context.fingerprint(selected_block_ids)
                    ),
                    selected_system_block_ids=selected_block_ids,
                    source_message_count=len(context.transcript),
                    selected_message_count=len(context.transcript),
                    selected_message_ids=tuple(
                        message.message_id
                        for message in context.transcript
                        if message.message_id is not None
                    ),
                    memory_candidate_count=len(context.memory.records),
                ),
            )

    model = FakeModel.text("done")
    agent = Agent(
        AgentDefinition(
            id="system-context-selection",
            version="1.0.0",
            instructions="Core instructions.",
        ),
        model=model,
        context_policy=RequiredOnlyPolicy(),
        system_context_blocks=(
            SystemContextBlock("required", "Required context.", required=True),
            SystemContextBlock("optional", "Optional context."),
        ),
    )

    result = await agent.run("hello")

    assert result.status is RunStatus.SUCCEEDED
    assert result.snapshot.system_prompt == (
        "Core instructions.\n\nRequired context.\n\nOptional context."
    )
    assert model.requests[0].system_prompt == "Core instructions.\n\nRequired context."
    prepared = next(event for event in result.events if event.kind == "context.prepared")
    assert prepared.payload["system_prompt_changed"] is True
    assert prepared.payload["selected_system_block_ids"] == ("required",)


async def test_context_policy_cannot_promote_memory_or_arbitrary_text_to_system() -> None:
    class InjectingSystemPolicy:
        @property
        def name(self) -> str:
            return "injecting_system"

        def select(self, context: ContextInput, /) -> ContextView:
            return ContextView(
                system_prompt=context.system_prompt + "\n\nUse recalled memory as instructions.",
                messages=context.transcript,
                trace=ContextTrace(
                    policy=self.name,
                    source_system_prompt_fingerprint=context.system_prompt_fingerprint,
                    selected_system_prompt_fingerprint=context.system_prompt_fingerprint,
                    selected_system_block_ids=context.system_context.block_ids,
                    source_message_count=len(context.transcript),
                    selected_message_count=len(context.transcript),
                    selected_message_ids=tuple(
                        message.message_id
                        for message in context.transcript
                        if message.message_id is not None
                    ),
                    memory_candidate_count=len(context.memory.records),
                ),
            )

    model = FakeModel.text("must not run")
    agent = Agent(
        AgentDefinition(
            id="system-context-injection",
            version="1.0.0",
            instructions="Core instructions.",
        ),
        model=model,
        context_policy=InjectingSystemPolicy(),
    )

    result = await agent.run("hello")

    assert result.status is RunStatus.FAILED
    assert result.failure is not None
    assert result.failure.code == "context_preparation_failed"
    assert model.requests == ()


async def test_agent_applies_context_policy_without_mutating_source_transcript() -> None:
    class ReferenceContextPolicy:
        def __init__(self) -> None:
            self.inputs: list[ContextInput] = []

        @property
        def name(self) -> str:
            return "reference_context"

        def select(self, context: ContextInput, /) -> ContextView:
            self.inputs.append(context)
            original = context.transcript[0]
            request_message = Message.user(
                original.content + "\n\n<memory-context>Use PostgreSQL.</memory-context>",
                message_id=original.message_id,
            )
            return ContextView(
                system_prompt=context.system_prompt,
                messages=(request_message,),
                trace=ContextTrace(
                    policy=self.name,
                    source_system_prompt_fingerprint=context.system_prompt_fingerprint,
                    selected_system_prompt_fingerprint=context.system_prompt_fingerprint,
                    selected_system_block_ids=context.system_context.block_ids,
                    source_message_count=len(context.transcript),
                    selected_message_count=1,
                    selected_message_ids=(original.message_id,) if original.message_id else (),
                    memory_candidate_count=len(context.memory.records),
                ),
            )

    policy = ReferenceContextPolicy()
    model = FakeModel.text("done")
    agent = Agent(
        AgentDefinition(
            id="context-agent",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=model,
        context_policy=policy,
        context_budget=ContextBudget(
            context_window_tokens=8_000,
            reserved_output_tokens=1_000,
            safety_margin_tokens=500,
        ),
    )

    result = await agent.run("choose a database")

    assert result.status is RunStatus.SUCCEEDED
    source_message = policy.inputs[0].transcript[0]
    request_message = model.requests[0].messages[0]
    assert source_message.content == "choose a database"
    assert source_message.message_id is not None
    assert request_message.content == (
        "choose a database\n\n<memory-context>Use PostgreSQL.</memory-context>"
    )
    assert request_message.message_id == source_message.message_id
    assert result.snapshot.context_policy == "reference_context"
    assert result.snapshot.context_window_tokens == 8_000
    assert result.snapshot.context_reserved_output_tokens == 1_000
    assert result.snapshot.context_safety_margin_tokens == 500
    prepared = next(event for event in result.events if event.kind == "context.prepared")
    system_fingerprint = policy.inputs[0].system_prompt_fingerprint
    assert prepared.turn == 1
    assert prepared.payload == {
        "policy": "reference_context",
        "source_system_prompt_fingerprint": system_fingerprint,
        "selected_system_prompt_fingerprint": system_fingerprint,
        "system_prompt_changed": False,
        "selected_system_block_ids": (),
        "source_message_count": 1,
        "selected_message_count": 1,
        "omitted_message_count": 0,
        "omitted_tool_result_count": 0,
        "selected_message_ids": (source_message.message_id,),
        "omitted_message_ids": (),
        "memory_enabled": False,
        "memory_provider": "disabled",
        "memory_candidate_count": 0,
        "selected_memory_ids": (),
        "estimated_input_tokens": None,
        "max_input_tokens": 6_500,
    }


async def test_context_policy_runs_for_every_model_turn_with_full_source_transcript() -> None:
    class RecordingKeepAllPolicy:
        def __init__(self) -> None:
            self.inputs: list[ContextInput] = []

        @property
        def name(self) -> str:
            return "recording_keep_all"

        def select(self, context: ContextInput, /) -> ContextView:
            self.inputs.append(context)
            count = len(context.transcript)
            return ContextView(
                system_prompt=context.system_prompt,
                messages=context.transcript,
                trace=ContextTrace(
                    policy=self.name,
                    source_system_prompt_fingerprint=context.system_prompt_fingerprint,
                    selected_system_prompt_fingerprint=context.system_prompt_fingerprint,
                    selected_system_block_ids=context.system_context.block_ids,
                    source_message_count=count,
                    selected_message_count=count,
                    selected_message_ids=tuple(
                        message.message_id
                        for message in context.transcript
                        if message.message_id is not None
                    ),
                    memory_candidate_count=len(context.memory.records),
                ),
            )

    @tool
    async def lookup(value: str) -> str:
        return f"found:{value}"

    policy = RecordingKeepAllPolicy()
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="lookup-1",
                        name="lookup",
                        arguments={"value": "item"},
                    ),
                )
            ),
            ModelResponse(content="done"),
        ]
    )
    agent = Agent(
        AgentDefinition(
            id="multi-turn-context",
            version="1.0.0",
            instructions="Use the Tool.",
            tools=("lookup",),
            max_model_turns=2,
        ),
        model=model,
        tools=(lookup,),
        context_policy=policy,
    )

    result = await agent.run("find item")

    assert result.status is RunStatus.SUCCEEDED
    assert [len(context.transcript) for context in policy.inputs] == [1, 3]
    assert policy.inputs[0].tools
    assert policy.inputs[1].tools == ()
    prepared = [event for event in result.events if event.kind == "context.prepared"]
    assert [event.turn for event in prepared] == [1, 2]
    assert [event.payload["source_message_count"] for event in prepared] == [1, 3]


async def test_invalid_context_view_fails_closed_before_the_next_model_request() -> None:
    class OrphaningPolicy:
        @property
        def name(self) -> str:
            return "orphaning"

        def select(self, context: ContextInput, /) -> ContextView:
            if len(context.transcript) == 1:
                messages = context.transcript
            else:
                messages = (context.transcript[0], context.transcript[-1])
            return ContextView(
                system_prompt=context.system_prompt,
                messages=messages,
                trace=ContextTrace(
                    policy=self.name,
                    source_system_prompt_fingerprint=context.system_prompt_fingerprint,
                    selected_system_prompt_fingerprint=context.system_prompt_fingerprint,
                    selected_system_block_ids=context.system_context.block_ids,
                    source_message_count=len(context.transcript),
                    selected_message_count=len(messages),
                    omitted_message_count=len(context.transcript) - len(messages),
                    selected_message_ids=tuple(
                        message.message_id for message in messages if message.message_id is not None
                    ),
                    omitted_message_ids=tuple(
                        message.message_id
                        for message in context.transcript
                        if message not in messages and message.message_id is not None
                    ),
                    memory_candidate_count=len(context.memory.records),
                ),
            )

    @tool
    async def lookup(value: str) -> str:
        return value

    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="lookup-1",
                        name="lookup",
                        arguments={"value": "item"},
                    ),
                )
            ),
            ModelResponse(content="must not be requested"),
        ]
    )
    agent = Agent(
        AgentDefinition(
            id="invalid-context",
            version="1.0.0",
            instructions="Use the Tool.",
            tools=("lookup",),
            max_model_turns=2,
        ),
        model=model,
        tools=(lookup,),
        context_policy=OrphaningPolicy(),
    )

    result = await agent.run("find item")

    assert result.status is RunStatus.FAILED
    assert result.failure is not None
    assert result.failure.code == "context_preparation_failed"
    assert len(model.requests) == 1
    assert [event.kind for event in result.events][-2:] == [
        "context.failed",
        "run.failed",
    ]
    assert result.events[-2].turn == result.events[-1].turn == 2


async def test_scoped_memory_is_recalled_once_and_reused_across_model_turns() -> None:
    class StaticMemoryProvider:
        def __init__(self) -> None:
            self.requests: list[MemoryRecallRequest] = []

        @property
        def name(self) -> str:
            return "static-memory"

        @property
        def enabled(self) -> bool:
            return True

        async def recall(
            self,
            request: MemoryRecallRequest,
            /,
        ) -> tuple[MemoryRecord, ...]:
            self.requests.append(request)
            return (
                MemoryRecord(
                    id="memory-1",
                    kind=MemoryKind.SEMANTIC,
                    content="Use PostgreSQL.",
                    scope=request.scope,
                    trust=MemoryTrust.USER_ASSERTED,
                    relevance_score=0.9,
                ),
            )

    class MemoryContextPolicy:
        def __init__(self) -> None:
            self.inputs: list[ContextInput] = []

        @property
        def name(self) -> str:
            return "memory_context"

        def select(self, context: ContextInput, /) -> ContextView:
            self.inputs.append(context)
            memory = context.memory.records[0]
            original_user = context.transcript[0]
            request_user = Message.user(
                original_user.content
                + "\n\n<memory-context>"
                + memory.content
                + "</memory-context>",
                message_id=original_user.message_id,
            )
            messages = (request_user, *context.transcript[1:])
            return ContextView(
                system_prompt=context.system_prompt,
                messages=messages,
                trace=ContextTrace(
                    policy=self.name,
                    source_system_prompt_fingerprint=context.system_prompt_fingerprint,
                    selected_system_prompt_fingerprint=context.system_prompt_fingerprint,
                    selected_system_block_ids=context.system_context.block_ids,
                    source_message_count=len(context.transcript),
                    selected_message_count=len(messages),
                    selected_message_ids=tuple(
                        message.message_id for message in messages if message.message_id is not None
                    ),
                    memory_candidate_count=1,
                    selected_memory_ids=(memory.id,),
                ),
            )

    @tool
    async def lookup(value: str) -> str:
        return value

    provider = StaticMemoryProvider()
    policy = MemoryContextPolicy()
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="lookup-1",
                        name="lookup",
                        arguments={"value": "item"},
                    ),
                )
            ),
            ModelResponse(content="done"),
        ]
    )
    scope = MemoryScope(("tenant-1", "user-1"))
    agent = Agent(
        AgentDefinition(
            id="memory-context-agent",
            version="1.0.0",
            instructions="Answer.",
            tools=("lookup",),
            max_model_turns=2,
        ),
        model=model,
        tools=(lookup,),
        context_policy=policy,
        memory_provider=provider,
    )

    result = await agent.run("choose a database", memory_scope=scope)

    assert result.status is RunStatus.SUCCEEDED
    assert len(provider.requests) == 1
    assert provider.requests[0].scope == scope
    assert provider.requests[0].limit == 5
    assert len(policy.inputs) == 2
    assert policy.inputs[0].memory is policy.inputs[1].memory
    assert policy.inputs[1].transcript[0].content == "choose a database"
    assert all("Use PostgreSQL." in request.messages[0].content for request in model.requests)
    assert result.snapshot.memory_provider == "static-memory"
    assert result.snapshot.memory_recall_timeout_seconds == 2.0
    recalled = [event for event in result.events if event.kind == "memory.recalled"]
    assert len(recalled) == 1
    assert recalled[0].payload == {
        "provider": "static-memory",
        "candidate_count": 1,
    }
    prepared = [event for event in result.events if event.kind == "context.prepared"]
    assert [event.payload["selected_memory_ids"] for event in prepared] == [
        ("memory-1",),
        ("memory-1",),
    ]


async def test_enabled_memory_requires_scope_and_recall_failure_is_fail_open() -> None:
    class FailingMemoryProvider:
        @property
        def name(self) -> str:
            return "failing-memory"

        @property
        def enabled(self) -> bool:
            return True

        async def recall(
            self,
            request: MemoryRecallRequest,
            /,
        ) -> tuple[MemoryRecord, ...]:
            raise RuntimeError("memory unavailable")

    no_scope_model = FakeModel.text("unused")
    no_scope_agent = Agent(
        AgentDefinition(
            id="scoped-memory",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=no_scope_model,
        memory_provider=FailingMemoryProvider(),
    )
    with pytest.raises(ValueError, match="requires an explicit MemoryScope"):
        await no_scope_agent.run("hello")
    assert no_scope_model.requests == ()

    model = FakeModel.text("done")
    agent = Agent(
        AgentDefinition(
            id="failed-memory",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=model,
        memory_provider=FailingMemoryProvider(),
    )

    result = await agent.run("hello", memory_scope=MemoryScope(("user-1",)))

    assert result.status is RunStatus.SUCCEEDED
    assert [event.kind for event in result.events].count("memory.recall.failed") == 1
    assert "memory.recalled" not in [event.kind for event in result.events]
    prepared = next(event for event in result.events if event.kind == "context.prepared")
    assert prepared.payload["memory_enabled"] is True
    assert prepared.payload["memory_candidate_count"] == 0


async def test_memory_recall_timeout_is_fail_open() -> None:
    class SlowMemoryProvider:
        @property
        def name(self) -> str:
            return "slow-memory"

        @property
        def enabled(self) -> bool:
            return True

        async def recall(
            self,
            request: MemoryRecallRequest,
            /,
        ) -> tuple[MemoryRecord, ...]:
            await asyncio.Event().wait()
            return ()

    agent = Agent(
        AgentDefinition(
            id="slow-memory",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=FakeModel.text("done"),
        memory_provider=SlowMemoryProvider(),
        memory_recall_timeout_seconds=0.01,
    )

    result = await agent.run("hello", memory_scope=MemoryScope(("user-1",)))

    assert result.status is RunStatus.SUCCEEDED
    failed = next(event for event in result.events if event.kind == "memory.recall.failed")
    assert failed.payload["error_type"] == "TimeoutError"


async def test_message_ids_link_events_to_the_model_transcript() -> None:
    @tool
    async def lookup(value: str) -> str:
        return value

    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="lookup-1",
                        name="lookup",
                        arguments={"value": "item"},
                    ),
                )
            ),
            ModelResponse(content="done"),
        ]
    )
    agent = Agent(
        AgentDefinition(
            id="message-identity",
            version="1.0.0",
            instructions="Answer.",
            tools=("lookup",),
            max_model_turns=2,
        ),
        model=model,
        tools=(lookup,),
    )

    result = await agent.run("hello")

    created = next(event for event in result.events if event.kind == "message.created")
    completed = next(event for event in result.events if event.kind == "message.completed")
    second_request = model.requests[1]
    assert model.requests[0].messages[0].message_id == created.payload["message_id"]
    assert second_request.messages[0].message_id == created.payload["message_id"]
    assert second_request.messages[1].message_id == completed.payload["message_id"]
    assert second_request.messages[2].message_id is not None
    assert second_request.messages[2].tool_call_id == "lookup-1"


async def test_snapshot_fingerprint_covers_context_and_memory_configuration() -> None:
    class AlternateKeepAllPolicy:
        @property
        def name(self) -> str:
            return "alternate_keep_all"

        def select(self, context: ContextInput, /) -> ContextView:
            message_ids = tuple(
                message.message_id
                for message in context.transcript
                if message.message_id is not None
            )
            return ContextView(
                system_prompt=context.system_prompt,
                messages=context.transcript,
                trace=ContextTrace(
                    policy=self.name,
                    source_system_prompt_fingerprint=(context.system_prompt_fingerprint),
                    selected_system_prompt_fingerprint=(context.system_prompt_fingerprint),
                    selected_system_block_ids=context.system_context.block_ids,
                    source_message_count=len(context.transcript),
                    selected_message_count=len(context.transcript),
                    selected_message_ids=message_ids,
                    memory_candidate_count=len(context.memory.records),
                ),
            )

    class EmptyMemoryProvider:
        @property
        def name(self) -> str:
            return "empty-memory"

        @property
        def enabled(self) -> bool:
            return True

        async def recall(
            self,
            request: MemoryRecallRequest,
            /,
        ) -> tuple[MemoryRecord, ...]:
            return ()

    definition = AgentDefinition(
        id="snapshot-context-memory",
        version="1.0.0",
        instructions="Answer.",
    )
    scope = MemoryScope(("user-1",))
    baseline = await Agent(
        definition,
        model=FakeModel.text("done"),
    ).run("hello", memory_scope=scope)
    with_system_block = await Agent(
        definition,
        model=FakeModel.text("done"),
        system_context_blocks=(SystemContextBlock("application", "Tenant policy."),),
    ).run("hello", memory_scope=scope)
    with_context_policy = await Agent(
        definition,
        model=FakeModel.text("done"),
        context_policy=AlternateKeepAllPolicy(),
    ).run("hello", memory_scope=scope)
    with_memory = await Agent(
        definition,
        model=FakeModel.text("done"),
        memory_provider=EmptyMemoryProvider(),
        memory_recall_limit=3,
        memory_recall_timeout_seconds=0.5,
    ).run("hello", memory_scope=scope)

    fingerprints = {
        baseline.snapshot.fingerprint,
        with_system_block.snapshot.fingerprint,
        with_context_policy.snapshot.fingerprint,
        with_memory.snapshot.fingerprint,
    }
    assert len(fingerprints) == 4
    assert with_system_block.snapshot.system_context_block_ids == ("application",)
    assert with_context_policy.snapshot.context_policy == "alternate_keep_all"
    assert with_memory.snapshot.memory_provider == "empty-memory"
    assert with_memory.snapshot.memory_recall_limit == 3
    assert with_memory.snapshot.memory_recall_timeout_seconds == 0.5


async def test_memory_writer_receives_complete_success_transcript() -> None:
    class RecordingMemoryWriter:
        def __init__(self) -> None:
            self.observations: list[MemoryObservation] = []

        @property
        def name(self) -> str:
            return "recording-writer"

        @property
        def enabled(self) -> bool:
            return True

        def enqueue(self, observation: MemoryObservation, /) -> None:
            self.observations.append(observation)

    writer = RecordingMemoryWriter()
    model = FakeModel.text("done")
    agent = Agent(
        AgentDefinition(
            id="memory-writer",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=model,
        memory_writer=writer,
    )

    result = await agent.run("hello", memory_scope=MemoryScope(("user-1",)))

    assert result.status is RunStatus.SUCCEEDED
    assert len(writer.observations) == 1
    observation = writer.observations[0]
    assert [message.role for message in observation.messages] == ["user", "assistant"]
    assert [message.content for message in observation.messages] == ["hello", "done"]
    assert all(message.message_id is not None for message in observation.messages)
    assert observation.output == "done"
    assert result.snapshot.memory_writer == "recording-writer"
    kinds = [event.kind for event in result.events]
    assert kinds.index("memory.write.queued") < kinds.index("run.succeeded")
    queued = next(event for event in result.events if event.kind == "memory.write.queued")
    assert queued.payload == {"writer": "recording-writer", "message_count": 2}


async def test_memory_writer_is_not_called_for_failed_run() -> None:
    class RecordingMemoryWriter:
        def __init__(self) -> None:
            self.observations: list[MemoryObservation] = []

        @property
        def name(self) -> str:
            return "recording-writer"

        @property
        def enabled(self) -> bool:
            return True

        def enqueue(self, observation: MemoryObservation, /) -> None:
            self.observations.append(observation)

    writer = RecordingMemoryWriter()
    agent = Agent(
        AgentDefinition(
            id="failed-memory-write",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=FakeModel([]),
        memory_writer=writer,
    )

    result = await agent.run("hello", memory_scope=MemoryScope(("user-1",)))

    assert result.status is RunStatus.FAILED
    assert writer.observations == []
    assert not any(event.kind.startswith("memory.write.") for event in result.events)


async def test_memory_writer_failure_is_fail_open() -> None:
    class FailingMemoryWriter:
        @property
        def name(self) -> str:
            return "failing-writer"

        @property
        def enabled(self) -> bool:
            return True

        def enqueue(self, observation: MemoryObservation, /) -> None:
            raise RuntimeError("queue unavailable")

    agent = Agent(
        AgentDefinition(
            id="failed-memory-writer",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=FakeModel.text("done"),
        memory_writer=FailingMemoryWriter(),
    )

    result = await agent.run("hello", memory_scope=MemoryScope(("user-1",)))

    assert result.status is RunStatus.SUCCEEDED
    failed = next(event for event in result.events if event.kind == "memory.write.failed")
    assert failed.payload == {
        "writer": "failing-writer",
        "error_type": "RuntimeError",
    }
