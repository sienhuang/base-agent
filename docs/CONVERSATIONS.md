# Conversations and Run-backed Turns

Conversation support extends the normal Run API; it does not introduce a second chat runtime. A
Conversation is an ordered history, and every user Turn is executed as one independently observable
Run:

```text
Conversation
├── Turn 1 -> Run 1 -> Events / Tools / Artifacts
├── Turn 2 -> Run 2 -> Events / Tools / Artifacts
└── Turn 3 -> Run 3 -> Events / Tools / Artifacts
```

## Single-Run and multi-Turn use

Existing standalone Runs are unchanged:

```python
result = await agent.run("Analyze this report")
```

Create a Conversation and pass its ID through the same method:

```python
conversation = await agent.create_conversation(metadata={"tenant": "customer-42"})

first = await agent.run(
    "My name is Xiao Ming.",
    conversation_id=conversation.id,
)
second = await agent.run(
    "What is my name?",
    conversation_id=conversation.id,
)
```

`first` and `second` have different Run IDs. Their stored Runs share `conversation_id`, while
`turn_sequence` is respectively 1 and 2. The second Model request contains the completed user and
assistant messages from the first Turn.

## History boundary

Only completed user/assistant Turn messages enter subsequent Model context. Tool calls and Tool
results remain inside the Run that produced them, where they are available in the Result and Run
event history. This keeps protocol-specific execution details from growing every later Turn.

`conversation_history_limit` on `Agent` is an even message count and defaults to 40 (20 completed
Turns). The store retains the full history; the limit bounds what is copied into a new Run. This is
a deterministic message-count boundary, not token-aware compaction or summarization.

## WAITING, resume, and cancellation

A Run in `WAITING` remains the active Turn:

```python
waiting = await agent.run("Build report", conversation_id=conversation.id)
completed = await agent.resume(UUID(waiting.metadata["run_id"]), "APAC")
```

`resume()` continues the same Run and Turn. It does not create another Turn. The Conversation blocks
another `run(..., conversation_id=...)` until that Run completes, fails, is cancelled, or reaches
its execution limit. Cancelling a waiting Run releases the Conversation and records a cancelled
Turn.

## Concurrency and ownership

- a Conversation belongs to one `AgentProfile.id`;
- `begin_turn()` atomically allocates the next sequence and claims `active_run_id`;
- overlapping Runs raise `ConversationBusyError`;
- an Agent with another profile raises `ConversationProfileMismatchError`;
- in-memory locking and PostgreSQL row locking implement the same `ConversationStore` protocol.

The Conversation is released before a terminal Run snapshot/event is exposed, so clients that see
a terminal Run may safely start the next Turn.

## Persistence

`InMemoryConversationStore` is the default. To make history durable, inject a `PostgresStore` as the
Conversation Store together with the other durable ports:

```python
agent = Agent(
    profile=profile,
    model=model,
    run_store=postgres,
    event_store=postgres,
    checkpoint_store=postgres,
    artifact_store=postgres,
    conversation_store=postgres,
)
```

PostgreSQL uses `base_agent_conversations` and `base_agent_conversation_turns`. `create_schema()` is
still for local development only; production deployments need reviewed migrations.

## HTTP API

Create and inspect a Conversation:

```http
POST /v1/conversations
{"metadata":{"tenant":"customer-42"}}

GET /v1/conversations/{conversation_id}
GET /v1/conversations/{conversation_id}/turns
GET /v1/conversations/{conversation_id}/messages
```

Start a normal Run with an optional Conversation link:

```http
POST /v1/runs
{"prompt":"What is my name?","conversation_id":"..."}
```

The semantic alias uses the same `RunTaskManager` and `Agent.start()` path:

```http
POST /v1/conversations/{conversation_id}/runs
{"prompt":"What is my name?"}
```

## Deliberate limits

This release does not yet provide token-aware history compaction, automatic summarization, branch
or fork semantics, deletion/retention APIs, multi-part content, or distributed recovery of a task
that was executing during process failure. Those are separate policies above the core Run/Turn
relationship.

