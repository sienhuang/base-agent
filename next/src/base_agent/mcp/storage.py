"""OAuth token storage shared by MCP control-plane and execution processes."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken


@runtime_checkable
class OAuthTokenStorage(Protocol):
    async def get_tokens(self) -> OAuthToken | None: ...

    async def set_tokens(self, tokens: OAuthToken) -> None: ...

    async def get_client_info(self) -> OAuthClientInformationFull | None: ...

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None: ...

    async def get_token_expiry_time(self) -> float | None: ...

    async def clear(self) -> None: ...


class InMemoryOAuthTokenStorage:
    """Process-local OAuth storage intended for tests and CLI sessions."""

    def __init__(self) -> None:
        self._tokens: OAuthToken | None = None
        self._client_info: OAuthClientInformationFull | None = None
        self._expires_at: float | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self._tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._tokens = tokens
        self._expires_at = _expires_at(tokens)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self._client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._client_info = client_info

    async def get_token_expiry_time(self) -> float | None:
        return self._expires_at

    async def clear(self) -> None:
        self._tokens = None
        self._client_info = None
        self._expires_at = None


class FileOAuthTokenStorage:
    """Atomic 0600 file storage suitable for one shared local deployment.

    The files are permission-restricted, not encrypted. Production hosts can
    implement :class:`OAuthTokenStorage` with a database or secret manager.
    """

    def __init__(self, root: str | Path, *, connection_name: str) -> None:
        if not connection_name or any(part in connection_name for part in ("/", "\\", "..")):
            raise ValueError("OAuth connection_name is not file-safe")
        self._root = Path(root) / connection_name
        self._tokens_path = self._root / "tokens.json"
        self._client_path = self._root / "client.json"

    async def get_tokens(self) -> OAuthToken | None:
        payload = await asyncio.to_thread(_read_json, self._tokens_path)
        if payload is None:
            return None
        raw = payload.get("tokens")
        return OAuthToken.model_validate(raw) if isinstance(raw, dict) else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        payload: dict[str, object] = {
            "tokens": tokens.model_dump(mode="json", exclude_none=True),
            "expires_at": _expires_at(tokens),
        }
        await asyncio.to_thread(_atomic_write_json, self._tokens_path, payload)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        payload = await asyncio.to_thread(_read_json, self._client_path)
        return OAuthClientInformationFull.model_validate(payload) if payload is not None else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        payload = client_info.model_dump(mode="json", exclude_none=True)
        await asyncio.to_thread(_atomic_write_json, self._client_path, payload)

    async def get_token_expiry_time(self) -> float | None:
        payload = await asyncio.to_thread(_read_json, self._tokens_path)
        if payload is None:
            return None
        value = payload.get("expires_at")
        if value is None:
            return None
        if not isinstance(value, int | float):
            raise ValueError("OAuth expires_at must be numeric")
        return float(value)

    async def clear(self) -> None:
        await asyncio.gather(
            asyncio.to_thread(_unlink, self._tokens_path),
            asyncio.to_thread(_unlink, self._client_path),
        )


def _expires_at(tokens: OAuthToken) -> float | None:
    return None if tokens.expires_in is None else time.time() + tokens.expires_in


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"OAuth storage file '{path}' must contain an object")
    return value


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _unlink(path: Path) -> None:
    path.unlink(missing_ok=True)
