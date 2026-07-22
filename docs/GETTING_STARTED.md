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

## Start a background Run

```python
handle = await agent.start("Complete the task")

async for event in handle.stream():
    print(event.sequence, event.type)

result = await handle.result()
```

See [Background Runs and Events](RUNS.md) for cancellation and cursor replay.

## Continue learning

- [Writing Tools](TOOLS.md)
- [Writing Skills](SKILLS.md)
- [Testing Agents](TESTING.md)
- [Background Runs and Events](RUNS.md)
- [Model Providers](PROVIDERS.md)
- [Reference Design Decisions](REFERENCE_DESIGN.md)
- [Troubleshooting](TROUBLESHOOTING.md)
- [Architecture](ARCHITECTURE.md)
