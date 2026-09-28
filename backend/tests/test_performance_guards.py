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


# ------------------------------------------- notification aggregation lock


@pytest.mark.django_db(transaction=True)
def test_new_message_locking_read_runs_inside_a_transaction(admin, member_a, private_thread, monkeypatch):
    """The send path must stay transactional, because it takes a row lock.

    ``notify_new_message`` aggregates repeat notifications with
    ``select_for_update()``. That is only legal inside a transaction: on MySQL
    (production) a locking read issued in autocommit raises
    TransactionManagementError, turning an ordinary message send into HTTP 500.
    Today it is safe purely because ``send_message`` and ``create_text_message``
    are decorated ``@transaction.atomic`` — an implicit dependency that a future
    refactor could remove without any local symptom, since the suite runs on
    SQLite, whose backend reports ``has_select_for_update = False`` and drops
    the clause silently.

    So this test pins the invariant instead of the implementation: whenever the
    send path issues a locking read, the connection must not be in autocommit.
    ``transaction=True`` is required, otherwise the enclosing test transaction
    would satisfy the assertion by itself and prove nothing.
    """
    from django.db import connection
    from django.db.models import QuerySet

    observed = []
    original = QuerySet.select_for_update

    def spy(self, *args, **kwargs):
        observed.append(connection.get_autocommit())
        return original(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", spy)

    # An eligible recipient is required: notify_new_message skips the locking
    # read entirely for recipients who have message notifications disabled.
    assert member_a.notify_messages, "fixture must have message notifications enabled"

    # Pin the aggregation window instead of relying on the default: a zero
    # window disables the locking read altogether. messaging_policy() also
    # memoises in a process-local cache that cache.clear() does not reset, so
    # that has to be invalidated too or a policy from an earlier test leaks in.
    from apps.platform_settings import services as platform_services
    from apps.platform_settings.models import PlatformConfiguration

    PlatformConfiguration.objects.update_or_create(
        pk=PlatformConfiguration.objects.values_list("pk", flat=True).first(),
        defaults={"notification_aggregation_window_seconds": 60, "notification_previews": True},
    )
    platform_services._local.update(value=None, at=0.0)

    first = authed(admin).post(
        f"/api/conversations/{private_thread.id}/messages/",
        {"client_id": "lock-guard-1", "text": "hello"},
        format="json",
    )
    assert first.status_code == 201, getattr(first, "data", first.content)

    # Second send inside the aggregation window: this is the branch that reads
    # an existing notification row under the lock.
    second = authed(admin).post(
        f"/api/conversations/{private_thread.id}/messages/",
        {"client_id": "lock-guard-2", "text": "hello again"},
        format="json",
    )
    assert second.status_code == 201, getattr(second, "data", second.content)

    assert observed, "the aggregation path must issue a locking read"
    assert not any(observed), (
        "select_for_update() was issued in autocommit mode; on MySQL this raises "
        "TransactionManagementError and the send returns HTTP 500"
    )

    from apps.notifications.models import Notification

    aggregated = Notification.objects.get(recipient=member_a, type="NEW_MESSAGE")
    assert aggregated.aggregate_count == 2, "two sends inside the window must aggregate onto one notification"
