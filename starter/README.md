# Agent App Starter

This directory is a complete, copyable application built only from base-agent public APIs. It is
small enough to understand in one sitting but includes the boundaries most Agent projects need:

- one composition root in `agent_app/agent.py`;
- environment-backed Provider selection;
- a reusable offline Provider that exercises a real Model → Tool → Model loop;
- a typed, permissioned example Tool plus the safe built-in ToolKit;
- a versioned Skill with an explicit allowlist;
- an asynchronous CLI and optional FastAPI entry point;
- Run-backed multi-Turn Conversations through the same `Agent.run()` path;
- deterministic Tool, Skill, Agent, and repeated-Run tests.

It contains no mock-manus, BI-WIKI, build-lineage, database, queue, browser, or Sandbox assumptions.

## Copy it

```bash
mkdir ./my-agent
rsync -a \
  --exclude '.venv*' \
  --exclude '.uv-cache' \
  --exclude 'dist' \
  --exclude '.env' \
  /path/to/base-agent/starter/ ./my-agent/
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

Start an interactive Conversation:

```bash
uv run agent-app --chat --no-skill
```

Every user Turn creates a different Run linked to the same Conversation. Use `/exit` to stop.

Generate and execute a Plan inside one offline Run:

```bash
uv run agent-app --plan --no-skill "complete planned work"
```

Without `--plan`, the Starter retains the normal model → Tool → model behavior.
With `--plan`, each completed Step is preserved while the offline Planner replaces only remaining
work; the real Provider follows the same Planner/ReAct lifecycle.

Run the whole task with ReAct on the same Model/Tool loop:

```bash
uv run agent-app --react --no-skill "count this text"
```

`--react` emits observable ReAct iterations and requires a structured final result. It is mutually
exclusive with `--plan`; normal execution remains the default.

The Starter composition root explicitly enables rotating JSON Lines logs at
`logs/base-agent.log`. Core `Agent` construction does not configure file logging. Set
`BASE_AGENT_LOG_FILE` to choose another path and `BASE_AGENT_LOG_LEVEL` to change the default
`INFO` level. Prompts, model output, Tool arguments, and secrets are not logged.

Tool failures are printed by the CLI with their `run_id`, Tool name, call ID, error code, and
message. The file log records the failure at `WARNING` with a redacted, bounded `error_message`.

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

## Use a local Codex or Claude CLI Provider

```bash
uv run agent-app --provider codex-cli --no-skill "Analyze this request"
uv run agent-app --provider claude-cli --model sonnet --no-skill "Analyze this request"
```

The adapters start trusted local executables without a shell, in an empty temporary directory,
with bounded output and a five-minute timeout. Codex uses an ephemeral read-only sandbox; Claude
has its built-in Tools and session persistence disabled. Application Tool calls return through the
normal base-agent Runtime.

## Run the optional HTTP API

```bash
uv sync --extra server
uv run uvicorn agent_app.server:app --host 127.0.0.1 --port 8000
```

The Server exposes the standard Run, resume, cancellation, event, and Artifact endpoints. Add
application authentication and durable stores before exposing it outside a trusted development
environment.

## Run as a Raft External Agent

Raft remains an optional hosted control plane. The application owns the Agent Runtime and connects
through the authenticated Raft CLI:

```bash
export RAFT_PROFILE=my-external-agent
export RAFT_AGENT_ID=00000000-0000-0000-0000-000000000000
export RAFT_AGENT_HANDLE=my-external-agent
export RAFT_CLI_EXECUTABLE=/absolute/path/to/raft
uv run agent-app-raft-worker
```

Use `--once` to drain the current inbox without starting the long-lived wake bridge. The Worker
accepts DMs, explicit `@handle` mentions, assigned tasks, and replies to waiting Runs. It owns task
claims and moves successful tasks to `in_review`; the model never receives the Raft credential.
See the base-agent [`docs/RAFT.md`](../docs/RAFT.md) contract before production use.

## What to change first

1. Replace the profile id and instructions in `agent_app/agent.py`.
2. Replace `word_count` with small domain Tools; keep only the built-in Tools the profile needs.
3. Replace `src/agent_app/skills/text-analysis/SKILL.md` with versioned domain procedures.
4. Keep the offline Provider for deterministic tests even after enabling a real Provider.
5. Add PostgreSQL, Redis, MCP, Sandbox, Browser, or Memory only when the application requires them.

## Built-in tools

The Starter registers the dependency-free `basic_tools()` bundle and exposes bounded user-input,
calculation, date/time, workspace-read, attachment, and Artifact tools. Local workspace writes are
registered but not exposed by the Starter profile; opt in by adding `workspace_write_text` and the
`workspace:write` permission. `search_memory` is registered but not exposed until a retriever is
configured.

## Opt-in data and development capabilities

Coding uses a disposable Docker Sandbox and requires the optional dependency plus a preloaded
image:

```bash
uv sync --extra coding
docker pull python:3.12
uv run agent-app --provider codex-cli --no-skill \
  --coding --sandbox-image python:3.12 \
  "Write and execute Python to calculate a confidence interval"
```

Web Search uses the Brave adapter:

```bash
export BRAVE_SEARCH_API_KEY='...'
uv run agent-app --provider codex-cli --no-skill --web-search \
  "Find and cite the current documentation for this API"
```

The company data path uses the installed `mtbi-cli`. Metadata is resolved through `meta`; bounded
read-only SQL is executed through OneSQL:

```bash
uv run agent-app --provider codex-cli --no-skill \
  --mtbi --mtbi-engine PRESTO \
  "Inspect the relevant table metadata and summarize daily metrics"
```

Equivalent environment settings are `AGENT_ENABLE_CODING`, `AGENT_SANDBOX_IMAGE`,
`AGENT_ENABLE_WEB_SEARCH`, `BRAVE_SEARCH_API_KEY`, `AGENT_ENABLE_MTBI`,
`AGENT_MTBI_CLI_EXECUTABLE`, `AGENT_MTBI_ENGINE`, and `AGENT_MTBI_REGION`. Enabling a bundle adds its
Tool names and permissions explicitly during Starter composition; none is enabled by default.

## Composition rules

- Tools perform atomic actions; Skills describe multi-step procedures.
- Agent construction belongs in the composition root, not inside Tools or Skills.
- Infrastructure is injected through Store/Resource/Provider ports.
- Business-specific models stay in this application.
- Live infrastructure objects are never serialized into prompts or checkpoints.
- Secrets never belong in source, Skills, Run metadata, or committed `.env` files.
