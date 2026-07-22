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

## Test components independently

- `ToolHarness` runs argument, permission, timeout, and result conversion paths.
- `SkillHarness` loads and validates a Skill against a real AgentProfile and ToolRegistry.
- `InMemoryRunStore` and `InMemoryEventStore` let tests inspect status and ordered events.

## Repository quality gates

```bash
uv run pytest
uv run ruff check .
uv run mypy src
uv build
```

Warnings are treated as test failures.
