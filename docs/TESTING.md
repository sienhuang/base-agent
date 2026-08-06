# Testing Agents

Tests should not rely on a real model, network timing, or mutable global state.

## Script model responses

```python
model = FakeModel(
    [
        ModelResponse(
            tool_calls=(ToolCall(id="1", name="lookup", arguments={"id": "42"}),)
        ),
        ModelResponse(content="Completed"),
    ]
)

result = await agent.run("Look up 42")
assert result.output == "Completed"
assert len(model.requests) == 2
```

`FakeModel.requests` exposes immutable request snapshots for assertions. Calling it after all
scripted responses are consumed raises `FakeModelExhaustedError`.

## Test a complete Agent Run

`AgentTestHarness` wraps an application-composed `Agent` that uses `FakeModel`. It calls the public
`Agent.run()` and `Agent.resume()` methods, then captures the evidence persisted by the real Runtime:

```python
from base_agent.testing import AgentTestHarness

harness = AgentTestHarness(agent)
episode = await harness.run("Look up 42")

assert episode.result.output == "Completed"
assert episode.run.status.value == "completed"
assert len(episode.model_requests) == 2
assert episode.event_types[-1].value == "run.completed"
```

`AgentTestRun` is an immutable snapshot containing:

- the public `AgentResult`;
- the persisted `Run`;
- ordered `RuntimeEvent` values;
- only the `ModelRequest` values issued by that Run.

The Harness does not implement a second execution loop or bypass application composition. Profile,
Tool, Skill, Strategy, Supervisor, Resource, Memory, Artifact, Store, WAITING, and resume behavior
therefore follow the same paths as a normal Agent.

For human-input scenarios, resume the same captured Run:

```python
waiting = await harness.run("Build the report")
assert waiting.result.status.value == "waiting"

completed = await harness.resume(waiting.run.id, "APAC")
assert completed.result.output == "Using APAC."
assert completed.run.id == waiting.run.id
```

The resumed snapshot contains cumulative Events and model requests for the whole Run. A single
Harness may execute multiple Runs; their model-request snapshots remain isolated.

`AgentTestHarness` intentionally requires `FakeModel`. Tests that intentionally exercise another
Provider should construct the Agent directly and use a dedicated Provider fake or adapter test.

## Test a complete Flow

`FlowTestHarness` composes the real in-memory Flow Repository, `FlowLifecycle`,
`SequentialFlowStrategy`, and `ScriptedAgentInvoker`. Applications provide only the
`FlowDefinition` and per-Agent outcomes:

```python
from base_agent import AgentResultStatus
from base_agent.testing import (
    FlowTestHarness,
    ScriptedAgentOutcome,
)

harness = FlowTestHarness(
    flow,
    {
        "researcher": (
            ScriptedAgentOutcome(
                status=AgentResultStatus.COMPLETED,
                output="Three sources support the conclusion.",
            ),
        ),
        "writer": (
            ScriptedAgentOutcome(
                status=AgentResultStatus.COMPLETED,
                output="Final report.",
            ),
        ),
    },
)

episode = await harness.run("Research and write the report.")

assert episode.result.output == "Final report."
assert episode.agent_keys == ("researcher", "writer")
assert episode.requests_for("writer")[0].input.handoff is not None
assert episode.event_types[-1].value == "flow.completed"
```

`FlowTestRun` is an immutable cumulative snapshot containing:

- the application-facing `FlowResult`;
- persisted `FlowRunState` and AgentInvocations;
- ordered Flow `RuntimeEvent` values;
- invocation, resume, and cancellation envelopes for only that Flow Run.

WAITING scenarios remain one tracked Flow:

```python
waiting = await harness.run("Build the report.")
completed = await harness.resume(waiting.result.run_id, "APAC")

assert len(completed.requests) == 2
assert len(completed.resumes) == 1
assert completed.event_types[-1].value == "flow.completed"
```

Use `harness.cancel(run_id, reason=...)` to assert parent/child cancellation evidence. Budget
termination is visible through `FlowResult.status` and the structured `budget` data on
`flow.limit_reached`.

The Harness does not implement another Flow loop. It exercises the same definition pinning,
handoff construction, revision/CAS, lifecycle events, budgets, WAITING/resume, and cancellation
propagation as the normal sequential strategy. It intentionally uses scripted Agents; tests for
the real Agent Runtime mapping should use `AgentRuntimeInvoker` with `FakeModel` Agents.

## Test components independently

- `ToolHarness` runs argument, permission, timeout, and result conversion paths.
- `SkillHarness` loads and validates a Skill against a real AgentProfile and ToolRegistry.
- `AgentTestHarness` runs a complete Agent and captures Result, Run, Events, and model requests.
- `FlowTestHarness` runs a complete sequential Flow and captures cumulative lifecycle evidence.
- `ScriptedAgentInvoker` remains available for lower-level Flow strategy tests.
- `InMemoryRunStore` and `InMemoryEventStore` let tests inspect status and ordered events.

## Repository quality gates

```bash
uv run pytest
uv run ruff check .
uv run mypy src
uv build
```

Warnings are treated as test failures.
