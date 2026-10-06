"""Tests for Redis/Channels resilience, health endpoints, WebSocket isolation, and error logging."""

from __future__ import annotations

import pytest
import redis.exceptions
from channels.testing import WebsocketCommunicator
from django.db import OperationalError
from rest_framework.test import APIClient

from apps.accounts import ws_auth
from apps.conversations.consumers import AppConsumer
from apps.conversations.realtime import emit_to_users
from config.asgi import application
from tests.test_websocket import _issue_cookie, drain, socket


@pytest.mark.django_db
def test_health_live_and_ready_endpoints():
    client = APIClient()

    live_resp = client.get("/health/live/")
    assert live_resp.status_code == 200
    live_body = live_resp.json()
    assert live_body["success"] is True
    assert live_body["data"]["status"] == "ok"

    ready_resp = client.get("/health/ready/")
    assert ready_resp.status_code == 200
    ready_body = ready_resp.json()
    assert ready_body["success"] is True
    assert ready_body["data"]["database"] == "ok"
    assert ready_body["data"]["cache"] == "ok"
    assert ready_body["data"]["channels"] == "ok"
    assert ready_body["data"]["status"] == "healthy"
    assert ready_body["data"]["degraded"] == []


@pytest.mark.django_db
def test_health_ready_distinguishes_degraded_cache_or_channels_from_fatal_db(monkeypatch):
    from apps.core import health

    client = APIClient()

    # 1. Channels degraded while DB is ok -> HTTP 200, status="degraded"
    monkeypatch.setattr(health, "_check_channels", lambda: ("failed", "TimeoutError"))
    degraded_resp = client.get("/health/ready/")
    assert degraded_resp.status_code == 200
    degraded_data = degraded_resp.json()["data"]
    assert degraded_data["status"] == "degraded"
    assert "channels" in degraded_data["degraded"]
    assert degraded_data["reasons"]["channels"] == "TimeoutError"

    # 2. Database down -> HTTP 503, status="unavailable"
    monkeypatch.setattr(health, "_check_database", lambda: ("failed", "OperationalError"))
    down_resp = client.get("/health/ready/")
    assert down_resp.status_code == 503
    down_data = down_resp.json()["data"]
    assert down_data["status"] == "unavailable"
    assert "database" in down_data["degraded"]


@pytest.mark.asyncio
async def test_redis_channel_layer_pool_options_and_brpop_timeout_resilience():
    from channels_redis.core import RedisChannelLayer

    layer = RedisChannelLayer(hosts=["redis://127.0.0.1:6379/0"])
    pool = layer.create_pool(0)
    kwargs = pool.connection_kwargs
    assert kwargs["socket_connect_timeout"] >= 5.0
    assert kwargs["socket_timeout"] >= layer.brpop_timeout + 10.0
    assert kwargs["retry_on_timeout"] is True
    assert kwargs["health_check_interval"] >= 30
    assert kwargs["socket_keepalive"] is True

    # Simulate a transient redis.exceptions.TimeoutError inside _orig_brpop_with_clean
    orig = RedisChannelLayer._orig_brpop_with_clean

    async def raising_brpop(self, index, channel, timeout):
        raise redis.exceptions.TimeoutError("Timeout reading from socket")

    RedisChannelLayer._orig_brpop_with_clean = raising_brpop
    try:
        result = await layer._brpop_with_clean(0, "test-channel", 5)
        assert result is None
    finally:
        RedisChannelLayer._orig_brpop_with_clean = orig


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_app_consumer_does_not_join_peer_personal_user_groups(admin, member_a, member_b):
    """Connecting to /ws/app/ must subscribe a user ONLY to their own user_<id>
    group, never to peer user_<peer_id> groups (which both caused O(N) Redis
    group_add calls and leaked per-user events).
    """
    comm_a = await socket(member_a)
    await comm_a.connect()
    ready_a = await comm_a.receive_json_from(timeout=3)
    assert ready_a["type"] == "connection.ready"

    comm_admin = await socket(admin)
    await comm_admin.connect()
    ready_admin = await comm_admin.receive_json_from(timeout=3)
    assert ready_admin["type"] == "connection.ready"

    # Drain all presence notifications triggered during connect
    while not await comm_a.receive_nothing(timeout=0.1):
        await comm_a.receive_json_from(timeout=1)
    while not await comm_admin.receive_nothing(timeout=0.1):
        await comm_admin.receive_json_from(timeout=1)

    # Emit a personal event strictly to member_b's user group; neither admin
    # nor member_a may receive it.
    emit_to_users([member_b.id], "notification.new", {"id": "notif-b-only"}, on_commit=False)
    assert await comm_admin.receive_nothing(timeout=0.4)
    assert await comm_a.receive_nothing(timeout=0.4)

    await comm_admin.disconnect()
    await comm_a.disconnect()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_ws_auth_database_failure_closes_with_1013_instead_of_4401(member_a, monkeypatch):
    cookie = await _issue_cookie(member_a)

    async def db_down(_token):
        from django.contrib.auth.models import AnonymousUser

        return AnonymousUser(), None, "SERVICE_UNAVAILABLE"

    monkeypatch.setattr(ws_auth, "auth_for", db_down)

    communicator = WebsocketCommunicator(application, "/ws/app/")
    communicator.scope["headers"] = [(b"cookie", cookie)]
    connected, _ = await communicator.connect()
    assert connected

    frame = await communicator.receive_json_from(timeout=3)
    assert frame == {"type": "error", "code": "SERVICE_UNAVAILABLE"}

    closed = await communicator.receive_output(timeout=3)
    assert closed["type"] == "websocket.close"
    assert closed["code"] == AppConsumer.TRANSIENT_CLOSE_CODE == 1013
    await communicator.disconnect()


@pytest.mark.django_db
def test_client_error_sink_logs_and_redacts_secrets():
    import logging

    records: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _ListHandler()
    client_logger = logging.getLogger("nexora.client")
    client_logger.addHandler(handler)
    try:
        client = APIClient()
        resp = client.post(
            "/api/client-errors/",
            {
                "kind": "window.error",
                "message": "Failed with pin=123456 and token=secret-jwt-value",
                "stack": "Error: pin=123456\n    at login.html:200:12",
                "url": "https://nexora-eight-lilac.vercel.app/login.html",
                "status": 500,
                "code": "HTTP_500",
                "endpoint": "/api/auth/change-pin/",
            },
            format="json",
        )
    finally:
        client_logger.removeHandler(handler)

    assert resp.status_code == 202
    combined = "\n".join(r.getMessage() for r in records)
    assert "123456" not in combined
    assert "secret-jwt-value" not in combined
    assert "[redacted]" in combined.lower()


@pytest.mark.django_db
def test_request_log_has_role_timing_query_metrics_and_never_logs_query_text(admin):
    import logging

    from django.http import HttpResponse
    from django.test import RequestFactory, override_settings

    from apps.accounts.models import User
    from apps.core.middleware import RequestLogMiddleware

    records = []

    class ListHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = ListHandler()
    request_logger = logging.getLogger("nexora.request")
    request_logger.addHandler(handler)

    def get_response(request):
        User.objects.filter(pk=admin.pk).count()
        return HttpResponse(status=503)

    request = RequestFactory().get(
        "/api/search/?q=private-message-never-log&token=do-not-log-this"
    )
    request.user = admin
    try:
        with override_settings(SLOW_REQUEST_MS=0, SLOW_QUERY_MS=0):
            middleware = RequestLogMiddleware(get_response)
            response = middleware(request)
    finally:
        request_logger.removeHandler(handler)

    assert response.status_code == 503
    record = next(record for record in records if record.name == "nexora.request")
    message = record.getMessage()
    assert "/api/search/" in message
    assert "db_queries=1" in message
    assert "db_ms=" in message and "db_slow=1" in message
    assert "ADMIN" in message
    assert str(admin.id) == record.user_id
    assert record.user_role == "ADMIN"
    assert "private-message-never-log" not in message
    assert "do-not-log-this" not in message
    assert "?q=" not in message


def test_production_formatter_scrubs_exception_text_before_writing_logs():
    import logging

    from apps.core.observability import ProductionFormatter, scrub

    try:
        raise RuntimeError(
            'storage failed pin=123456 token=secret-value '
            'AWS_SECRET_ACCESS_KEY=provider-secret csrf_token="csrf-secret" X-Amz-Signature=signed-value'
        )
    except RuntimeError:
        record = logging.LogRecord("nexora.test", logging.ERROR, __file__, 1, "unexpected failure", (), __import__("sys").exc_info())

    formatter = ProductionFormatter("%(levelname)s %(message)s")
    rendered = formatter.format(record)
    assert "123456" not in rendered
    assert "secret-value" not in rendered
    assert "provider-secret" not in rendered
    assert "csrf-secret" not in rendered
    assert "signed-value" not in rendered
    assert "[redacted]" in rendered
    nested = scrub({"signed_url": "https://objects.example/private?token=secret"})
    assert nested["signed_url"] == "[redacted]"
