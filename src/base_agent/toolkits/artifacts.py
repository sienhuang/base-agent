"""Run-scoped attachment and Artifact tools."""

from uuid import UUID

from base_agent.tools import FunctionTool, ToolContext, tool


def artifact_tools(
    *,
    max_read_bytes: int = 1_000_000,
    max_write_bytes: int = 1_000_000,
) -> tuple[FunctionTool, ...]:
    """Build bounded text tools over the current Run's ArtifactManager."""
    if max_read_bytes < 1:
        raise ValueError("max_read_bytes must be greater than zero")
    if max_write_bytes < 1:
        raise ValueError("max_write_bytes must be greater than zero")

    @tool(name="list_attachments", permissions=frozenset({"artifact:read"}))
    async def list_attachments(context: ToolContext) -> list[dict[str, object]]:
        """List attachment metadata available to the current Run."""
        return [
            attachment.model_dump(mode="json")
            for attachment in context.artifacts.attachments
        ]

    @tool(name="read_attachment_text", permissions=frozenset({"artifact:read"}))
    async def read_attachment_text(
        attachment_id: UUID,
        context: ToolContext,
    ) -> dict[str, object]:
        """Read one UTF-8 text attachment available to the current Run."""
        content = await context.artifacts.read_attachment(attachment_id)
        return _text_payload(content, max_read_bytes=max_read_bytes)

    @tool(name="list_artifacts", permissions=frozenset({"artifact:read"}))
    async def list_artifacts(context: ToolContext) -> list[dict[str, object]]:
        """List metadata for Artifacts created by the current Run."""
        return [
            artifact.model_dump(mode="json")
            for artifact in context.artifacts.artifacts
        ]

    @tool(name="read_artifact_text", permissions=frozenset({"artifact:read"}))
    async def read_artifact_text(
        artifact_id: UUID,
        context: ToolContext,
    ) -> dict[str, object]:
        """Read one UTF-8 text Artifact owned by the current Run."""
        content = await context.artifacts.read_artifact(artifact_id)
        return _text_payload(content, max_read_bytes=max_read_bytes)

    @tool(name="create_text_artifact", permissions=frozenset({"artifact:write"}))
    async def create_text_artifact(
        name: str,
        content: str,
        context: ToolContext,
        media_type: str = "text/plain",
    ) -> dict[str, object]:
        """Create a text Artifact owned by the current Run."""
        encoded = content.encode("utf-8")
        if len(encoded) > max_write_bytes:
            raise ValueError(
                f"artifact content exceeds {max_write_bytes} UTF-8 bytes"
            )
        if not media_type.startswith("text/") and media_type != "application/json":
            raise ValueError("media_type must be text/* or application/json")
        artifact = await context.artifacts.create(
            name=name,
            media_type=media_type,
            content=encoded,
        )
        return artifact.model_dump(mode="json")

    return (
        list_attachments,
        read_attachment_text,
        list_artifacts,
        read_artifact_text,
        create_text_artifact,
    )


def _text_payload(content: bytes, *, max_read_bytes: int) -> dict[str, object]:
    if len(content) > max_read_bytes:
        raise ValueError(f"content exceeds {max_read_bytes} bytes")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("content is not valid UTF-8 text") from exc
    return {"content": text, "size_bytes": len(content)}
