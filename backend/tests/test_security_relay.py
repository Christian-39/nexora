"""Tests for the authenticated ASGI relay boundary and anonymous throttle keys."""

import pytest
from django.conf import settings
from django.test import RequestFactory

from apps.core.throttles import LoginThrottle
from apps.security.relay import SecurityRelayBoundary


class _EchoASGI:
    def __init__(self):
        self.scope = None

    async def __call__(self, scope, receive, send):
        self.scope = scope
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})
        else:
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.close", "code": 1000})


def _scope(protocol="http", headers=()):
    return {
        "type": protocol,
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http" if protocol == "http" else "ws",
        "path": "/api/public/config/" if protocol == "http" else "/ws/app/",
        "raw_path": b"/api/public/config/" if protocol == "http" else b"/ws/app/",
        "query_string": b"",
        "headers": list(headers),
        "client": ("10.0.0.3", 12345),
        "server": ("10.0.0.4", 8000),
        "subprotocols": [],
    }


async def _run(app, scope):
    output = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        output.append(message)

    await app(scope, receive, send)
    return output


@pytest.mark.asyncio
async def test_http_relay_auth_removes_shared_token_and_only_then_trusts_https(monkeypatch):
    monkeypatch.setattr(settings, "SECURITY_RELAY_REQUIRED", True, raising=False)
    monkeypatch.setattr(settings, "SECURITY_RELAY_TOKEN", "relay-test-secret-0123456789abcdef", raising=False)
    downstream = _EchoASGI()
    middleware = SecurityRelayBoundary(downstream)
    scope = _scope(
        headers=[
            (b"x-nexora-relay-token", b"relay-test-secret-0123456789abcdef"),
            (b"x-forwarded-proto", b"https"),
            (b"host", b"nexora-relay.example.test"),
        ]
    )

    output = await _run(middleware, scope)

    assert output[0]["status"] == 204
    assert downstream.scope["scheme"] == "https"
    assert (b"x-nexora-relay-token", b"relay-test-secret-0123456789abcdef") not in downstream.scope["headers"]
    assert (b"x-forwarded-proto", b"https") in downstream.scope["headers"]


@pytest.mark.asyncio
async def test_websocket_relay_auth_maps_https_to_wss(monkeypatch):
    monkeypatch.setattr(settings, "SECURITY_RELAY_REQUIRED", True, raising=False)
    monkeypatch.setattr(settings, "SECURITY_RELAY_TOKEN", "relay-test-secret-0123456789abcdef", raising=False)
    downstream = _EchoASGI()
    middleware = SecurityRelayBoundary(downstream)
    scope = _scope(
        "websocket",
        headers=[
            (b"x-nexora-relay-token", b"relay-test-secret-0123456789abcdef"),
            (b"x-forwarded-proto", b"https"),
            (b"host", b"nexora-relay.example.test"),
        ],
    )

    output = await _run(middleware, scope)

    assert output[0]["type"] == "websocket.accept"
    assert downstream.scope["scheme"] == "wss"
    assert all(key != b"x-nexora-relay-token" for key, _ in downstream.scope["headers"])


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"x-nexora-relay-token", b"incorrect-secret")],
        [
            (b"x-nexora-relay-token", b"relay-test-secret-0123456789abcdef"),
            (b"x-forwarded-proto", b"https,http"),
        ],
        [
            (b"x-nexora-relay-token", b"relay-test-secret-0123456789abcdef"),
            (b"x-forwarded-proto", b"https"),
            (b"x-forwarded-proto", b"http"),
        ],
    ],
)
@pytest.mark.asyncio
async def test_http_rejects_missing_or_invalid_relay_credentials_and_ambiguous_scheme(monkeypatch, headers):
    monkeypatch.setattr(settings, "SECURITY_RELAY_REQUIRED", True, raising=False)
    monkeypatch.setattr(settings, "SECURITY_RELAY_TOKEN", "relay-test-secret-0123456789abcdef", raising=False)
    downstream = _EchoASGI()
    middleware = SecurityRelayBoundary(downstream)

    output = await _run(middleware, _scope(headers=headers))

    assert output[0]["type"] == "http.response.start"
    assert output[0]["status"] == 404
    assert downstream.scope is None


@pytest.mark.asyncio
async def test_websocket_rejects_missing_relay_credentials(monkeypatch):
    monkeypatch.setattr(settings, "SECURITY_RELAY_REQUIRED", True, raising=False)
    monkeypatch.setattr(settings, "SECURITY_RELAY_TOKEN", "relay-test-secret-0123456789abcdef", raising=False)
    downstream = _EchoASGI()

    output = await _run(SecurityRelayBoundary(downstream), _scope("websocket"))

    assert output == [{"type": "websocket.close", "code": 4403}]
    assert downstream.scope is None


def test_anonymous_throttle_uses_a_scope_bound_hash_of_browser_id_not_shared_relay_ip(settings):
    throttle = LoginThrottle()
    factory = RequestFactory()

    one = factory.get("/api/auth/login/", HTTP_X_NEXORA_CLIENT_ID="browser-opaque-01")
    two = factory.get("/api/auth/login/", HTTP_X_NEXORA_CLIENT_ID="browser-opaque-02")
    same = factory.get("/api/auth/login/", HTTP_X_NEXORA_CLIENT_ID="browser-opaque-01")
    for request in (one, two, same):
        request.META["REMOTE_ADDR"] = "10.0.0.7"  # the relay's shared private peer address

    one_key = throttle.get_ident(one)
    two_key = throttle.get_ident(two)
    same_key = throttle.get_ident(same)

    assert one_key != two_key
    assert one_key == same_key
    assert "browser-opaque" not in one_key
    assert one_key.startswith("browser:")


def test_invalid_or_missing_anonymous_id_falls_back_to_direct_peer_ip():
    throttle = LoginThrottle()
    factory = RequestFactory()
    first = factory.get("/api/auth/login/", HTTP_X_NEXORA_CLIENT_ID="not valid spaces")
    second = factory.get("/api/auth/login/")
    first.META["REMOTE_ADDR"] = second.META["REMOTE_ADDR"] = "10.0.0.7"

    assert throttle.get_ident(first) == throttle.get_ident(second)


def test_request_id_middleware_uses_the_same_bounded_ascii_grammar_as_the_relay():
    import re

    from django.http import HttpResponse

    from apps.core.middleware import RequestIDMiddleware

    seen = {}
    middleware = RequestIDMiddleware(lambda request: (seen.setdefault("id", request.request_id), HttpResponse())[1])
    factory = RequestFactory()

    accepted = middleware(factory.get("/health/live/", HTTP_X_REQUEST_ID="relay-request_01"))
    assert seen["id"] == "relay-request_01"
    assert accepted["X-Request-ID"] == seen["id"]

    for invalid in ("not:shared:grammar", "x" * 65, "id-with-unicode-é"):
        seen.clear()
        response = middleware(factory.get("/health/live/", HTTP_X_REQUEST_ID=invalid))
        assert seen["id"] != invalid
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", seen["id"])
        assert response["X-Request-ID"] == seen["id"]


def test_security_event_ip_helper_never_trusts_forwarded_client_headers(monkeypatch):
    from apps.security.services import client_ip

    request = RequestFactory().get(
        "/api/public/config/",
        HTTP_X_FORWARDED_FOR="198.51.100.27",
        HTTP_X_REAL_IP="203.0.113.15",
    )
    request.META["REMOTE_ADDR"] = "10.0.0.7"

    monkeypatch.setattr(settings, "SECURITY_RELAY_REQUIRED", True, raising=False)
    assert client_ip(request) is None

    monkeypatch.setattr(settings, "SECURITY_RELAY_REQUIRED", False, raising=False)
    assert client_ip(request) == "10.0.0.7"
