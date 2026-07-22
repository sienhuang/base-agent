# Optional Memory Retrieval

Memory is an optional retrieval capability, not a mandatory Agent base class and not an automatic
string appended to the system prompt. The core defines:

- `MemoryRecord`: immutable text plus namespace and metadata;
- `MemoryQuery`: bounded, filterable search request;
- `MemoryMatch`: a record and normalized relevance score;
- `MemoryRetriever`: provider-neutral asynchronous search protocol;
- `InMemoryMemoryStore`: deterministic lexical implementation for local use and tests.

## Initial retrieval

Configure a Retriever when constructing an Agent:

```python
memory = InMemoryMemoryStore(
    (
        MemoryRecord(
            content="The customer prefers APAC reports.",
            namespace="customer-42",
        ),
    )
)

agent = Agent(
    profile=profile,
    model=model,
    memory_retriever=memory,
    memory_namespace="customer-42",
    memory_limit=5,
)
```

The Runtime performs one bounded retrieval before the first model step. Results enter
`ModelRequest.memories` as structured data. Provider adapters decide how to map them; adapters that
cannot map memory safely must reject it instead of silently dropping it. `OpenAIChatProvider`
currently raises `UnsupportedMemoryError`.

Memory retrieval is not repeated after WAITING. The checkpoint retains the selected matches and
resume restores the same model context. A durable checkpoint store therefore needs the same access
controls and encryption policy as conversation messages.

## Failure policy

The default `MemoryFailureMode.BEST_EFFORT` emits `memory.failed`, records the error summary, and
continues without matches. Use `MemoryFailureMode.REQUIRED` when execution must stop before the
model if retrieval is unavailable.

```python
agent = Agent(
    ...,
    memory_retriever=memory,
    memory_failure_mode=MemoryFailureMode.REQUIRED,
)
```

## Strategy and Tool retrieval

Strategies can issue additional queries with `services.memories.search(query)`. Tools receive the
same capability through the hidden `ToolContext`:

```python
@tool
async def recall(query: str, context: ToolContext) -> list[str]:
    matches = await context.memories.search(MemoryQuery(text=query, limit=3))
    return [match.record.content for match in matches]
```

The automatic `memory.retrieved` and `model.requested` events contain only record IDs and scores;
the initial memory body is not copied into Run or Result metadata. A Tool that deliberately returns
memory content creates a normal ToolResult, which follows normal Tool event/message observability.
Tool authors must redact or summarize sensitive content before returning it.

## Storage boundary

`InMemoryMemoryStore` uses token overlap and is intentionally not a semantic search engine.
Applications can implement `MemoryRetriever` over PostgreSQL, a vector database, a knowledge
service, or domain APIs. Embeddings, indexing, write-back, retention, tenant authorization, and
deletion policies remain outside the core retrieval loop.
