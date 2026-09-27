"""Database-driven settings, branding, push subscriptions and the dashboard."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from apps.notifications.models import Notification, PushDelivery, PushSubscription
from apps.platform_settings.models import PlatformConfiguration
from tests.conftest import authed, client_id, jpeg_bytes

ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"


# ---------------------------------------------------------------------------
# Admin settings
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_only_admin_can_read_or_write_settings(admin, member_a):
    assert authed(member_a).get("/api/settings/").status_code == 403
    assert authed(member_a).patch("/api/settings/", {"app_name": "Nope"}, format="json").status_code == 403
    assert authed(admin).get("/api/settings/").status_code == 200


@pytest.mark.django_db
def test_settings_update_changes_backend_behaviour_and_public_config(admin, member_a):
    client = authed(admin)
    response = client.patch(
        "/api/settings/",
        {
            "organization": {"organization_name": "Harbour Logistics", "contact_phone": "+2348000000000"},
            "branding": {"app_name": "Harbour", "app_short_name": "HRB", "primary_color": "#112233"},
            "messaging": {"max_message_length": 120, "reactions": False, "max_image_size": 3 * 1024**2},
        },
        format="json",
    )
    assert response.status_code == 200, response.data

    row = PlatformConfiguration.objects.get()
    assert row.organization_name == "Harbour Logistics"
    assert row.max_message_length == 120
    assert row.allow_reactions is False
    assert row.max_image_size_mb == 3

    # The public document the frontend consumes reflects it immediately.
    public = APIClient().get("/api/public/config/").json()["data"]
    assert public["app_name"] == "Harbour"
    assert public["app_short_name"] == "HRB"
    assert public["primary_color"] == "#112233"
    assert public["limits"]["max_message_length"] == 120
    assert public["limits"]["max_image_size"] == 3 * 1024**2
    assert public["features"]["reactions"] is False
    assert public["pwa"]["name"] == "Harbour Logistics"


@pytest.mark.django_db
def test_invalid_colour_is_rejected(admin):
    response = authed(admin).patch("/api/settings/", {"primary_color": "red; drop table"}, format="json")
    assert response.status_code == 400


@pytest.mark.django_db
def test_settings_ignore_unknown_and_infrastructure_keys(admin, settings):
    response = authed(admin).patch(
        "/api/settings/",
        {"app_name": "Kept", "SECRET_KEY": "pwned", "DATABASE_URL": "mysql://x", "REDIS_URL": "redis://x"},
        format="json",
    )
    assert response.status_code == 200
    assert PlatformConfiguration.objects.get().app_name == "Kept"
    assert settings.SECRET_KEY != "pwned"


@pytest.mark.django_db
def test_policies_round_trip(admin, member_a):
    client = authed(admin)
    assert client.patch(
        "/api/settings/policies/",
        {"policies": [{"key": "privacy", "body": "We keep your data private."}]},
        format="json",
    ).status_code == 200

    # Members can read policies but not write them.
    read = authed(member_a).get("/api/settings/policies/")
    assert read.status_code == 200
    bodies = {p["key"]: p["body"] for p in read.json()["data"]["policies"]}
    assert bodies["privacy"] == "We keep your data private."
    assert authed(member_a).patch(
        "/api/settings/policies/", {"policies": [{"key": "privacy", "body": "x"}]}, format="json"
    ).status_code == 403


@pytest.mark.django_db
def test_branding_asset_upload_and_public_serving(admin, member_a, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    response = authed(admin).post(
        "/api/settings/assets/logo/", {"file": jpeg_bytes(size=(64, 64))}, format="multipart"
    )
    assert response.status_code in (200, 201), response.data

    public = APIClient().get("/api/public/config/").json()["data"]
    assert public["logo_url"].endswith("/api/public/branding/logo/")
    assert APIClient().get("/api/public/branding/logo/").status_code == 200

    assert authed(member_a).post(
        "/api/settings/assets/logo/", {"file": jpeg_bytes()}, format="multipart"
    ).status_code == 403


@pytest.mark.django_db
def test_security_policy_is_admin_only_and_validated(admin, member_a):
    assert authed(member_a).get("/api/security/").status_code == 403

    client = authed(admin)
    assert client.patch("/api/security/", {"max_login_attempts": 999}, format="json").status_code == 400

    updated = client.patch(
        "/api/security/", {"max_login_attempts": 4, "lockout_minutes": 30}, format="json"
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["max_login_attempts"] == 4
    assert PlatformConfiguration.objects.get().login_failure_limit == 4


@pytest.mark.django_db
def test_security_events_listing_is_admin_only(admin, member_a):
    assert authed(member_a).get("/api/security/events/").status_code == 403
    assert authed(admin).get("/api/security/events/").status_code == 200


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_dashboard_reports_real_counts(admin, member_a, member_b, private_thread):
    from apps.conversations.services import send_message

    send_message(user=admin, conversation=private_thread, client_id=client_id(), text="one")
    assert authed(member_a).get("/api/dashboard/").status_code == 403

    data = authed(admin).get("/api/dashboard/").json()["data"]
    assert data["members"]["total"] == 2
    assert data["members"]["active"] == 2
    assert data["members"]["pending_pin"] >= 1

    # Flat, front-end-facing keys (admin.html reads these) must mirror the nested
    # structure and expose real numbers, not placeholders. This is the contract
    # whose mismatch previously left every dashboard metric showing an em dash.
    assert data["total_members"] == 2
    assert data["active_members"] == 2
    assert data["inactive_members"] == 0
    assert data["active_conversations"] >= 1
    assert data["total_groups"] == 0
    # The admin just sent one message today.
    assert data["messages_today"] >= 1
    for key in ("unread_conversations", "media_today", "voice_notes_today"):
        assert key in data and isinstance(data[key], int)
    assert data["messages"]["total"] == 1
    assert data["conversations"]["total"] == 1


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_push_config_exposes_only_the_public_key(member_a, settings):
    settings.PUSH_PUBLIC_KEY = "BPublicKeyValue"
    settings.PUSH_PRIVATE_KEY = "PRIVATE-DO-NOT-LEAK"

    body = authed(member_a).get("/api/push/").json()["data"]
    assert body["vapid_public_key"] == "BPublicKeyValue"
    assert body["enabled"] is True
    assert "PRIVATE-DO-NOT-LEAK" not in str(body)


@pytest.mark.django_db
def test_push_subscribe_and_unsubscribe(member_a):
    client = authed(member_a)
    response = client.post(
        "/api/push/subscribe/",
        {
            "subscription": {"endpoint": ENDPOINT, "keys": {"p256dh": "key-p", "auth": "key-a"}},
            "user_agent": "pytest",
        },
        format="json",
    )
    assert response.status_code == 201
    assert "key-p" not in str(response.data)  # key material is write-only

    subscription = PushSubscription.objects.get()
    assert subscription.user == member_a and subscription.is_active

    # Re-subscribing with the same endpoint updates instead of duplicating.
    client.post(
        "/api/push/subscribe/",
        {"subscription": {"endpoint": ENDPOINT, "keys": {"p256dh": "key-p2", "auth": "key-a2"}}},
        format="json",
    )
    assert PushSubscription.objects.count() == 1

    assert client.post("/api/push/unsubscribe/", {"endpoint": ENDPOINT}, format="json").status_code == 200
    subscription.refresh_from_db()
    assert subscription.is_active is False


@pytest.mark.django_db
def test_push_subscription_rejects_non_https_endpoint(member_a):
    response = authed(member_a).post(
        "/api/push/subscribe/",
        {"subscription": {"endpoint": "http://evil.test/x", "keys": {"p256dh": "a", "auth": "b"}}},
        format="json",
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_a_user_cannot_see_another_users_subscriptions(member_a, member_b):
    authed(member_a).post(
        "/api/push/subscribe/",
        {"subscription": {"endpoint": ENDPOINT, "keys": {"p256dh": "a", "auth": "b"}}},
        format="json",
    )
    assert authed(member_b).get("/api/push/").json()["data"]["subscriptions"] == []


@pytest.mark.django_db
def test_notifications_aggregate_within_the_window(admin, member_a, private_thread, settings):
    settings.PUSH_PRIVATE_KEY = "test-private-key"
    from apps.conversations.services import send_message

    authed(member_a)  # ensure credential_state is CHANGED
    PushSubscription.objects.create(
        user=member_a, endpoint=ENDPOINT, p256dh="a", auth="b", is_active=True
    )

    for index in range(3):
        send_message(user=admin, conversation=private_thread, client_id=client_id(), text=f"m{index}")

    notifications = Notification.objects.filter(recipient=member_a, type="NEW_MESSAGE")
    assert notifications.count() == 1
    row = notifications.get()
    assert row.aggregate_count == 3
    assert row.message == "3 new messages from Administrator"
    # Exactly one pending delivery, refreshed rather than duplicated.
    assert PushDelivery.objects.filter(notification=row, state="PENDING").count() == 1


@pytest.mark.django_db
def test_notification_previews_can_be_disabled_by_policy(admin, member_a, private_thread):
    from apps.conversations.services import send_message
    from apps.platform_settings.admin_views import ensure_configuration
    from apps.platform_settings.services import invalidate

    row = ensure_configuration()
    row.notification_previews = False
    row.notification_aggregation_window_seconds = 0
    row.save(update_fields=["notification_previews", "notification_aggregation_window_seconds"])
    invalidate()

    send_message(user=admin, conversation=private_thread, client_id=client_id(), text="secret content")
    notification = Notification.objects.filter(recipient=member_a).latest("created_at")
    assert "secret content" not in notification.message
    assert notification.message == "You have a new message."


@pytest.mark.django_db
def test_notification_read_and_unread_count(admin, member_a, private_thread):
    from apps.conversations.services import send_message

    send_message(user=admin, conversation=private_thread, client_id=client_id(), text="hi")
    client = authed(member_a)

    assert client.get("/api/notifications/unread-count/").json()["data"]["notifications"] == 1
    assert client.post("/api/notifications/read/", {"all": True}, format="json").status_code == 200
    assert client.get("/api/notifications/unread-count/").json()["data"]["notifications"] == 0


@pytest.mark.django_db
def test_gone_endpoint_is_pruned_on_delivery_failure(admin, member_a, private_thread, settings, monkeypatch):
    settings.PUSH_PRIVATE_KEY = "test-private-key"
    from apps.conversations.services import send_message
    from apps.notifications import services

    subscription = PushSubscription.objects.create(
        user=member_a, endpoint=ENDPOINT, p256dh="a", auth="b", is_active=True
    )
    send_message(user=admin, conversation=private_thread, client_id=client_id(), text="hi")
    delivery = PushDelivery.objects.get()

    class Gone(Exception):
        response = type("R", (), {"status_code": 410})()

    def boom(*args, **kwargs):
        raise Gone()

    monkeypatch.setattr(services, "deliver", services.deliver)
    import pywebpush

    monkeypatch.setattr(pywebpush, "webpush", boom)
    monkeypatch.setattr(pywebpush, "WebPushException", Gone)

    services.deliver(delivery)
    delivery.refresh_from_db()
    subscription.refresh_from_db()
    assert delivery.state == "FAILED"
    assert subscription.is_active is False
