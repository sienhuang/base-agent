# Raft External Agent adapter

`base_agent.integrations.raft` is an optional application adapter. It connects an
application-owned `Agent` to Raft without making Raft part of the core Runtime and without giving
the model direct access to Raft credentials.

## Responsibility boundary

```text
Raft                         base-agent application
------------------------     -----------------------------------
identity and membership      Agent profile and execution policy
messages and task board  ->  RaftWorker -> Agent.run()/resume()
claim and task status        Model, Tools, Skills, Memory, Artifact
wake-hint bridge             local Run/Event/Checkpoint stores
```

The Worker, not the model:

- starts the official content-free `raft agent bridge`;
- exposes a token-protected loopback wake endpoint;
- drains message bodies with the authenticated `raft message check`;
- ignores unaddressed channel traffic, self messages, system receipts, third-party app payloads,
  closed tasks, and tasks assigned to somebody else;
- claims an existing task before execution;
- sends bounded results through stdin, never through shell interpolation;
- moves successfully completed tasks to `in_review`;
- journals claim/reply/completion phases locally so a transport retry does not normally rerun the
  same message.

Raft remains an external hosted control plane. This adapter does not make the Raft server
self-hosted.

## Application composition

Install and authorize the published Raft CLI first:

```bash
npm install -g @botiverse/raft@latest
raft agent login start --server https://api.raft.build \
  --agent <external-agent-id> \
  --profile-slug <profile>

# Open the printed approval link. After the operator approves it:
raft agent login wait --server https://api.raft.build \
  --agent <external-agent-id> \
  --device-code <code-from-login-start> \
  --profile-slug <profile>
```

Then compose the Worker in the application:

```python
from uuid import UUID

from base_agent.integrations.raft import RaftWorker, RaftWorkerConfig

worker = RaftWorker(
    build_agent(),
    RaftWorkerConfig(
        profile="my-external-agent",
        agent_id=UUID("00000000-0000-0000-0000-000000000000"),
        handle="my-external-agent",
        executable="/absolute/path/to/raft",
    ),
)
await worker.run_forever()
```

The credential stays in the Raft/Slock profile directory. Do not copy it into `.env`, source,
Skills, prompts, Run metadata, or logs.

## Delivery behavior

The loopback endpoint implements `raft-channel-wake.v1`. Wake requests contain metadata only; the
endpoint rejects content-shaped fields. A successful wake queues an inbox drain and returns a
stable Runtime session identifier. The bridge owns wake reconnect, deduplication, retry, and
reconciliation.

Raft CLI `message check` acknowledges delivery before returning. To reduce the remaining crash
window, the Worker immediately writes returned canonical output to a mode-`0600` local spool. It
replays that spool before checking for more messages.

Only DMs, explicit `@handle` mentions, tasks assigned to this Agent, and replies to a locally
waiting Run are executed by default. Existing tasks are claimed before work begins. A failed claim
is treated as a concurrency lock and the Worker does not execute the task.

## Current limitations

- Raft External Agents and their CLI contract are still experimental.
- Raft's activity/online indicator can remain `inactive` while an External Agent bridge is
  connected and working; rely on bridge delivery and end-to-end message checks for health.
- The CLI currently returns canonical text rather than a versioned JSON message envelope, so the
  adapter has a strict parser and fails loudly on unknown output.
- Raft identity is transport identity, not backend data authorization. Tools must still enforce
  tenant and data permissions in their real backend.
- Attachment download/import and Artifact upload are not mapped yet.
- Replies are bounded. Oversized text stays in the local Run/Artifact plane and only a truncated
  response is sent to Raft.
- A waiting Run can resume while the same Worker and compatible CheckpointStore remain available.
  Production restart recovery requires a durable Run/Checkpoint/Conversation store.
