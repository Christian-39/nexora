"""Security and transport tests for the standalone native-Python relay."""

from __future__ import annotations

from collections import deque

import httpx
import pytest

import app as relay_app
from app import (
    RelayConfig,
    SecurityRelay,
    _allowed_path,
    _is_internal_host,
    _request_id,
    _safe_request_headers,
    _strip_internal_cookie_domain,
    _validated_public_host,
)


TOKEN = "relay-test-secret-of-at-least-32-characters"
CONFIG = RelayConfig(
    upstream_host="nexora-backend.internal",
    upstream_port=10000,
    upstream_scheme="http",
    public_hosts=("relay.example.test",),
    public_scheme="https",
    shared_token=TOKEN,
)


def _http_scope(*, path="/api/messages/", raw_path=None, headers=(), query=b"", method="GET"):
    encoded = path.encode("utf-8") if raw_path is None else raw_path
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": encoded,
        "query_string": query,
        "headers": list(headers),
        "client": ("192.0.2.12", 43100),
        "server": ("relay.internal", 10000),
    }


def _ws_scope(*, path="/ws/app/", raw_path=None, headers=(), query=b"", subprotocols=()):
    encoded = path.encode("utf-8") if raw_path is None else raw_path
    return {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": path,
        "raw_path": encoded,
        "query_string": query,
        "headers": list(headers),
        "client": ("192.0.2.12", 43100),
        "server": ("relay.internal", 10000),
        "subprotocols": list(subprotocols),
    }


async def _invoke(app, scope, messages=()):
    incoming = deque(messages)
    sent = []

    async def receive():
        if incoming:
            return incoming.popleft()
        return {"type": "http.disconnect" if scope["type"] == "http" else "websocket.disconnect", "code": 1000}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


def test_request_id_is_bounded_ascii_and_matches_the_backend_log_grammar():
    assert _request_id([(b"x-request-id", b"relay-request_01")]) == "relay-request_01"
    for invalid in (b"not:shared:grammar", b"x" * 65, b"request-with-unicode-\xff"):
        result = _request_id([(b"x-request-id", invalid)])
        assert 1 <= len(result) <= 64
        assert all(char.isalnum() or char in "-_" for char in result)


def test_public_host_is_exact_and_rejects_userinfo_and_duplicates():
    assert _validated_public_host([(b"host", b"relay.example.test")], CONFIG) == "relay.example.test"
    assert _validated_public_host([(b"host", b"relay.example.test:443")], CONFIG) == "relay.example.test"
    assert _validated_public_host([(b"host", b"evil.example.test")], CONFIG) is None
    assert _validated_public_host([(b"host", b"relay.example.test@evil.example")], CONFIG) is None
    assert _validated_public_host(
        [(b"host", b"relay.example.test"), (b"host", b"relay.example.test")], CONFIG
    ) is None


def test_route_allowlist_rejects_traversal_encoded_separators_and_non_api_paths():
    assert _allowed_path("/api/messages/")
    assert _allowed_path("/ws/app/")
    assert _allowed_path("/health/live/")
    for path in ("/admin/", "/api/../admin/", "/api//messages/", "/api/%2e%2e/admin/", "/api\\admin/"):
        assert not _allowed_path(path)


def test_incoming_forwarded_headers_are_discarded_and_rebuilt_by_the_relay():
    headers = [
        (b"host", b"relay.example.test"),
        (b"x-forwarded-host", b"attacker.invalid"),
        (b"x-forwarded-proto", b"http"),
        (b"x-forwarded-for", b"198.51.100.10"),
        (b"x-forwarded-client-cert", b"attacker-cert"),
        (b"forwarded", b"for=198.51.100.11;proto=http"),
        (b"x-real-ip", b"203.0.113.9"),
        (b"x-nexora-relay-token", b"attacker-token"),
        (b"x-request-id", b"bad id"),
        (b"x-nexora-client-id", b"browser-test-01"),
        (b"authorization", b"Bearer user-token"),
        (b"cookie", b"session=private"),
        (b"range", b"bytes=0-100"),
        (b"content-type", b"application/octet-stream"),
    ]

    outgoing = _safe_request_headers(headers, CONFIG, "relay-request-01", "relay.example.test")
    mapping = [(name.lower(), value) for name, value in outgoing]

    assert ("host", "relay.example.test") in mapping
    assert ("x-forwarded-host", "relay.example.test") in mapping
    assert ("x-forwarded-proto", "https") in mapping
    assert ("x-nexora-relay-token", TOKEN) in mapping
    assert ("x-request-id", "relay-request-01") in mapping
    assert ("x-nexora-client-id", "browser-test-01") in mapping
    assert ("authorization", "Bearer user-token") in mapping
    assert ("cookie", "session=private") in mapping
    assert ("range", "bytes=0-100") in mapping
    assert all(name not in {"forwarded", "x-forwarded-for", "x-forwarded-client-cert", "x-real-ip"} for name, _ in mapping)
    assert not any(value in {"attacker.invalid", "http", "198.51.100.10", "attacker-token"} for _, value in mapping)


def test_internal_location_cookie_domain_and_private_ip_are_not_returned_to_clients():
    assert _is_internal_host("nexora-backend.internal", CONFIG)
    assert _is_internal_host("10.2.3.4", CONFIG)
    assert _is_internal_host("127.0.0.1", CONFIG)
    assert not _is_internal_host("storage.example.test", CONFIG)
    assert _strip_internal_cookie_domain(
        b"nexora_access=secret; Domain=nexora-backend.internal; Path=/; Secure; HttpOnly", CONFIG
    ) == b"nexora_access=secret; Path=/; Secure; HttpOnly"
    assert _strip_internal_cookie_domain(
        b"session=x; Domain=example.test; Path=/; Secure", CONFIG
    ) == b"session=x; Domain=example.test; Path=/; Secure"


def test_relay_config_rejects_public_or_ip_literal_upstream_and_non_https_public_scheme():
    valid = {
        "UPSTREAM_HOST": "nexora-backend.internal",
        "UPSTREAM_PORT": "10000",
        "UPSTREAM_SCHEME": "http",
        "PUBLIC_HOSTS": "relay.example.test",
        "SECURITY_RELAY_TOKEN": TOKEN,
    }
    assert RelayConfig.from_env(valid).upstream_host == "nexora-backend.internal"
    for host in ("127.0.0.1", "10.0.0.2", "api.example.test", "localhost"):
        with pytest.raises(ValueError):
            RelayConfig.from_env({**valid, "UPSTREAM_HOST": host})
    with pytest.raises(ValueError):
        RelayConfig.from_env({**valid, "PUBLIC_SCHEME": "http"})


@pytest.mark.asyncio
async def test_http_proxy_streams_request_preserves_auth_and_media_headers_and_scrubs_internal_response():
    captured = {}

    class ResponseBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"ok":true}'

        async def aclose(self):
            return None

    async def handler(request):
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["body"] = await request.aread()
        return httpx.Response(
            201,
            headers=[
                ("location", "http://nexora-backend.internal/api/messages/created/"),
                ("x-forwarded-for", "10.2.3.4"),
                ("x-request-id", "backend-private"),
                ("set-cookie", "nexora_access=private; Domain=nexora-backend.internal; Path=/; Secure; HttpOnly"),
                ("set-cookie", "nexora_refresh=private; Path=/api/auth/; Secure; HttpOnly"),
            ],
            stream=ResponseBody(),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = SecurityRelay(CONFIG, http_client=client)
    source_headers = [
        (b"host", b"relay.example.test"),
        (b"content-type", b"application/octet-stream"),
        (b"content-length", b"11"),
        (b"authorization", b"Bearer user-token"),
        (b"cookie", b"nexora_access=private"),
        (b"range", b"bytes=0-100"),
        (b"x-forwarded-for", b"198.51.100.99"),
        (b"x-forwarded-host", b"evil.invalid"),
        (b"x-forwarded-proto", b"http"),
        (b"x-nexora-relay-token", b"spoofed"),
        (b"x-nexora-client-id", b"browser-test-01"),
        (b"x-request-id", b"relay-request-01"),
    ]
    body = b"media-bytes"
    sent = await _invoke(
        relay,
        _http_scope(path="/api/media/uploads/", headers=source_headers, query=b"upload_id=42&part=1", method="POST"),
        messages=[{"type": "http.request", "body": body, "more_body": False}],
    )
    await client.aclose()

    assert captured["url"] == "http://nexora-backend.internal:10000/api/media/uploads/?upload_id=42&part=1"
    assert captured["body"] == body
    assert captured["headers"]["host"] == "relay.example.test"
    assert captured["headers"]["authorization"] == "Bearer user-token"
    assert captured["headers"]["cookie"] == "nexora_access=private"
    assert captured["headers"]["range"] == "bytes=0-100"
    assert captured["headers"]["x-forwarded-host"] == "relay.example.test"
    assert captured["headers"]["x-forwarded-proto"] == "https"
    assert captured["headers"]["x-nexora-relay-token"] == TOKEN
    assert captured["headers"]["x-request-id"] == "relay-request-01"
    assert captured["headers"]["x-nexora-client-id"] == "browser-test-01"
    assert "x-forwarded-for" not in captured["headers"]

    response_start = sent[0]
    response_headers = response_start["headers"]
    assert response_start["status"] == 201
    assert (b"location", b"https://relay.example.test/api/messages/created/") in response_headers
    assert (b"x-request-id", b"relay-request-01") in response_headers
    assert not any(key.lower() == b"x-forwarded-for" for key, _ in response_headers)
    assert not any(b"nexora-backend.internal" in value for _, value in response_headers)
    set_cookies = [value for key, value in response_headers if key.lower() == b"set-cookie"]
    assert set_cookies == [
        b"nexora_access=private; Path=/; Secure; HttpOnly",
        b"nexora_refresh=private; Path=/api/auth/; Secure; HttpOnly",
    ]
    assert b"".join(message.get("body", b"") for message in sent[1:]) == b'{"ok":true}'


@pytest.mark.asyncio
async def test_upstream_server_error_log_is_correlated_without_logging_response_content(caplog):
    async def handler(_request):
        return httpx.Response(503, content=b"private provider response body")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = SecurityRelay(CONFIG, http_client=client)
    sent = await _invoke(
        relay,
        _http_scope(
            headers=[
                (b"host", b"relay.example.test"),
                (b"x-request-id", b"relay-request-503"),
            ]
        ),
    )
    await client.aclose()

    assert sent[0]["status"] == 503
    assert "relay_http_upstream_response" in caplog.text
    assert "request_id=relay-request-503" in caplog.text
    assert "stage=upstream_response status=503" in caplog.text
    assert "private provider response body" not in caplog.text


@pytest.mark.asyncio
async def test_relay_rejects_unlisted_host_and_disallowed_paths_without_connecting():
    contacted = False

    async def handler(request):
        nonlocal contacted
        contacted = True
        return httpx.Response(200, content=b"unexpected")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = SecurityRelay(CONFIG, http_client=client)

    bad_host = await _invoke(relay, _http_scope(headers=[(b"host", b"evil.example.test")]))
    bad_path = await _invoke(relay, _http_scope(path="/admin/", headers=[(b"host", b"relay.example.test")]))
    await client.aclose()

    assert bad_host[0]["status"] == 404
    assert bad_path[0]["status"] == 404
    assert not contacted


@pytest.mark.asyncio
async def test_relay_rejects_raw_decoded_path_mismatch_and_ambiguous_encoded_separators():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    relay = SecurityRelay(CONFIG, http_client=client)
    headers = [(b"host", b"relay.example.test")]

    mismatch = await _invoke(
        relay,
        _http_scope(path="/api/messages/", raw_path=b"/ws/app/", headers=headers),
    )
    encoded_separator = await _invoke(
        relay,
        _http_scope(path="/api/messages/", raw_path=b"/api/%2fadmin/", headers=headers),
    )
    await client.aclose()

    assert mismatch[0]["status"] == 404
    assert encoded_separator[0]["status"] == 404


@pytest.mark.asyncio
async def test_websocket_proxy_rebuilds_handshake_and_keeps_private_frames_on_the_relay():
    captured = {}

    class FakeUpstream:
        subprotocol = "nexora.v1"
        close_code = 1000

        def __init__(self):
            self.sent = []
            self.reply_sent = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def send(self, message):
            self.sent.append(message)

        async def close(self, code=1000):
            self.close_code = code

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.reply_sent:
                raise StopAsyncIteration
            self.reply_sent = True
            return '{"event":"ready"}'

    upstream = FakeUpstream()

    def fake_connect(uri, **kwargs):
        captured["uri"] = uri
        captured["kwargs"] = kwargs
        return upstream

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(relay_app.websockets, "connect", fake_connect)
    try:
        relay = SecurityRelay(CONFIG)
        headers = [
            (b"host", b"relay.example.test"),
            (b"origin", b"https://frontend.example.test"),
            (b"x-forwarded-for", b"198.51.100.44"),
            (b"x-forwarded-proto", b"http"),
            (b"sec-websocket-key", b"spoofed-key"),
            (b"sec-websocket-version", b"13"),
            (b"sec-websocket-protocol", b"nexora.v1"),
            (b"x-nexora-client-id", b"browser-test-01"),
        ]
        sent = await _invoke(
            relay,
            _ws_scope(
                headers=headers,
                query=b"token=not-a-secret-in-logs",
                subprotocols=["nexora.v1"],
            ),
            messages=[
                {"type": "websocket.connect"},
                {"type": "websocket.receive", "text": '{"event":"ping"}'},
                {"type": "websocket.disconnect", "code": 1000},
            ],
        )
    finally:
        monkeypatch.undo()

    assert captured["uri"] == "ws://nexora-backend.internal:10000/ws/app/?token=not-a-secret-in-logs"
    request_headers = {name.lower(): value for name, value in captured["kwargs"]["additional_headers"]}
    assert request_headers["host"] == "relay.example.test"
    assert request_headers["x-forwarded-host"] == "relay.example.test"
    assert request_headers["x-forwarded-proto"] == "https"
    assert request_headers["x-nexora-relay-token"] == TOKEN
    assert request_headers["origin"] == "https://frontend.example.test"
    assert "x-forwarded-for" not in request_headers
    assert "sec-websocket-key" not in request_headers
    assert captured["kwargs"]["subprotocols"] == ["nexora.v1"]
    assert upstream.sent == ['{"event":"ping"}']
    assert {message.get("text") for message in sent if message["type"] == "websocket.send"} == {'{"event":"ready"}'}
    assert any(message["type"] == "websocket.accept" for message in sent)
