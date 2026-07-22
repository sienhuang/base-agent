# Attachments and Artifacts

Attachments and Artifacts are immutable content references:

- an `Attachment` is stored input selected when a Run starts;
- an `Artifact` is stored output created by a Tool or orchestration strategy.

References contain an ID, display name, media type, byte size, SHA-256 checksum, timestamp, and
safe metadata. Binary content is held behind `ArtifactStore`; it is never serialized into a model
message, Run event, Result, or Runtime checkpoint. Download URLs and authorization remain an
application/API responsibility and are deliberately absent from core references.

## Adding input

```python
attachment = await agent.add_attachment(
    name="sales.csv",
    media_type="text/csv",
    content=csv_bytes,
)

result = await agent.run(
    "Inspect the uploaded data",
    attachments=(attachment,),
)
```

The Runtime verifies that every supplied Attachment exists in the configured store. Attachments
are available as structured `ModelRequest.attachments` and through `ToolContext.artifacts`.
Provider adapters decide how to map structured attachments to their API. An adapter must reject an
unsupported attachment instead of silently dropping it; the current `OpenAIChatProvider` does so
because generic Chat Completions cannot resolve core ArtifactStore IDs.

## Reading input and producing output

```python
from base_agent import Artifact, ToolContext, tool

@tool
async def build_report(attachment_id: UUID, context: ToolContext) -> Artifact:
    source = await context.artifacts.read_attachment(attachment_id)
    report = transform(source)
    return await context.artifacts.create(
        name="report.json",
        media_type="application/json",
        content=report,
        metadata={"source_attachment": str(attachment_id)},
    )
```

`ToolContext` remains hidden from the model-facing Tool schema. The Tool result contains only the
Artifact reference, allowing the model to reason about the output without copying its bytes into
conversation history.

Artifacts are immediately persisted and emit `artifact.created`. The final Artifact references
are available on `AgentResult.artifacts`, `Run.artifacts`, `Agent.list_artifacts(run_id)`, and the
ArtifactStore. Applications retrieve bytes with an authorized store/API operation such as
`Agent.read_content(artifact.id)`.

## Access and resume boundaries

- A Tool may read only Attachments declared on its current Run.
- A Tool may read only Artifacts belonging to its current Run.
- WAITING checkpoints retain references, not bytes.
- Resume requires the same logical ArtifactStore or another implementation backed by the same
  durable content.
- Metadata enters events and checkpoints, so it must not contain credentials or sensitive payloads.

`InMemoryArtifactStore` is intended for local execution and tests. Production applications can
implement the same `ArtifactStore` protocol over object storage, a database, or a content-addressed
service without changing Tools or orchestration strategies.
