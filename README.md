# base-agent

`base-agent` is a small, framework-agnostic Python runtime for building agents with models,
tools, skills, supervision, and observable runs.

The project is intentionally a core library rather than a complete agent product. It must run
without FastAPI, Redis, PostgreSQL, Docker, a browser, or a vector database. Those capabilities
belong in optional adapters and applications built on top of the core.

## Project status

Milestones 0 through 7 are complete. The core includes provider-neutral model contracts, typed
tools, observable runs, cooperative cancellation, versioned Skills, composable supervision,
offline examples, and focused component Harnesses. Milestone 8 is in progress: an optional
OpenAI-compatible model Provider is available, while generic runtime capabilities and
infrastructure integrations remain adapter work.

The generic Runtime supports background Run handles, cursor-based live events, cooperative
cancellation, checkpointed human-input suspension/resume, replaceable orchestration strategies,
and durable execution plans without a server or queue.

## Quick start

```bash
uv sync
uv run python examples/hello_agent.py
uv run python examples/tool_agent.py
uv run python examples/skill_agent/run.py
```

All examples are deterministic and offline. Start with the
[`Getting Started`](docs/GETTING_STARTED.md) guide, then continue with:

- [Writing Tools](docs/TOOLS.md)
- [Writing Skills](docs/SKILLS.md)
- [Testing Agents](docs/TESTING.md)
- [Background Runs and Events](docs/RUNS.md)
- [Orchestration Strategies and Plans](docs/ORCHESTRATION.md)
- [Execution-scoped Resources](docs/RESOURCES.md)
- [Attachments and Artifacts](docs/ARTIFACTS.md)
- [Optional Memory Retrieval](docs/MEMORY.md)
- [Optional FastAPI Run Server](docs/SERVER.md)
- [Optional PostgreSQL Persistence](docs/POSTGRES.md)
- [Optional Redis Event Notifications](docs/REDIS.md)
- [Model Providers](docs/PROVIDERS.md)
- [Reference Design Decisions](docs/REFERENCE_DESIGN.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)

Optional OpenAI-compatible support is installed separately:

```bash
uv add 'base-agent[openai]'
```

The HTTP/SSE adapter is also optional:

```bash
uv add 'base-agent[server]'
```

Durable PostgreSQL stores are installed separately as well:

```bash
uv add 'base-agent[postgres]'
```

Cross-process event notifications can optionally use Redis:

```bash
uv add 'base-agent[redis]'
```

## Design goals

- Run the first local example without external infrastructure.
- Define agents through composition instead of deep inheritance.
- Treat tools and skills as explicit, testable capabilities.
- Make every run observable through a stable event model.
- Support background execution and cursor-based live event observation without requiring a server.
- Support deterministic tests without calling a real model.
- Keep domain logic such as BI-WIKI and build-lineage outside the core.

## Non-goals

The core package does not provide a web UI, API server, distributed task queue, sandbox service,
browser automation, MCP server, or domain-specific agent. See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the boundary.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy src
uv build
```

## Roadmap

The incremental implementation plan and acceptance criteria live in
[`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md).

## License

A license has not been selected yet. Choose one before publishing or accepting external reuse.
