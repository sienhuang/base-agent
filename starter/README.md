# Agent App Starter

This directory is a complete, copyable application built only from base-agent public APIs. It is
small enough to understand in one sitting but includes the boundaries most Agent projects need:

- one composition root in `agent_app/agent.py`;
- environment-backed Provider selection;
- a reusable offline Provider that exercises a real Model → Tool → Model loop;
- a typed, permissioned example Tool;
- a versioned Skill with an explicit allowlist;
- an asynchronous CLI and optional FastAPI entry point;
- deterministic Tool, Skill, Agent, and repeated-Run tests.

It contains no mock-manus, BI-WIKI, build-lineage, database, queue, browser, or Sandbox assumptions.

## Copy it

```bash
cp -R /path/to/base-agent/starter ./my-agent
cd ./my-agent
```

Rename `agent-app`, the `agent_app` Python package, and the `starter-agent` profile when establishing
the real application identity. Do not rename them merely to encode one Skill or Tool.

## Install during local base-agent development

Until base-agent is published to your package registry, point uv at a local checkout:

```bash
uv add --editable /absolute/path/to/base-agent
uv sync --group dev
```

After publication, the existing `base-agent>=0.1,<0.2` dependency works without a path source:

```bash
uv sync --group dev
```

There are no path dependencies in the copied template itself.

## Run offline

```bash
uv run agent-app "hello reusable agent"
```

Expected output:

```text
Offline starter completed the Tool loop: 3 words, 20 characters.
```

The offline Provider is intentionally reusable across Runs and makes no network calls.

## Test and check

```bash
uv run pytest
uv run ruff check .
uv run mypy src
```

## Use an OpenAI-compatible Provider

Install the optional Provider and set configuration outside source control:

```bash
uv sync --extra openai
export AGENT_PROVIDER=openai
export AGENT_MODEL=gpt-4.1-mini
export OPENAI_API_KEY='...'
uv run agent-app "Analyze this request"
```

Set `OPENAI_BASE_URL` for a compatible endpoint. `.env.example` documents variables, but the
starter does not automatically read `.env`; use your deployment secret/configuration system.

## Run the optional HTTP API

```bash
uv sync --extra server
uv run uvicorn agent_app.server:app --host 127.0.0.1 --port 8000
```

The Server exposes the standard Run, resume, cancellation, event, and Artifact endpoints. Add
application authentication and durable stores before exposing it outside a trusted development
environment.

## What to change first

1. Replace the profile id and instructions in `agent_app/agent.py`.
2. Replace `word_count` with small domain Tools; declare permissions for reads and side effects.
3. Replace `src/agent_app/skills/text-analysis/SKILL.md` with versioned domain procedures.
4. Keep the offline Provider for deterministic tests even after enabling a real Provider.
5. Add PostgreSQL, Redis, MCP, Sandbox, Browser, or Memory only when the application requires them.

## Composition rules

- Tools perform atomic actions; Skills describe multi-step procedures.
- Agent construction belongs in the composition root, not inside Tools or Skills.
- Infrastructure is injected through Store/Resource/Provider ports.
- Business-specific models stay in this application.
- Live infrastructure objects are never serialized into prompts or checkpoints.
- Secrets never belong in source, Skills, Run metadata, or committed `.env` files.
