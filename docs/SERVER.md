# Optional FastAPI Run Server

The Server adapter maps the framework-neutral Agent API onto HTTP and Server-Sent Events. FastAPI
and Uvicorn are optional dependencies:

```bash
uv add 'base-agent[server]'
```

Create an application from one configured Agent:

```python
from base_agent.server import create_app

app = create_app(agent)
```

Run it with an ASGI server:

```bash
uv run --extra server uvicorn examples.server_app:app
```

Importing `base_agent` does not import FastAPI. Only `base_agent.server` requires the Server extra.

## API

The default prefix is `/v1`:

| Method | Path | Behavior |
| --- | --- | --- |
| `POST` | `/v1/runs` | Start a background Run and return `202`. |
| `GET` | `/v1/runs/{run_id}` | Return the durable Run snapshot. |
| `POST` | `/v1/runs/{run_id}/cancel` | Request cooperative cancellation. |
| `POST` | `/v1/runs/{run_id}/resume` | Supply pending human input and continue. |
| `GET` | `/v1/runs/{run_id}/events` | Replay persisted events after an optional cursor. |
| `GET` | `/v1/runs/{run_id}/events/stream` | Replay and follow events over SSE. |
| `GET` | `/v1/runs/{run_id}/artifacts` | List Run Artifact references. |
| `GET` | `/v1/runs/{run_id}/artifacts/{artifact_id}` | Get one Run-owned Artifact reference. |

Start requests accept structured references, not inline binary payloads:

```json
{
  "prompt": "Inspect the uploaded report",
  "skills": ["report-analysis"],
  "attachment_ids": ["2d82ad67-f683-4993-97db-4310f19b15d8"]
}
```

The referenced Attachments must already exist in the Agent's ArtifactStore. Upload policy,
malware scanning, size limits, tenant ownership, and retention remain application concerns.

## Event streams

SSE frames use the runtime event sequence as both `id` and replay cursor:

```text
id: 5
event: tool.completed
data: {"id":"...","run_id":"...","sequence":5,...}
```

Reconnect using either `?after_sequence=5` or `Last-Event-ID: 5`. If both are supplied, the larger
cursor wins. A stream ends at COMPLETED, FAILED, CANCELLED, LIMIT_REACHED, or the current WAITING
boundary. A new stream after the WAITING cursor can follow events emitted by resume.

The configured EventStore must implement `EventStream`. History-only stores receive a clear `501`
instead of a simulated polling stream.

## Task lifecycle

`RunTaskManager` is owned by one FastAPI application; there is no process-global task registry. It
retains `RunHandle` objects while their background tasks are active. Resume operations are shielded
from HTTP client cancellation, so a disconnected request cannot consume a checkpoint and cancel
the resumed Run.

This manager does not make an asyncio Task durable across process restarts. A production service
that needs restart recovery or multiple workers must combine durable stores with an external job
runner and routing/lease strategy.

## Security

The adapter intentionally does not invent an authentication system. Place it behind application
authentication/authorization middleware and enforce tenant ownership before exposing it.

Artifact content download is disabled by default. Enable it only when the entire application is
protected and the Agent's ArtifactStore is correctly scoped:

```python
app = create_app(agent, expose_artifact_content=True)
```

The optional content endpoint sends `Content-Disposition: attachment` and
`X-Content-Type-Options: nosniff`, but those headers do not replace authorization, content scanning,
or data-loss prevention.
