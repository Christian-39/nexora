"""Nexora Security Relay: a constrained, network-boundary ASGI reverse proxy.

The public Render Python service accepts only configured HTTP/WebSocket hosts
and Nexora API/socket paths. It has one fixed private upstream; it is not an
open forward proxy. HTTP bodies are streamed without buffering, cookies and
Authorization pass through, and all forwarding metadata is overwritten or
removed before the shared relay token is injected.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import unquote_to_bytes, urlsplit

import httpx
import websockets

logger = logging.getLogger("nexora.security_relay")

_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
_HOP_BY_HOP = {
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"proxy-connection",
    b"te",
    b"trailer",
    b"transfer-encoding",
    b"upgrade",
}
_UNTRUSTED_FORWARDING = {
    b"forwarded",
    b"x-forwarded-for",
    b"x-forwarded-host",
    b"x-forwarded-port",
    b"x-forwarded-proto",
    b"x-forwarded-ssl",
    b"x-forwarded-client-cert",
    b"x-real-ip",
    b"x-original-url",
    b"x-rewrite-url",
}
_WEBSOCKET_HANDSHAKE = {
    b"sec-websocket-key",
    b"sec-websocket-version",
    b"sec-websocket-protocol",
    b"sec-websocket-extensions",
}
# Keep the relay, Django middleware, and browser ApiError parser on one bounded
# correlation-id grammar so the ID seen by a user is the one in backend logs.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_CLIENT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SAFE_HOST = re.compile(r"^[a-zA-Z0-9.-]{1,253}$")
_SAFE_DNS_LABEL = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")
_SAFE_SECRET = re.compile(r"^[A-Za-z0-9_+./=-]{32,512}$")


def _valid_dns_host(host: str) -> bool:
    return (
        bool(_SAFE_HOST.fullmatch(host))
        and all(_SAFE_DNS_LABEL.fullmatch(label) for label in host.rstrip(".").split("."))
    )


@dataclass(frozen=True)
class RelayConfig:
    upstream_host: str
    upstream_port: int
    upstream_scheme: str
    public_hosts: tuple[str, ...]
    public_scheme: str
    shared_token: str
    max_body_bytes: int = 2_164_260_864  # 2 GiB + multipart metadata allowance
    connect_timeout_seconds: float = 10.0
    write_timeout_seconds: float = 600.0
    read_timeout_seconds: float = 600.0

    @classmethod
    def from_env(cls, environ=None) -> "RelayConfig":
        source = os.environ if environ is None else environ
        upstream_host = str(source.get("UPSTREAM_HOST", "")).strip().lower()
        upstream_port_raw = str(source.get("UPSTREAM_PORT", "")).strip()
        upstream_scheme = str(source.get("UPSTREAM_SCHEME", "http")).strip().lower()
        configured_public_hosts = (
            source.get("PUBLIC_HOSTS")
            or source.get("PUBLIC_HOST")
            or source.get("RENDER_EXTERNAL_HOSTNAME")
            or ""
        )
        public_hosts = tuple(
            sorted(
                {
                    host.strip().lower().rstrip(".")
                    for host in str(configured_public_hosts).split(",")
                    if host.strip()
                }
            )
        )
        public_scheme = str(source.get("PUBLIC_SCHEME", "https")).strip().lower()
        shared_token = str(source.get("SECURITY_RELAY_TOKEN", source.get("RELAY_AUTH_TOKEN", ""))).strip()
        max_body_raw = str(source.get("MAX_BODY_BYTES", "2164260864")).strip()

        is_private_dns_name = upstream_host.endswith(".internal") or "." not in upstream_host
        if (
            not _valid_dns_host(upstream_host)
            or upstream_host.startswith(".")
            or upstream_host.endswith(".")
            or upstream_host == "localhost"
            or not is_private_dns_name
        ):
            raise ValueError("UPSTREAM_HOST must be one configured private Render service DNS host")
        if not upstream_port_raw.isdigit() or not (1 <= int(upstream_port_raw) <= 65535):
            raise ValueError("UPSTREAM_PORT must be a valid TCP port")
        if upstream_scheme not in {"http", "https"}:
            raise ValueError("UPSTREAM_SCHEME must be http or https")
        if not public_hosts or any(not _valid_dns_host(host) for host in public_hosts):
            raise ValueError("PUBLIC_HOSTS must contain one or more exact DNS hostnames")
        if public_scheme != "https":
            raise ValueError("PUBLIC_SCHEME must be https")
        if not _SAFE_SECRET.fullmatch(shared_token):
            raise ValueError("SECURITY_RELAY_TOKEN must be a generated 32-512 character secret")
        if not max_body_raw.isdigit() or not (1 <= int(max_body_raw) <= 2**63 - 1):
            raise ValueError("MAX_BODY_BYTES must be a positive integer")

        return cls(
            upstream_host=upstream_host,
            upstream_port=int(upstream_port_raw),
            upstream_scheme=upstream_scheme,
            public_hosts=public_hosts,
            public_scheme=public_scheme,
            shared_token=shared_token,
            max_body_bytes=int(max_body_raw),
        )

    @property
    def upstream_base(self) -> str:
        return f"{self.upstream_scheme}://{self.upstream_host}:{self.upstream_port}"


class BodyLimitExceeded(Exception):
    pass


class RelayConfigurationUnavailable(Exception):
    pass


def _pairs(scope) -> list[tuple[bytes, bytes]]:
    return list(scope.get("headers", ()))


def _values(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> list[bytes]:
    return [value for key, value in headers if key.lower() == name]


def _request_id(headers: Iterable[tuple[bytes, bytes]]) -> str:
    values = _values(headers, b"x-request-id")
    if len(values) == 1:
        try:
            candidate = values[0].decode("ascii").strip()
        except UnicodeDecodeError:
            candidate = ""
        if _SAFE_ID.fullmatch(candidate):
            return candidate
    return str(uuid.uuid4())


def _validated_public_host(headers: Iterable[tuple[bytes, bytes]], config: RelayConfig) -> str | None:
    values = _values(headers, b"host")
    if len(values) != 1:
        return None
    try:
        raw = values[0].decode("ascii").strip()
        parsed = urlsplit(f"//{raw}")
        hostname = (parsed.hostname or "").lower().rstrip(".")
        # No userinfo, path, query, fragment, or unusual authority syntax.
        if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            return None
        if parsed.port is not None and not (1 <= parsed.port <= 65535):
            return None
    except (UnicodeDecodeError, ValueError):
        return None
    return hostname if hostname in config.public_hosts else None


def _is_internal_host(host: str, config: RelayConfig) -> bool:
    normalized = host.lower().rstrip(".").lstrip(".")
    if (
        normalized == config.upstream_host
        or normalized == "localhost"
        or normalized.endswith((".internal", ".local", ".localhost", ".svc"))
        or "." not in normalized
    ):
        return bool(normalized)
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local or address.is_reserved


def _allowed_path(path: str) -> bool:
    lowered = path.lower()
    if (
        not path.startswith("/")
        or "\\" in path
        or "\x00" in path
        or "//" in path
        or any(token in lowered for token in ("%2f", "%5c", "%2e"))
    ):
        return False
    if any(segment in {".", ".."} for segment in path.split("/")):
        return False
    return (
        path.startswith("/api/")
        or path.startswith("/ws/")
        or path in {"/health/live/", "/health/ready/", "/relay-health"}
    )


def _raw_path(scope) -> str:
    value = scope.get("raw_path")
    decoded_path = scope.get("path", "/")
    if value is not None:
        try:
            path = value.decode("ascii")
            normalized = unquote_to_bytes(path).decode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            raise ValueError("invalid path encoding") from None
        if normalized != decoded_path:
            raise ValueError("raw and decoded paths disagree")
    else:
        path = str(decoded_path)
    lowered = path.lower()
    if any(token in lowered for token in ("%2f", "%5c", "%2e")):
        raise ValueError("ambiguous encoded path")
    if not _allowed_path(str(decoded_path)):
        raise ValueError("path not allowed")
    return path


def _connection_tokens(headers: Iterable[tuple[bytes, bytes]]) -> set[bytes]:
    tokens: set[bytes] = set()
    for value in _values(headers, b"connection"):
        for token in value.split(b","):
            token = token.strip().lower()
            if token:
                tokens.add(token)
    return tokens


def _safe_request_headers(
    headers: Iterable[tuple[bytes, bytes]],
    config: RelayConfig,
    request_id: str,
    public_host: str,
    *,
    websocket: bool = False,
) -> list[tuple[str, str]]:
    source = list(headers)
    remove = _HOP_BY_HOP | _UNTRUSTED_FORWARDING | {b"host", b"x-nexora-relay-token", b"x-request-id"}
    remove |= _connection_tokens(source)
    if websocket:
        remove |= _WEBSOCKET_HANDSHAKE

    result: list[tuple[str, str]] = []
    for key, value in source:
        lowered = key.lower()
        if (
            lowered in remove
            or lowered.startswith(b"x-forwarded-")
            or lowered.startswith(b"x-original-")
            or lowered.startswith(b"x-rewrite-")
        ):
            continue
        if lowered == b"x-nexora-client-id":
            try:
                client_id = value.decode("ascii").strip()
            except UnicodeDecodeError:
                continue
            if not _SAFE_CLIENT_ID.fullmatch(client_id):
                continue
            result.append(("x-nexora-client-id", client_id))
            continue
        try:
            name = lowered.decode("ascii")
            text = value.decode("latin-1")
        except UnicodeDecodeError:
            continue
        result.append((name, text))

    # Fixed relay-controlled metadata. No user-supplied XFF/Real-IP survives.
    result.extend(
        [
            ("host", public_host),
            ("x-forwarded-host", public_host),
            ("x-forwarded-proto", config.public_scheme),
            ("x-nexora-relay-token", config.shared_token),
            ("x-request-id", request_id),
        ]
    )
    return result


def _strip_internal_cookie_domain(value: bytes, config: RelayConfig) -> bytes:
    def replace(match):
        raw_domain = match.group(1).strip().strip(b'"').lstrip(b".").decode("latin-1").lower()
        return b"" if _is_internal_host(raw_domain, config) else match.group(0)

    return re.sub(rb";\s*domain\s*=\s*([^;]+)", replace, value, flags=re.IGNORECASE)


class SecurityRelay:
    """HTTP + WebSocket reverse proxy with fixed upstream and fail-closed rules."""

    def __init__(self, config: RelayConfig | None = None, *, http_client: httpx.AsyncClient | None = None):
        self.config = config
        self._client = http_client
        self._owns_client = http_client is None

    def _get_config(self) -> RelayConfig:
        if self.config is not None:
            return self.config
        try:
            self.config = RelayConfig.from_env()
        except (ValueError, TypeError) as exc:
            raise RelayConfigurationUnavailable from exc
        return self.config

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            config = self._get_config()
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=config.connect_timeout_seconds,
                    read=config.read_timeout_seconds,
                    write=config.write_timeout_seconds,
                    pool=config.connect_timeout_seconds,
                ),
                limits=httpx.Limits(max_connections=500, max_keepalive_connections=64),
                follow_redirects=False,
                trust_env=False,
            )
        return self._client

    async def __call__(self, scope, receive, send):
        protocol = scope.get("type")
        if protocol == "lifespan":
            return await self._lifespan(receive, send)
        if protocol == "http":
            return await self._http(scope, receive, send)
        if protocol == "websocket":
            return await self._websocket(scope, receive, send)
        return

    async def _lifespan(self, receive, send):
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    self._get_config()
                    self._get_client()
                except RelayConfigurationUnavailable:
                    await send({"type": "lifespan.startup.failed", "message": "relay configuration unavailable"})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                if self._owns_client and self._client is not None:
                    await self._client.aclose()
                    self._client = None
                await send({"type": "lifespan.shutdown.complete"})
                return

    @staticmethod
    async def _respond(send, status: int, body: bytes = b"", headers: Iterable[tuple[bytes, bytes]] = ()):
        outgoing = list(headers)
        if not any(key.lower() == b"content-length" for key, _ in outgoing):
            outgoing.append((b"content-length", str(len(body)).encode("ascii")))
        await send({"type": "http.response.start", "status": status, "headers": outgoing})
        await send({"type": "http.response.body", "body": body, "more_body": False})

    async def _safe_error(self, send, status: int, code: str, message: str, request_id: str):
        body = json.dumps(
            {"success": False, "message": message, "code": code, "request_id": request_id},
            separators=(",", ":"),
        ).encode("utf-8")
        await self._respond(
            send,
            status,
            body,
            [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"cache-control", b"no-store"),
                (b"x-request-id", request_id.encode("ascii")),
            ],
        )

    async def _body_stream(self, receive, limit: int):
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > limit:
                raise BodyLimitExceeded
            if chunk:
                yield chunk
            if not message.get("more_body", False):
                break

    async def _http(self, scope, receive, send):
        headers = _pairs(scope)
        request_id = _request_id(headers)
        try:
            config = self._get_config()
        except RelayConfigurationUnavailable:
            await self._safe_error(send, 503, "RELAY_UNAVAILABLE", "The service is temporarily unavailable.", request_id)
            return
        try:
            path = _raw_path(scope)
        except ValueError:
            await self._safe_error(send, 404, "NOT_FOUND", "The requested resource was not found.", request_id)
            return

        method = str(scope.get("method", "GET")).upper()
        public_host = _validated_public_host(headers, config)
        if public_host is None:
            await self._safe_error(send, 404, "NOT_FOUND", "The requested resource was not found.", request_id)
            return
        if method not in _METHODS:
            await self._safe_error(send, 405, "METHOD_NOT_ALLOWED", "The request method is not supported.", request_id)
            return

        content_lengths = _values(headers, b"content-length")
        if len(content_lengths) > 1:
            await self._safe_error(send, 400, "INVALID_CONTENT_LENGTH", "The request could not be processed.", request_id)
            return
        if content_lengths:
            try:
                content_length = int(content_lengths[0].decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                await self._safe_error(send, 400, "INVALID_CONTENT_LENGTH", "The request could not be processed.", request_id)
                return
            if content_length < 0 or content_length > config.max_body_bytes:
                await self._safe_error(send, 413, "REQUEST_TOO_LARGE", "The request exceeds the allowed size.", request_id)
                return

        query = scope.get("query_string", b"")
        if len(query) > 16_384:
            await self._safe_error(send, 414, "URI_TOO_LONG", "The request URI is too long.", request_id)
            return
        try:
            query_text = query.decode("ascii")
            url = f"{config.upstream_base}{path}"
            if query_text:
                url += f"?{query_text}"
        except UnicodeDecodeError:
            await self._safe_error(send, 400, "INVALID_URI", "The request could not be processed.", request_id)
            return

        request_headers = _safe_request_headers(headers, config, request_id, public_host)
        has_body = method not in {"GET", "HEAD", "OPTIONS"} or bool(content_lengths)
        content = self._body_stream(receive, config.max_body_bytes) if has_body else None
        started = time.monotonic()
        try:
            client = self._get_client()
            request = client.build_request(method, url, headers=request_headers, content=content)
            response = await client.send(request, stream=True)
        except BodyLimitExceeded:
            await self._safe_error(send, 413, "REQUEST_TOO_LARGE", "The request exceeds the allowed size.", request_id)
            return
        except httpx.TimeoutException as exc:
            logger.warning(
                "relay_http_upstream_failed request_id=%s stage=upstream_timeout error_type=%s duration_ms=%s",
                request_id,
                type(exc).__name__,
                int((time.monotonic() - started) * 1000),
            )
            await self._safe_error(send, 504, "RELAY_UPSTREAM_TIMEOUT", "The service took too long to respond.", request_id)
            return
        except httpx.RequestError as exc:
            logger.warning(
                "relay_http_upstream_failed request_id=%s stage=upstream_connect error_type=%s duration_ms=%s",
                request_id,
                type(exc).__name__,
                int((time.monotonic() - started) * 1000),
            )
            await self._safe_error(send, 503, "RELAY_UPSTREAM_UNAVAILABLE", "The service is temporarily unavailable.", request_id)
            return
        except Exception as exc:  # noqa: BLE001 - never forward internal error details
            logger.error(
                "relay_http_failed request_id=%s stage=proxy error_type=%s",
                request_id,
                type(exc).__name__,
            )
            await self._safe_error(send, 502, "RELAY_ERROR", "The request could not be completed.", request_id)
            return

        if response.status_code >= 500:
            # A safe hop-level breadcrumb when the private application returns
            # an error; no response body, media metadata, or headers are logged.
            logger.warning(
                "relay_http_upstream_response request_id=%s stage=upstream_response status=%s duration_ms=%s",
                request_id,
                response.status_code,
                int((time.monotonic() - started) * 1000),
            )

        response_headers: list[tuple[bytes, bytes]] = []
        response_connection_tokens = _connection_tokens(response.headers.raw)
        blocked = _HOP_BY_HOP | response_connection_tokens | {
            b"server",
            b"via",
            b"x-powered-by",
            b"x-request-id",
            b"x-nexora-relay-token",
            b"x-nexora-client-id",
            b"x-real-ip",
            b"forwarded",
        }
        for key, value in response.headers.raw:
            lowered = key.lower()
            if (
                lowered in blocked
                or lowered.startswith(b"x-forwarded-")
                or lowered.startswith(b"x-original-")
                or lowered.startswith(b"x-rewrite-")
            ):
                continue
            if lowered in {b"location", b"content-location"}:
                try:
                    location = urlsplit(value.decode("latin-1"))
                    location_host = (location.hostname or "").lower()
                    if _is_internal_host(location_host, config):
                        public_location = f"{config.public_scheme}://{public_host}"
                        rewritten = public_location + (location.path or "/")
                        if location.query:
                            rewritten += f"?{location.query}"
                        if location.fragment:
                            rewritten += f"#{location.fragment}"
                        value = rewritten.encode("latin-1")
                except (UnicodeDecodeError, ValueError):
                    continue
            elif lowered == b"set-cookie":
                value = _strip_internal_cookie_domain(value, config)
            response_headers.append((lowered, value))
        response_headers.append((b"x-request-id", request_id.encode("ascii")))
        try:
            await send({"type": "http.response.start", "status": response.status_code, "headers": response_headers})
            async for chunk in response.aiter_raw():
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - downstream disconnect / streaming error
            logger.warning(
                "relay_http_stream_failed request_id=%s stage=response_stream error_type=%s",
                request_id,
                type(exc).__name__,
            )
        finally:
            await response.aclose()

    async def _websocket(self, scope, receive, send):
        headers = _pairs(scope)
        request_id = _request_id(headers)
        try:
            config = self._get_config()
        except RelayConfigurationUnavailable:
            await send({"type": "websocket.close", "code": 1011})
            return
        try:
            path = _raw_path(scope)
        except ValueError:
            await send({"type": "websocket.close", "code": 4404})
            return

        public_host = _validated_public_host(headers, config)
        if public_host is None or not path.startswith("/ws/") or not _allowed_path(str(scope.get("path", "/"))):
            await send({"type": "websocket.close", "code": 4403})
            return

        initial = await receive()
        if initial.get("type") != "websocket.connect":
            return
        query = scope.get("query_string", b"")
        try:
            query_text = query.decode("ascii")
            scheme = "wss" if config.upstream_scheme == "https" else "ws"
            uri = f"{scheme}://{config.upstream_host}:{config.upstream_port}{path}"
            if query_text:
                uri += f"?{query_text}"
        except UnicodeDecodeError:
            await send({"type": "websocket.close", "code": 4400})
            return

        ws_headers = _safe_request_headers(headers, config, request_id, public_host, websocket=True)
        subprotocols = list(scope.get("subprotocols", ()))
        started = time.monotonic()
        try:
            async with websockets.connect(
                uri,
                additional_headers=ws_headers,
                subprotocols=subprotocols or None,
                compression=None,
                proxy=None,
                open_timeout=config.connect_timeout_seconds,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                max_size=1_048_576,
            ) as upstream:
                await send({"type": "websocket.accept", "subprotocol": upstream.subprotocol})

                async def client_to_upstream():
                    while True:
                        message = await receive()
                        if message["type"] == "websocket.disconnect":
                            return "client_disconnect"
                        if message["type"] != "websocket.receive":
                            continue
                        if message.get("bytes") is not None:
                            await upstream.send(message["bytes"])
                        elif message.get("text") is not None:
                            await upstream.send(message["text"])

                async def upstream_to_client():
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await send({"type": "websocket.send", "bytes": message})
                        else:
                            await send({"type": "websocket.send", "text": message})
                    return "upstream_close"

                client_task = asyncio.create_task(client_to_upstream())
                upstream_task = asyncio.create_task(upstream_to_client())
                tasks = {client_task, upstream_task}
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                outcome = next(iter(done)).result()
                if outcome == "upstream_close":
                    code = upstream.close_code
                    safe_code = code if isinstance(code, int) and 1000 <= code <= 4999 and code not in {1004, 1005, 1006, 1015} else 1000
                    await send({"type": "websocket.close", "code": safe_code})
                else:
                    await upstream.close(code=1000)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - avoid logging URL, headers, cookies, or body
            logger.warning(
                "relay_websocket_failed request_id=%s stage=upstream_handshake_or_stream error_type=%s duration_ms=%s",
                request_id,
                type(exc).__name__,
                int((time.monotonic() - started) * 1000),
            )
            await send({"type": "websocket.close", "code": 1011})


application = SecurityRelay()
