"""Performance and deployment-correctness guards.

These tests encode the fixes this repair made:

* the CORP header must be configurable (``cross-origin`` for the split
  Vercel/Render deployment so avatars can be embedded) — it was hard-coded
  to ``same-site``, which silently broke every cross-site image;
* the public configuration endpoint advertises safe shared caching;
* the WebSocket handshake does ONE presence-peer query (it did three, plus
  one cache read per peer);
* the conversation list runs a bounded number of queries for a page of
  conversations (no per-conversation last-message query).
"""

from __future__ import annotations

import pytest
from channels.testing import WebsocketCommunicator
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from tests.conftest import authed
from tests.test_websocket import _issue_cookie


# ---------------------------------------------------------------- headers


@pytest.mark.django_db
def test_api_responses_carry_the_configured_cross_origin_resource_policy(admin):
    client = authed(admin)
    response = client.get("/api/me/")
    from django.conf import settings

    assert response["Cross-Origin-Resource-Policy"] == settings.CROSS_ORIGIN_RESOURCE_POLICY
    assert response["Cross-Origin-Resource-Policy"] == "cross-origin"


@pytest.mark.django_db
def test_public_config_advertises_safe_shared_caching(db):
    from rest_framework.test import APIClient

    response = APIClient().get("/api/public/config/")
    assert response.status_code == 200
    cache_control = response["Cache-Control"]
    assert "public" in cache_control
    assert "max-age" in cache_control


@pytest.mark.django_db
def test_private_api_responses_are_never_publicly_cacheable(admin):
    client = authed(admin)
    response = client.get("/api/me/")
    cache_control = response.get("Cache-Control", "")
    assert "public" not in cache_control


# ------------------------------------------------- websocket presence cost


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_socket_handshake_runs_one_presence_peer_query(member_a, admin, monkeypatch):
    """connect() must query presence peers exactly once.

    The previous implementation ran ``_presence_peers`` three times per
    handshake (audience, watchers, snapshot) plus one cache.get per peer —
    on every page navigation.
    """
    from apps.conversations import consumers

    calls = {"peers": 0}
    original = consumers._presence_peers

    def counting_peers(user):
        calls["peers"] += 1
        return original(user)

    monkeypatch.setattr(consumers, "_presence_peers", counting_peers)

    communicator = WebsocketCommunicator(application := __import__("config.asgi", fromlist=["application"]).application, "/ws/app/")
    communicator.scope["headers"] = [(b"cookie", await _issue_cookie(member_a))]
    connected, _ = await communicator.connect()
    assert connected
    ready = await communicator.receive_json_from(timeout=3)
    assert ready["type"] == "connection.ready"
    assert "presence" in ready["data"]

    await communicator.disconnect()
    assert calls["peers"] == 1, "the handshake must resolve the presence audience exactly once"


# ------------------------------------------------------ conversation list


@pytest.mark.django_db
def test_conversation_list_is_query_bounded(admin, member_a, private_thread, django_assert_max_num_queries):
    """A page of conversations must not add per-conversation queries.

    The last message for the whole page is fetched with one query; presence
    for the page's participants is one batched cache read. We allow a small
    constant envelope (auth, pagination, participants prefetch, last
    messages, unread annotations).
    """
    from apps.conversations.services import private_conversation

    for i in range(5):
        other = __import__("apps.accounts.models", fromlist=["User"]).User.objects.create_user(
            f"+4420794609{i:02d}", "123456", full_name=f"Member {i}", role="MEMBER"
        )
        private_conversation(admin, other)

    client = authed(admin)
    with CaptureQueriesContext(connection := __import__("django.db", fromlist=["connection"]).connection) as ctx:
        response = client.get("/api/conversations/")
    assert response.status_code == 200

    # 6 conversations, no messages anywhere: auth+session (2), main queryset
    # (1), participants prefetch (2), and nothing else. The old code added
    # TWO queries per conversation (last message + unread COUNT) — 12 extra
    # queries that scale with the member base. A flat bound proves the fix.
    sqls = [q["sql"] for q in ctx.captured_queries]
    assert len(ctx.captured_queries) <= 8, sqls
