"""Loopback implementation of Raft's content-free wake endpoint contract."""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

_MAX_WAKE_BYTES = 64 * 1024
_CONTENT_FIELDS = frozenset(
    {
        "body",
        "channel",
        "channelname",
        "content",
        "message",
        "messages",
        "preview",
        "sender",
        "senderid",
        "snippet",
        "text",
    }
)


class RaftWakeServer:
    """Accept authenticated, metadata-only wake notices on loopback."""

    def __init__(
        self,
        *,
        profile: str,
        agent_id: UUID,
    ) -> None:
        self.profile = profile
        self.agent_id = agent_id
        self.token = secrets.token_urlsafe(32)
        self.runtime_session = f"base-agent-{uuid4()}"
        self._event = asyncio.Event()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def endpoint(self) -> str:
        if self._server is None:
            raise RuntimeError("Raft wake server has not been started")
        raw_host, port = self._server.server_address[:2]
        host = raw_host.decode() if isinstance(raw_host, bytes) else raw_host
        return f"http://{host}:{port}/wake"

    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("Raft wake server is already running")
        loop = asyncio.get_running_loop()
        expected_profile = self.profile
        expected_agent_id = str(self.agent_id)
        expected_token = self.token
        runtime_session = self.runtime_session
        wake_event = self._event

        class WakeHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if urlsplit(self.path).path != "/activity/drain":
                    self._respond(HTTPStatus.NOT_FOUND, {"ok": False})
                    return
                if not self._authorized(expected_token):
                    self._respond(
                        HTTPStatus.UNAUTHORIZED,
                        {
                            "ok": False,
                            "failureClass": "auth_revoked",
                            "reason": "invalid activity token",
                        },
                    )
                    return
                self._respond(
                    HTTPStatus.OK,
                    {
                        "schema": "raft-activity-drain.v1",
                        "events": [],
                        "dropped": 0,
                    },
                )

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/wake":
                    self._respond(HTTPStatus.NOT_FOUND, {"ok": False})
                    return
                if not self._authorized(expected_token):
                    self._respond(
                        HTTPStatus.UNAUTHORIZED,
                        {
                            "ok": False,
                            "failureClass": "auth_revoked",
                            "reason": "invalid wake token",
                        },
                    )
                    return
                try:
                    content_length = int(self.headers.get("content-length", "0"))
                except ValueError:
                    content_length = 0
                if content_length < 1 or content_length > _MAX_WAKE_BYTES:
                    self._respond(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "ok": False,
                            "failureClass": "protocol_mismatch",
                            "reason": "invalid wake body size",
                        },
                    )
                    return
                try:
                    payload = json.loads(self.rfile.read(content_length))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._respond(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "ok": False,
                            "failureClass": "protocol_mismatch",
                            "reason": "wake body must be JSON",
                        },
                    )
                    return
                if not isinstance(payload, dict) or not _valid_wake_payload(
                    payload,
                    expected_profile=expected_profile,
                    expected_agent_id=expected_agent_id,
                ):
                    self._respond(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "ok": False,
                            "failureClass": "protocol_mismatch",
                            "reason": "wake payload does not match the configured agent",
                        },
                    )
                    return
                loop.call_soon_threadsafe(wake_event.set)
                self._respond(
                    HTTPStatus.OK,
                    {"ok": True, "runtimeSession": runtime_session},
                )

            def log_message(self, format: str, *args: object) -> None:
                return

            def _authorized(self, token: str) -> bool:
                supplied_token = self.headers.get("x-raft-bridge-token", "")
                return hmac.compare_digest(supplied_token, token)

            def _respond(
                self,
                status: HTTPStatus,
                payload: dict[str, object],
            ) -> None:
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), WakeHandler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"raft-wake-{self.profile}",
            daemon=True,
        )
        self._thread.start()

    async def wait(self) -> None:
        await self._event.wait()
        self._event.clear()

    async def close(self) -> None:
        if self._server is None:
            return
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        if thread is not None:
            await asyncio.to_thread(thread.join, 2)


def _valid_wake_payload(
    payload: dict[str, Any],
    *,
    expected_profile: str,
    expected_agent_id: str,
) -> bool:
    lowered_keys = {str(key).casefold() for key in payload}
    if lowered_keys & _CONTENT_FIELDS:
        return False
    if payload.get("schema") not in {None, "raft-channel-wake.v1"}:
        return False
    if payload.get("profile") != expected_profile:
        return False
    if payload.get("agentId") != expected_agent_id:
        return False
    return all(
        isinstance(payload.get(field), str) and bool(payload[field])
        for field in (
            "attemptId",
            "eventId",
            "messageId",
            "coreSessionId",
            "adapterInstance",
            "occurredAt",
        )
    )
