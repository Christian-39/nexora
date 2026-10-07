"""ASGI boundary for the production Security Relay.

The relay secret authenticates the trusted network hop for both HTTP and
WebSockets. It is not a user credential and is never copied into Django's
request headers. The relay overwrites forwarded metadata before adding this
header; untrusted requests are rejected before Django/Channels can process
cookies, authorization, or application events.
"""

from __future__ import annotations

import hmac

from django.conf import settings

_RELAY_HEADER = b"x-nexora-relay-token"
_FORWARDED_PROTO = b"x-forwarded-proto"


class SecurityRelayBoundary:
    """Require a constant-time shared-secret check at the ASGI network edge."""

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _header_values(scope, name: bytes) -> list[bytes]:
        return [value for key, value in scope.get("headers", ()) if key.lower() == name]

    @staticmethod
    async def _reject_http(send, *, status: int) -> None:
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-length", b"0"), (b"cache-control", b"no-store")],
        })
        await send({"type": "http.response.body", "body": b""})

    @staticmethod
    async def _reject_websocket(send) -> None:
        await send({"type": "websocket.close", "code": 4403})

    async def __call__(self, scope, receive, send):
        protocol = scope.get("type")
        if protocol not in {"http", "websocket"}:
            return await self.app(scope, receive, send)

        required = bool(getattr(settings, "SECURITY_RELAY_REQUIRED", False))
        expected = str(getattr(settings, "SECURITY_RELAY_TOKEN", "") or "")
        values = self._header_values(scope, _RELAY_HEADER)

        if required:
            if not expected:
                if protocol == "http":
                    return await self._reject_http(send, status=503)
                return await self._reject_websocket(send)
            if len(values) != 1:
                if protocol == "http":
                    return await self._reject_http(send, status=404)
                return await self._reject_websocket(send)
            try:
                provided = values[0].decode("ascii")
            except UnicodeDecodeError:
                provided = ""
            if not hmac.compare_digest(provided, expected):
                if protocol == "http":
                    return await self._reject_http(send, status=404)
                return await self._reject_websocket(send)

        # Only the relay's single fixed proto header is trusted. Reject an
        # authenticated-but-misconfigured hop rather than falling back to
        # scheme inference from untrusted network metadata.
        proto = None
        if required:
            forwarded_proto = self._header_values(scope, _FORWARDED_PROTO)
            if len(forwarded_proto) != 1:
                if protocol == "http":
                    return await self._reject_http(send, status=404)
                return await self._reject_websocket(send)
            try:
                proto = forwarded_proto[0].decode("ascii").lower()
            except UnicodeDecodeError:
                proto = ""
            if proto not in {"http", "https"}:
                if protocol == "http":
                    return await self._reject_http(send, status=404)
                return await self._reject_websocket(send)

        # The raw token is removed before the scope reaches Django or Channels.
        safe_scope = dict(scope)
        safe_scope["headers"] = [
            (key, value) for key, value in scope.get("headers", ()) if key.lower() != _RELAY_HEADER
        ]
        if required and proto:
            # Uvicorn proxy-header parsing is disabled in this deployment;
            # this authenticated relay hop is the sole scheme authority.
            safe_scope["scheme"] = "wss" if protocol == "websocket" and proto == "https" else (
                "ws" if protocol == "websocket" else proto
            )
        return await self.app(safe_scope, receive, send)
