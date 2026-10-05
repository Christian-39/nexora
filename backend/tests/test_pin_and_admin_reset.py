"""Comprehensive tests for first-login PIN setup, voluntary PIN change, and admin PIN reset."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import DeviceSession, User
from apps.accounts.services import create_member, initial_pin
from apps.audit.models import AuditLog
from apps.security.models import SecurityEvent
from tests.conftest import authed
from tests.test_sessions_and_messages import login


def csrf_post(client, path, payload):
    if "csrftoken" not in client.cookies:
        client.get("/api/auth/csrf/")
    token = client.cookies["csrftoken"].value
    return client.post(path, payload, format="json", HTTP_X_CSRFTOKEN=token)


@pytest.mark.django_db
def test_first_login_pin_change_succeeds_with_only_new_and_confirm_pin(admin):
    """Regression test for Critical Bug #1:
    New member logs in with initial PIN and sets a new PIN on the
    'Set a new PIN to continue' screen sending only {new_pin, confirm_pin}.
    """
    member = create_member(actor=admin, phone="08069871234", full_name="Adaeze Okafor")
    assert member.phone == "+2348069871234"
    assert initial_pin(member.phone) == "234806"
    assert member.must_change_pin is True
    assert member.credential_state == User.Credential.INITIAL

    client = APIClient()
    login_resp = login(client, member, "234806")
    assert login_resp.status_code == 200
    assert login_resp.json()["data"]["must_change_pin"] is True

    pre_session = DeviceSession.objects.get(user=member, revoked_at__isnull=True)

    # First-login screen sends ONLY new_pin and confirm_pin (no current_pin).
    resp = csrf_post(
        client,
        "/api/auth/change-pin/",
        {"new_pin": "482915", "confirm_pin": "482915"},
    )
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["must_change_pin"] is False
    assert body["data"]["credential_state"] == User.Credential.CHANGED

    member.refresh_from_db()
    assert member.must_change_pin is False
    assert member.credential_state == User.Credential.CHANGED
    assert member.check_password("482915")
    assert not member.check_password("234806")
    assert member.password != "482915"  # hashed, never plaintext

    # Prior session was revoked and a fresh session cookie was issued so the
    # member proceeds directly to the dashboard without being signed out.
    pre_session.refresh_from_db()
    assert pre_session.revoked_at is not None
    assert DeviceSession.objects.filter(user=member, revoked_at__isnull=True).count() == 1

    me_resp = client.get("/api/me/")
    assert me_resp.status_code == 200
    assert me_resp.json()["data"]["must_change_pin"] is False

    conv_resp = client.get("/api/conversations/")
    assert conv_resp.status_code == 200


@pytest.mark.django_db
def test_first_login_pin_change_rejects_mismatched_weak_and_reused_initial_pins(admin):
    member = create_member(actor=admin, phone="+2348039485712", full_name="Chinedu Eze")
    client = APIClient()
    login(client, member, "234803")

    # 1. Mismatched confirm_pin
    mismatch = csrf_post(
        client,
        "/api/auth/change-pin/",
        {"new_pin": "482915", "confirm_pin": "482916"},
    )
    assert mismatch.status_code == 400
    assert "confirm_pin" in mismatch.json()["errors"]

    # 2. Non-6-digit PINs (5 digits, 7 digits, alphabetic)
    for bad_pin in ("12345", "1234567", "abcdef"):
        bad = csrf_post(
            client,
            "/api/auth/change-pin/",
            {"new_pin": bad_pin, "confirm_pin": bad_pin},
        )
        assert bad.status_code == 400
        assert "new_pin" in bad.json()["errors"]

    # 3. Repeated digits and ascending/descending sequences
    for weak_pin in ("000000", "111111", "123456", "654321", "987654"):
        weak = csrf_post(
            client,
            "/api/auth/change-pin/",
            {"new_pin": weak_pin, "confirm_pin": weak_pin},
        )
        assert weak.status_code == 400
        assert "new_pin" in weak.json()["errors"]

    # 4. Reusing the initial phone-derived PIN ("234803")
    from django.core.cache import cache

    cache.clear()
    reused = csrf_post(
        client,
        "/api/auth/change-pin/",
        {"new_pin": "234803", "confirm_pin": "234803"},
    )
    assert reused.status_code == 400
    assert "new_pin" in reused.json()["errors"]

    member.refresh_from_db()
    assert member.must_change_pin is True


@pytest.mark.django_db
def test_voluntary_pin_change_requires_current_pin_and_keeps_session_alive(member_a):
    member_a.set_password("482915")
    member_a.credential_state = User.Credential.CHANGED
    member_a.save(update_fields=["password", "credential_state"])

    client = APIClient()
    login(client, member_a, "482915")

    # Missing current_pin when must_change_pin is False -> rejected with 400 on current_pin
    missing_current = csrf_post(
        client,
        "/api/auth/change-pin/",
        {"new_pin": "739184", "confirm_pin": "739184"},
    )
    assert missing_current.status_code == 400
    assert "current_pin" in missing_current.json()["errors"]

    # Wrong current_pin -> rejected with 400
    wrong_current = csrf_post(
        client,
        "/api/auth/change-pin/",
        {"current_pin": "918273", "new_pin": "739184", "confirm_pin": "739184"},
    )
    assert wrong_current.status_code == 400
    assert "current_pin" in wrong_current.json()["errors"]

    # Valid current_pin + new_pin + confirm_pin -> succeeds and keeps caller authenticated
    ok = csrf_post(
        client,
        "/api/auth/change-pin/",
        {"current_pin": "482915", "new_pin": "739184", "confirm_pin": "739184"},
    )
    assert ok.status_code == 200
    member_a.refresh_from_db()
    assert member_a.check_password("739184")
    assert client.get("/api/me/").status_code == 200


@pytest.mark.django_db
def test_admin_can_reset_member_pin_and_member_must_change_it_on_next_login(admin, member_a):
    # Member has previously changed their PIN and is currently locked out with an active session.
    member_a.set_password("739184")
    member_a.credential_state = User.Credential.CHANGED
    member_a.failed_login_count = 5
    member_a.locked_until = timezone.now() + timedelta(minutes=15)
    member_a.save(update_fields=["password", "credential_state", "failed_login_count", "locked_until"])

    member_client = APIClient()
    # Create an active session for member_a before reset
    DeviceSession.objects.create(
        user=member_a,
        jti="active-before-reset",
        expires_at=timezone.now() + timedelta(days=1),
    )

    admin_client = authed(admin)
    reset_resp = admin_client.post(f"/api/members/{member_a.id}/reset-pin/", {}, format="json")
    assert reset_resp.status_code == 200, reset_resp.json()
    reset_data = reset_resp.json()["data"]
    assert reset_data["id"] == str(member_a.id)
    assert reset_data["must_change_pin"] is True
    assert reset_data["credential_state"] == User.Credential.RESET_REQUIRED
    # Raw PIN / password is never exposed in the response
    assert "initial_pin" not in reset_data
    assert "pin" not in reset_data
    assert "password" not in reset_data

    member_a.refresh_from_db()
    assert member_a.credential_state == User.Credential.RESET_REQUIRED
    assert member_a.must_change_pin is True
    assert member_a.failed_login_count == 0
    assert member_a.locked_until is None
    assert member_a.check_password(initial_pin(member_a.phone))  # "234801"
    assert not member_a.device_sessions.filter(revoked_at__isnull=True).exists()
    assert AuditLog.objects.filter(actor=admin, action="CREDENTIAL_RESET", object_id=str(member_a.id)).exists()
    assert SecurityEvent.objects.filter(event="CREDENTIAL_RESET", user=member_a).exists()

    # Member can now sign in with the reset PIN ("234801") and is forced to change it
    relogin = login(member_client, member_a, initial_pin(member_a.phone))
    assert relogin.status_code == 200
    assert relogin.json()["data"]["must_change_pin"] is True

    # Member sets a new PIN without needing current_pin
    changed = csrf_post(
        member_client,
        "/api/auth/change-pin/",
        {"new_pin": "592837", "confirm_pin": "592837"},
    )
    assert changed.status_code == 200
    assert changed.json()["data"]["must_change_pin"] is False


@pytest.mark.django_db
def test_regular_member_cannot_reset_any_pin(member_a, member_b):
    member_a.credential_state = User.Credential.CHANGED
    member_a.save(update_fields=["credential_state"])
    client = authed(member_a)

    resp_other = client.post(f"/api/members/{member_b.id}/reset-pin/", {}, format="json")
    assert resp_other.status_code == 403

    resp_self = client.post(f"/api/members/{member_a.id}/reset-pin/", {}, format="json")
    assert resp_self.status_code == 403


@pytest.mark.django_db
def test_member_list_supports_is_active_boolean_filter(admin, member_a, member_b):
    member_b.is_active = False
    member_b.save(update_fields=["is_active"])

    client = authed(admin)
    active_payload = client.get("/api/members/?is_active=true").json()["data"]
    active_rows = active_payload["results"] if isinstance(active_payload, dict) else active_payload
    active_ids = {row["id"] for row in active_rows}
    assert str(member_a.id) in active_ids
    assert str(member_b.id) not in active_ids

    inactive_payload = client.get("/api/members/?is_active=false").json()["data"]
    inactive_rows = inactive_payload["results"] if isinstance(inactive_payload, dict) else inactive_payload
    inactive_ids = {row["id"] for row in inactive_rows}
    assert str(member_b.id) in inactive_ids
    assert str(member_a.id) not in inactive_ids
