"""Immutable Artifact contracts and the small Host-owned storage boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from base_agent.events import EventEmitter

_SAFE_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"artifact {field_name} must not be blank")
    if value != value.strip():
        raise ValueError(f"artifact {field_name} must not have surrounding whitespace")


@dataclass(frozen=True, slots=True)
class ArtifactSpec:
    """A logical output contract declared before a Run starts."""

    name: str
    media_type: str
    location: str | None = None
    required: bool = True
    max_bytes: int | None = None

    def __post_init__(self) -> None:
        _require_text(self.name, "name")
        _require_text(self.media_type, "media_type")
        if self.location is not None:
            _require_text(self.location, "location")
        if self.max_bytes is not None and self.max_bytes < 1:
            raise ValueError("artifact max_bytes must be positive when provided")


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Compatibility name for a Host-owned ArtifactSpec with a location."""

    name: str
    location: str
    media_type: str

    def __post_init__(self) -> None:
        _require_text(self.name, "name")
        _require_text(self.location, "location")
        _require_text(self.media_type, "media_type")

    def to_spec(self) -> ArtifactSpec:
        return ArtifactSpec(
            name=self.name,
            media_type=self.media_type,
            location=self.location,
        )


class ArtifactBindingStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ArtifactBinding:
    """Auditable outcome of binding one produced Artifact to an ArtifactSpec."""

    spec: ArtifactSpec
    artifact_id: str | None
    status: ArtifactBindingStatus
    stage: str
    run_id: str
    attempt: int
    detail: str = ""

    def __post_init__(self) -> None:
        _require_text(self.stage, "binding stage")
        _require_text(self.run_id, "binding run_id")
        if self.attempt < 1:
            raise ValueError("artifact binding attempt must be positive")
        if self.status is ArtifactBindingStatus.ACCEPTED and self.artifact_id is None:
            raise ValueError("accepted ArtifactBinding requires artifact_id")


@dataclass(frozen=True, slots=True)
class Artifact:
    """Immutable metadata for content committed to an ArtifactStore."""

    id: str
    name: str
    media_type: str
    size_bytes: int
    sha256: str
    created_at: datetime
    run_id: str
    agent_id: str
    flow_run_id: str | None = None
    invocation_id: str | None = None
    stage: str | None = None
    step_id: str | None = None
    attempt: int | None = None
    tool_call_id: str | None = None
    location: str | None = None
    source_artifact_ids: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.id, "id"),
            (self.name, "name"),
            (self.media_type, "media_type"),
            (self.run_id, "run_id"),
            (self.agent_id, "agent_id"),
        ):
            _require_text(value, field_name)
        if self.location is not None:
            _require_text(self.location, "location")
        if self.size_bytes < 0:
            raise ValueError("artifact size_bytes must be non-negative")
        if len(self.sha256) != 64:
            raise ValueError("artifact sha256 must be a hexadecimal SHA-256 digest")
        try:
            int(self.sha256, 16)
        except ValueError as exc:
            raise ValueError("artifact sha256 must be a hexadecimal SHA-256 digest") from exc
        if self.attempt is not None and self.attempt < 1:
            raise ValueError("artifact attempt must be positive when provided")
        if self.created_at.tzinfo is None:
            raise ValueError("artifact created_at must be timezone-aware")
        if len(set(self.source_artifact_ids)) != len(self.source_artifact_ids):
            raise ValueError("artifact source_artifact_ids must be unique")
        for source_id in self.source_artifact_ids:
            _require_text(source_id, "source_artifact_id")
            if source_id == self.id:
                raise ValueError("artifact cannot reference itself as a source")
        try:
            serialized_metadata = json.dumps(
                dict(self.metadata),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("artifact metadata must be JSON-serializable") from exc
        object.__setattr__(
            self,
            "metadata",
            _freeze_json_mapping(json.loads(serialized_metadata)),
        )


@runtime_checkable
class ArtifactStore(Protocol):
    """Persistence boundary; implementations own content and immutable metadata."""

    async def create(
        self,
        content: bytes,
        *,
        artifact: Artifact,
    ) -> Artifact: ...

    async def get(self, artifact_id: str) -> Artifact | None: ...

    async def read(self, artifact_id: str) -> bytes: ...

    async def list(self, *, run_id: str | None = None) -> tuple[Artifact, ...]: ...


class ArtifactNotFoundError(LookupError):
    pass


class ArtifactAccessError(PermissionError):
    pass


class InMemoryArtifactStore:
    """Process-local reference store used by tests and single-process applications."""

    def __init__(self) -> None:
        self._metadata: dict[str, Artifact] = {}
        self._content: dict[str, bytes] = {}

    async def create(self, content: bytes, *, artifact: Artifact) -> Artifact:
        if artifact.id in self._metadata:
            raise ValueError(f"artifact '{artifact.id}' already exists")
        payload = bytes(content)
        digest = hashlib.sha256(payload).hexdigest()
        if artifact.size_bytes != len(payload) or artifact.sha256 != digest:
            raise ValueError("artifact metadata does not match its content")
        self._metadata[artifact.id] = artifact
        self._content[artifact.id] = payload
        return artifact

    async def get(self, artifact_id: str) -> Artifact | None:
        return self._metadata.get(artifact_id)

    async def read(self, artifact_id: str) -> bytes:
        try:
            return self._content[artifact_id]
        except KeyError as exc:
            raise ArtifactNotFoundError(f"artifact '{artifact_id}' was not found") from exc

    async def list(self, *, run_id: str | None = None) -> tuple[Artifact, ...]:
        artifacts = tuple(self._metadata.values())
        if run_id is not None:
            artifacts = tuple(item for item in artifacts if item.run_id == run_id)
        return artifacts


class LocalArtifactStore:
    """Atomic filesystem adapter suitable for one Host process."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self._objects = self.root / "objects"
        self._metadata = self.root / "metadata"
        self._objects.mkdir(parents=True, exist_ok=True)
        self._metadata.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def create(self, content: bytes, *, artifact: Artifact) -> Artifact:
        payload = bytes(content)
        _validate_artifact_content(payload, artifact)
        artifact_id = _safe_artifact_id(artifact.id)
        async with self._lock:
            if await self.get(artifact_id) is not None:
                raise ValueError(f"artifact '{artifact_id}' already exists")
            await asyncio.to_thread(self._commit, payload, artifact)
        return artifact

    async def get(self, artifact_id: str) -> Artifact | None:
        metadata_path = self._metadata_path(artifact_id)
        if not metadata_path.exists():
            return None
        data = await asyncio.to_thread(metadata_path.read_text, encoding="utf-8")
        return _artifact_from_json(data)

    async def read(self, artifact_id: str) -> bytes:
        artifact = await self.get(artifact_id)
        if artifact is None:
            raise ArtifactNotFoundError(f"artifact '{artifact_id}' was not found")
        try:
            payload = await asyncio.to_thread(self._object_path(artifact_id).read_bytes)
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError(
                f"artifact '{artifact_id}' content was not found"
            ) from exc
        _validate_artifact_content(payload, artifact)
        return payload

    async def list(self, *, run_id: str | None = None) -> tuple[Artifact, ...]:
        paths = await asyncio.to_thread(
            lambda: tuple(sorted(self._metadata.glob("*.json")))
        )
        artifacts: list[Artifact] = []
        for path in paths:
            data = await asyncio.to_thread(path.read_text, encoding="utf-8")
            artifact = _artifact_from_json(data)
            if run_id is None or artifact.run_id == run_id:
                artifacts.append(artifact)
        artifacts.sort(key=lambda item: (item.created_at, item.id))
        return tuple(artifacts)

    def _commit(self, content: bytes, artifact: Artifact) -> None:
        object_path = self._object_path(artifact.id)
        metadata_path = self._metadata_path(artifact.id)
        _atomic_write(object_path, content)
        _atomic_write(
            metadata_path,
            json.dumps(
                _artifact_json(artifact),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
        )

    def _object_path(self, artifact_id: str) -> Path:
        return self._objects / f"{_safe_artifact_id(artifact_id)}.bin"

    def _metadata_path(self, artifact_id: str) -> Path:
        return self._metadata / f"{_safe_artifact_id(artifact_id)}.json"


class ArtifactManager:
    """Run-scoped, access-controlled facade injected into ToolContext."""

    def __init__(
        self,
        store: ArtifactStore,
        *,
        run_id: str,
        agent_id: str,
        emitter: EventEmitter | None = None,
        flow_run_id: str | None = None,
        invocation_id: str | None = None,
        stage: str | None = None,
        step_id: str | None = None,
        attempt: int | None = None,
        tool_call_id: str | None = None,
        granted_artifact_ids: tuple[str, ...] = (),
    ) -> None:
        self._store = store
        self.run_id = run_id
        self.agent_id = agent_id
        self._emitter = emitter
        self.flow_run_id = flow_run_id
        self.invocation_id = invocation_id
        self.stage = stage
        self.step_id = step_id
        self.attempt = attempt
        self.tool_call_id = tool_call_id
        self._granted_ids = tuple(dict.fromkeys(granted_artifact_ids))
        self._granted = frozenset(self._granted_ids)

    def for_tool(self, tool_call_id: str) -> ArtifactManager:
        return ArtifactManager(
            self._store,
            run_id=self.run_id,
            agent_id=self.agent_id,
            emitter=self._emitter,
            flow_run_id=self.flow_run_id,
            invocation_id=self.invocation_id,
            stage=self.stage,
            step_id=self.step_id,
            attempt=self.attempt,
            tool_call_id=tool_call_id,
            granted_artifact_ids=self._granted_ids,
        )

    async def create(
        self,
        *,
        name: str,
        media_type: str,
        content: bytes | str,
        location: str | None = None,
        source_artifact_ids: tuple[str, ...] = (),
        metadata: Mapping[str, Any] | None = None,
        max_bytes: int | None = None,
    ) -> Artifact:
        payload = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        if max_bytes is not None and len(payload) > max_bytes:
            raise ValueError(f"artifact exceeds {max_bytes} byte limit")
        for source_id in source_artifact_ids:
            await self._require_visible(source_id)
        artifact = Artifact(
            id=str(uuid4()),
            name=name,
            media_type=media_type,
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            created_at=datetime.now(UTC),
            run_id=self.run_id,
            agent_id=self.agent_id,
            flow_run_id=self.flow_run_id,
            invocation_id=self.invocation_id,
            stage=self.stage,
            step_id=self.step_id,
            attempt=self.attempt,
            tool_call_id=self.tool_call_id,
            location=location,
            source_artifact_ids=source_artifact_ids,
            metadata=metadata or {},
        )
        committed = await self._store.create(payload, artifact=artifact)
        if committed != artifact:
            raise ValueError("ArtifactStore changed immutable Artifact metadata")
        if self._emitter is not None:
            await self._emitter.emit(
                "artifact.created",
                artifact_id=committed.id,
                name=committed.name,
                media_type=committed.media_type,
                size_bytes=committed.size_bytes,
                sha256=committed.sha256,
                created_at=committed.created_at.isoformat(),
                producer_run_id=committed.run_id,
                producer_agent_id=committed.agent_id,
                flow_run_id=committed.flow_run_id,
                stage=committed.stage,
                location=committed.location,
                tool_call_id=committed.tool_call_id,
                source_artifact_ids=committed.source_artifact_ids,
            )
        return committed

    async def get(self, artifact_id: str) -> Artifact:
        return await self._require_visible(artifact_id)

    async def read(self, artifact_id: str) -> bytes:
        await self._require_visible(artifact_id)
        return await self._store.read(artifact_id)

    async def find(self, name: str) -> Artifact:
        visible = await self.list_visible()
        matches = [artifact for artifact in visible if artifact.name == name]
        if not matches:
            raise ArtifactNotFoundError(f"visible artifact '{name}' was not found")
        return matches[-1]

    async def read_named(self, name: str) -> bytes:
        return await self.read((await self.find(name)).id)

    async def list_outputs(self) -> tuple[Artifact, ...]:
        return await self._store.list(run_id=self.run_id)

    async def list_inputs(self) -> tuple[Artifact, ...]:
        artifacts: list[Artifact] = []
        for artifact_id in self._granted_ids:
            artifact = await self._store.get(artifact_id)
            if artifact is None:
                raise ArtifactNotFoundError(
                    f"granted artifact '{artifact_id}' was not found"
                )
            artifacts.append(artifact)
        return tuple(artifacts)

    async def validate_inputs(self) -> tuple[Artifact, ...]:
        artifacts = await self.list_inputs()
        if self._emitter is not None:
            for artifact in artifacts:
                await self._emitter.emit(
                    "artifact.granted",
                    artifact_id=artifact.id,
                    name=artifact.name,
                    producer_run_id=artifact.run_id,
                    sha256=artifact.sha256,
                )
        return artifacts

    async def list_visible(self) -> tuple[Artifact, ...]:
        own = await self.list_outputs()
        granted = await self.list_inputs()
        by_id = {artifact.id: artifact for artifact in (*granted, *own)}
        return tuple(by_id.values())

    async def _require_visible(self, artifact_id: str) -> Artifact:
        artifact = await self._store.get(artifact_id)
        if artifact is None:
            raise ArtifactNotFoundError(f"artifact '{artifact_id}' was not found")
        if artifact.run_id != self.run_id and artifact_id not in self._granted:
            raise ArtifactAccessError(f"artifact '{artifact_id}' is not granted to this Run")
        return artifact


def artifact_spec(value: ArtifactSpec | ArtifactRef) -> ArtifactSpec:
    """Normalize the old ArtifactRef contract at compatibility boundaries."""
    return value if isinstance(value, ArtifactSpec) else value.to_spec()


def _safe_artifact_id(artifact_id: str) -> str:
    if not _SAFE_ARTIFACT_ID.fullmatch(artifact_id):
        raise ValueError("artifact id contains unsafe filesystem characters")
    return artifact_id


def _validate_artifact_content(content: bytes, artifact: Artifact) -> None:
    digest = hashlib.sha256(content).hexdigest()
    if artifact.size_bytes != len(content) or artifact.sha256 != digest:
        raise ValueError("artifact metadata does not match its content")


def _artifact_json(artifact: Artifact) -> dict[str, Any]:
    return {
        "id": artifact.id,
        "name": artifact.name,
        "media_type": artifact.media_type,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
        "created_at": artifact.created_at.isoformat(),
        "run_id": artifact.run_id,
        "agent_id": artifact.agent_id,
        "flow_run_id": artifact.flow_run_id,
        "invocation_id": artifact.invocation_id,
        "stage": artifact.stage,
        "step_id": artifact.step_id,
        "attempt": artifact.attempt,
        "tool_call_id": artifact.tool_call_id,
        "location": artifact.location,
        "source_artifact_ids": list(artifact.source_artifact_ids),
        "metadata": _thaw_json(artifact.metadata),
    }


def _artifact_from_json(value: str) -> Artifact:
    try:
        data = json.loads(value)
        return Artifact(
            id=str(data["id"]),
            name=str(data["name"]),
            media_type=str(data["media_type"]),
            size_bytes=int(data["size_bytes"]),
            sha256=str(data["sha256"]),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            run_id=str(data["run_id"]),
            agent_id=str(data["agent_id"]),
            flow_run_id=_optional_json_string(data.get("flow_run_id")),
            invocation_id=_optional_json_string(data.get("invocation_id")),
            stage=_optional_json_string(data.get("stage")),
            step_id=_optional_json_string(data.get("step_id")),
            attempt=(int(data["attempt"]) if data.get("attempt") is not None else None),
            tool_call_id=_optional_json_string(data.get("tool_call_id")),
            location=_optional_json_string(data.get("location")),
            source_artifact_ids=tuple(
                str(item) for item in data.get("source_artifact_ids", ())
            ),
            metadata=dict(data.get("metadata", {})),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid Artifact metadata record") from exc


def _optional_json_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("optional Artifact metadata field must be a string")
    return value


def _atomic_write(path: Path, content: bytes) -> None:
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as temporary:
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def _freeze_json_mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("artifact metadata must be a JSON object")
    return MappingProxyType(
        {str(key): _freeze_json(item) for key, item in value.items()}
    )


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return _freeze_json_mapping(value)
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value
