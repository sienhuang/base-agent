# Getting Started

## Requirements

- Python 3.12 or newer;
- `uv` for the repository workflow.

No API key, database, queue, container runtime, or network service is required for the examples.

## Set up the repository

```bash
uv sync
uv run python examples/hello_agent.py
```

Expected output:

```text
Hello from base-agent!
```

## Create an Agent

An Agent composes a profile, model provider, optional Tools, optional Skills, stores, and a
Supervisor. It does not require subclassing.

```python
from base_agent import Agent, AgentProfile, ModelResponse
from base_agent.testing import FakeModel

agent = Agent(
    profile=AgentProfile(
        id="assistant",
        instructions="Answer clearly.",
    ),
    model=FakeModel([ModelResponse(content="Done")]),
)

result = await agent.run("Complete the task")
assert result.output == "Done"
```

`FakeModel` is intentionally part of the supported developer API. Use it to make application tests
deterministic before adding a real provider adapter.

## Continue across user Turns

Every Conversation Turn still uses the normal Run path:

```python
conversation = await agent.create_conversation()

first = await agent.run("My name is Xiao Ming.", conversation_id=conversation.id)
second = await agent.run("What is my name?", conversation_id=conversation.id)

assert first.metadata["run_id"] != second.metadata["run_id"]
assert second.metadata["turn_sequence"] == 2
```

See [Conversations and Run-backed Turns](CONVERSATIONS.md) for persistence, concurrency,
WAITING/resume, history limits, and HTTP.

## Start a background Run

```python
handle = await agent.start("Complete the task")

async for event in handle.stream():
    print(event.sequence, event.type)

result = await handle.result()
```

See [Background Runs and Events](RUNS.md) for cancellation and cursor replay.

## Execute a Plan

Pass a validated Plan or ask the same Run to create one:

```python
from base_agent import ExecutionPlan, PlanStep

plan = ExecutionPlan(
    id="report",
    title="Build report",
    steps=(
        PlanStep(id="inspect", description="Inspect the source"),
        PlanStep(
            id="publish",
            description="Publish the result",
            dependencies=("inspect",),
        ),
    ),
)

supplied = await agent.run("Build the report", plan=plan)
generated = await agent.run("Build the report", planning=True)
```

Both paths use the normal Run, Tool, Event, cancellation, WAITING/resume, and token-usage
infrastructure. After each completed Step, the Planner reviews the remaining work while the Runtime
preserves all executed Steps and results unchanged. An unchanged proposal does not mutate the Plan;
only a structural difference replaces pending Steps. Calling `agent.run(prompt)` without either
option retains the standard model → Tool loop.

Run the entire Agent as explicit ReAct while keeping the same Model/Tool runtime:

```python
from base_agent import AgentRuntime, ReActStrategy

agent = Agent(
    profile=profile,
    model=model,
    tools=tools,
    runtime=AgentRuntime(strategy=ReActStrategy()),
)
```

## Continue learning

- [Copy the application starter](../starter/README.md)
- [Writing Tools](TOOLS.md)
- [Writing Skills](SKILLS.md)
- [Testing Agents](TESTING.md)
- [Background Runs and Events](RUNS.md)
- [Conversations and Run-backed Turns](CONVERSATIONS.md)
- [Model Providers](PROVIDERS.md)
- [Orchestration and Plans](ORCHESTRATION.md)
- [ReAct on the shared Model/Tool loop](REACT.md)
- [Reference Design Decisions](REFERENCE_DESIGN.md)
- [Troubleshooting](TROUBLESHOOTING.md)
- [Architecture](ARCHITECTURE.md)
