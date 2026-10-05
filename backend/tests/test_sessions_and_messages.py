"""Sessions, message lifecycle, idempotency, ordering, receipts and unread."""

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import DeviceSession, User
from apps.conversations.models import Message
from apps.conversations.services import private_conversation, send_message
from tests.conftest import authed, client_id


def login(client, user, pin):
    client.get("/api/auth/csrf/")
    token = client.cookies["csrftoken"].value
    return client.post(
        "/api/auth/login/",
        {"phone": user.phone, "pin": pin, "device_label": "Phone"},
        format="json",
        HTTP_X_CSRFTOKEN=token,
    )


@pytest.mark.django_db
def test_login_creates_revocable_session_and_sets_httponly_cookies(member_a):
    client = APIClient()
    response = login(client, member_a, "123456")
    assert response.status_code == 200
    assert DeviceSession.objects.filter(user=member_a, revoked_at__isnull=True).count() == 1

    access = response.cookies["nexora_access"]
    refresh = response.cookies["nexora_refresh"]
    assert access["httponly"] and refresh["httponly"]
    assert refresh["path"] == "/api/auth/"
    # No credential material is echoed in the body.
    body = response.json()["data"]
    assert "pin" not in body and "password" not in body
    member_a.refresh_from_db()
    assert member_a.password not in str(response.data)


@pytest.mark.django_db
def test_me_reports_first_login_pin_requirement(member_a):
    client = APIClient()
    login(client, member_a, "123456")
    body = client.get("/api/me/").json()["data"]
    assert body["must_change_pin"] is True
    assert body["is_admin"] is False
    assert body["display_name"] == "Member A"


@pytest.mark.django_db
def test_pin_change_is_enforced_before_other_endpoints(member_a, admin):
    private_conversation(admin, member_a)
    client = APIClient()
    login(client, member_a, "123456")
    initial_session = member_a.device_sessions.filter(revoked_at__isnull=True).get()
    blocked = client.get("/api/conversations/")
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "PERMISSION_DENIED"

    token = client.cookies["csrftoken"].value
    changed = client.post(
        "/api/auth/change-pin/",
        {"current_pin": "123456", "new_pin": "418273"},
        format="json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert changed.status_code == 200
    assert changed.json()["data"]["must_change_pin"] is False
    member_a.refresh_from_db()
    assert member_a.credential_state == "CHANGED"
    assert member_a.check_password("418273")
    # Changing the PIN revokes the prior session and issues a fresh session
    # so the caller can immediately proceed without a second sign-in.
    initial_session.refresh_from_db()
    assert initial_session.revoked_at is not None
    assert member_a.device_sessions.filter(revoked_at__isnull=True).count() == 1
    unblocked = client.get("/api/conversations/")
    assert unblocked.status_code == 200


@pytest.mark.django_db
def test_refresh_rotates_and_revoked_session_is_rejected(member_a):
    member_a.credential_state = "CHANGED"
    member_a.save(update_fields=["credential_state"])
    client = APIClient()
    login(client, member_a, "123456")
    first_refresh = client.cookies["nexora_refresh"].value
    token = client.cookies["csrftoken"].value

    rotated = client.post("/api/auth/refresh/", {}, format="json", HTTP_X_CSRFTOKEN=token)
    assert rotated.status_code == 200
    assert client.cookies["nexora_refresh"].value != first_refresh

    DeviceSession.objects.filter(user=member_a).update(revoked_at=timezone.now())
    assert client.get("/api/me/").status_code == 401


@pytest.mark.django_db
def test_session_listing_and_revocation(member_a):
    client = authed(member_a)
    rows = client.get("/api/auth/sessions/").json()["data"]
    assert len(rows) == 1
    assert "token" not in str(rows).lower()

    assert client.delete(f"/api/auth/sessions/{rows[0]['id']}/").status_code == 200
    assert client.get("/api/me/").status_code == 401


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_message_idempotency_at_service_and_api_level(admin, member_a):
    conversation, _ = private_conversation(admin, member_a)
    key = client_id()

    first, created_first = send_message(user=member_a, conversation=conversation, client_id=key, text="hello")
    second, created_second = send_message(user=member_a, conversation=conversation, client_id=key, text="hello")
    assert first.id == second.id and created_first and not created_second

    client = authed(member_a)
    api_key = client_id()
    body = {"client_id": api_key, "text": "retry me"}
    one = client.post(f"/api/conversations/{conversation.id}/messages/", body, format="json")
    two = client.post(f"/api/conversations/{conversation.id}/messages/", body, format="json")
    assert one.status_code == 201 and two.status_code == 200
    assert one.json()["data"]["id"] == two.json()["data"]["id"]
    assert Message.objects.filter(client_id=api_key).count() == 1


@pytest.mark.django_db
def test_ordering_is_backend_authoritative(admin, member_a):
    conversation, _ = private_conversation(admin, member_a)
    client = authed(member_a)
    for index in range(5):
        client.post(
            f"/api/conversations/{conversation.id}/messages/",
            {"client_id": client_id(), "text": f"m{index}"},
            format="json",
        )
    results = client.get(f"/api/conversations/{conversation.id}/messages/").json()["data"]["results"]
    # Newest first, strictly ordered by the server clock.
    texts = [row["text"] for row in results]
    assert texts == ["m4", "m3", "m2", "m1", "m0"]
    stamps = [row["created_at"] for row in results]
    assert stamps == sorted(stamps, reverse=True)


@pytest.mark.django_db
def test_receipts_and_unread_counts(admin, member_a):
    conversation, _ = private_conversation(admin, member_a)
    authed(admin).post(
        f"/api/conversations/{conversation.id}/messages/",
        {"client_id": client_id(), "text": "for the member"},
        format="json",
    )

    member_client = authed(member_a)
    unread = member_client.get("/api/unread/").json()["data"]
    assert unread["conversations"][str(conversation.id)] == 1
    assert unread["global"] == 1

    assert member_client.post(f"/api/conversations/{conversation.id}/read/").status_code == 200
    assert member_client.get("/api/unread/").json()["data"]["global"] == 0

    # The sender now sees READ delivery state — backend-computed, not local.
    message = authed(admin).get(f"/api/conversations/{conversation.id}/messages/").json()["data"]["results"][0]
    assert message["delivery"]["state"] == "READ"
    assert message["status"] == "read"


@pytest.mark.django_db
def test_message_status_reconciliation_endpoint(admin, member_a):
    conversation, _ = private_conversation(admin, member_a)
    created = authed(member_a).post(
        f"/api/conversations/{conversation.id}/messages/",
        {"client_id": client_id(), "text": "reconcile"},
        format="json",
    ).json()["data"]

    body = authed(member_a).post("/api/messages/status/", {"ids": [created["id"]]}, format="json")
    assert body.status_code == 200
    assert body.json()["data"]["results"][0]["id"] == created["id"]


@pytest.mark.django_db
def test_delete_for_self_hides_only_for_that_user(admin, member_a):
    conversation, _ = private_conversation(admin, member_a)
    message, _ = send_message(user=admin, conversation=conversation, client_id=client_id(), text="keep")

    assert authed(member_a).delete(f"/api/messages/{message.id}/", {"scope": "self"}, format="json").status_code == 200
    assert authed(member_a).get(f"/api/conversations/{conversation.id}/messages/").json()["data"]["results"] == []
    assert len(authed(admin).get(f"/api/conversations/{conversation.id}/messages/").json()["data"]["results"]) == 1


@pytest.mark.django_db
def test_reply_and_reaction_round_trip(admin, member_a):
    conversation, _ = private_conversation(admin, member_a)
    parent, _ = send_message(user=admin, conversation=conversation, client_id=client_id(), text="parent")

    client = authed(member_a)
    reply = client.post(
        f"/api/conversations/{conversation.id}/messages/",
        {"client_id": client_id(), "text": "child", "reply_to": str(parent.id)},
        format="json",
    )
    assert reply.status_code == 201
    assert reply.json()["data"]["reply_to"]["id"] == str(parent.id)

    assert client.post(f"/api/messages/{parent.id}/reactions/", {"reaction": "LIKE"}, format="json").status_code == 201
    listing = client.get(f"/api/conversations/{conversation.id}/messages/").json()["data"]["results"]
    reactions = [row["reactions"] for row in listing if row["id"] == str(parent.id)][0]
    assert reactions == [{"user_id": str(member_a.id), "reaction": "LIKE"}]

    assert client.delete(f"/api/messages/{parent.id}/reactions/?reaction=LIKE").status_code == 200


@pytest.mark.django_db
def test_message_length_limit_is_enforced_from_database_settings(admin, member_a, settings):
    from apps.platform_settings.admin_views import ensure_configuration
    from apps.platform_settings.services import invalidate

    row = ensure_configuration()
    row.max_message_length = 100
    row.save(update_fields=["max_message_length"])
    invalidate()

    conversation, _ = private_conversation(admin, member_a)
    response = authed(member_a).post(
        f"/api/conversations/{conversation.id}/messages/",
        {"client_id": client_id(), "text": "x" * 500},
        format="json",
    )
    assert response.status_code == 400
