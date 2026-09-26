"""Credentials, lockout, CSRF, secret exposure and IDOR."""

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.accounts.services import create_member, initial_pin
from apps.conversations.services import private_conversation, send_message
from tests.conftest import authed, client_id


@pytest.mark.django_db
def test_initial_pin_rule_and_duplicate_phone_rejected(admin):
    member = create_member(actor=admin, phone="+2348012345678", full_name="A")
    assert initial_pin(member.phone) == "234801"
    assert member.check_password("234801")
    assert member.credential_state == "INITIAL"
    with pytest.raises(Exception):
        create_member(actor=admin, phone="+2348012345678", full_name="B")


@pytest.mark.django_db
def test_member_cannot_create_member(member_a, member_b):
    with pytest.raises(Exception):
        create_member(actor=member_a, phone="+2348011111111", full_name="X")


@pytest.mark.django_db
def test_member_cannot_private_message_member(member_a, member_b):
    with pytest.raises(Exception):
        private_conversation(member_a, member_b)


@pytest.mark.django_db
def test_admin_to_member_and_member_to_admin_are_the_same_thread(admin, member_a):
    first, created_first = private_conversation(admin, member_a)
    second, created_second = private_conversation(member_a, admin)
    assert first.id == second.id
    assert created_first and not created_second


@pytest.mark.django_db
def test_login_rejects_bad_pin_and_locks_after_configured_failures(member_a):
    client = APIClient()
    client.get("/api/auth/csrf/")
    token = client.cookies["csrftoken"].value
    for _ in range(5):
        response = client.post(
            "/api/auth/login/",
            {"phone": member_a.phone, "pin": "000000"},
            format="json",
            HTTP_X_CSRFTOKEN=token,
        )
        assert response.status_code == 401
        assert "pin" not in str(response.data).lower() or response.data["code"] == "AUTH_FAILED"

    member_a.refresh_from_db()
    assert member_a.locked_until and member_a.locked_until > timezone.now()

    # Even the correct PIN is refused while the lockout holds.
    assert client.post(
        "/api/auth/login/", {"phone": member_a.phone, "pin": "123456"}, format="json", HTTP_X_CSRFTOKEN=token
    ).status_code == 401


@pytest.mark.django_db
def test_login_requires_csrf(member_a):
    client = APIClient(enforce_csrf_checks=True)
    response = client.post("/api/auth/login/", {"phone": member_a.phone, "pin": "123456"}, format="json")
    assert response.status_code == 403


@pytest.mark.django_db
def test_json_csrf_token_supports_cross_origin_auth_lifecycle(member_a, settings):
    """The Vercel client can use the JSON token without reading Render cookies."""
    origin = "https://nexora-eight-lilac.vercel.app"
    settings.CORS_ALLOWED_ORIGINS = [origin]
    settings.CSRF_TRUSTED_ORIGINS = [origin]
    settings.CORS_ALLOW_CREDENTIALS = True

    client = APIClient(enforce_csrf_checks=True)
    bootstrap = client.get("/api/auth/csrf/", HTTP_ORIGIN=origin)
    assert bootstrap.status_code == 200
    token = bootstrap.json()["data"]["csrf_token"]
    assert isinstance(token, str) and token
    assert "csrftoken" in bootstrap.cookies
    assert bootstrap["Access-Control-Allow-Origin"] == origin
    assert bootstrap["Access-Control-Allow-Credentials"] == "true"

    login_response = client.post(
        "/api/auth/login/",
        {"phone": member_a.phone, "pin": "123456"},
        format="json",
        HTTP_ORIGIN=origin,
        HTTP_X_CSRFTOKEN=token,
    )
    assert login_response.status_code == 200
    assert login_response.cookies["nexora_access"]["httponly"]
    assert login_response.cookies["nexora_refresh"]["httponly"]
    assert client.get("/api/me/", HTTP_ORIGIN=origin).status_code == 200

    # Refresh remains protected: the cookie by itself is not sufficient.
    assert client.post(
        "/api/auth/refresh/", {}, format="json", HTTP_ORIGIN=origin
    ).status_code == 403
    assert client.post(
        "/api/auth/refresh/",
        {},
        format="json",
        HTTP_ORIGIN=origin,
        HTTP_X_CSRFTOKEN=token,
    ).status_code == 200

    logout_response = client.post(
        "/api/auth/logout/",
        {},
        format="json",
        HTTP_ORIGIN=origin,
        HTTP_X_CSRFTOKEN=token,
    )
    assert logout_response.status_code == 200
    assert logout_response.cookies["nexora_access"]["max-age"] == 0
    assert logout_response.cookies["nexora_refresh"]["max-age"] == 0
    assert client.get("/api/me/", HTTP_ORIGIN=origin).status_code == 401


@pytest.mark.django_db
def test_unsafe_api_request_without_csrf_is_refused(admin, private_thread):
    from rest_framework_simplejwt.tokens import RefreshToken

    from apps.accounts.models import DeviceSession

    refresh = RefreshToken.for_user(admin)
    session = DeviceSession.objects.create(
        user=admin, jti=str(refresh["jti"]), expires_at=timezone.now() + timezone.timedelta(days=1)
    )
    refresh["sid"] = str(session.id)
    client = APIClient(enforce_csrf_checks=True)
    client.cookies["nexora_access"] = str(refresh.access_token)

    response = client.post(
        f"/api/conversations/{private_thread.id}/messages/",
        {"client_id": client_id(), "text": "no csrf"},
        format="json",
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_public_config_exposes_no_secrets(settings):
    settings.PUSH_PRIVATE_KEY = "super-secret-private-key"
    settings.STORAGE_SECRET_KEY = "storage-secret"
    body = str(APIClient().get("/api/public/config/").json())
    for secret in (
        settings.SECRET_KEY,
        "super-secret-private-key",
        "storage-secret",
        "DATABASE_PASSWORD",
        "REDIS",
    ):
        assert secret not in body


@pytest.mark.django_db
def test_audit_and_security_logs_never_store_credentials(admin, member_a):
    from apps.audit.models import AuditLog
    from apps.audit.services import record
    from apps.security.services import event

    record(admin, "TEST", member_a, None, {"pin": "123456", "access_token": "abc", "safe": "ok"})
    row = AuditLog.objects.latest("created_at")
    assert row.metadata == {"safe": "ok"}

    security_row = event("TEST", None, admin, {"password": "hunter2", "note": "fine"})
    assert security_row.metadata == {"note": "fine"}


@pytest.mark.django_db
def test_edit_delete_and_search_authorization(admin, member_a, member_b):
    conversation, _ = private_conversation(admin, member_a)
    message, _ = send_message(
        user=member_a, conversation=conversation, client_id=client_id(), text="searchable hello"
    )

    # Another member's search cannot reach it.
    assert authed(member_b).get("/api/messages/search/?q=searchable").json()["data"]["results"] == []

    assert authed(member_a).patch(
        f"/api/messages/{message.id}/", {"text": "edited hello"}, format="json"
    ).status_code == 200
    assert authed(member_b).patch(
        f"/api/messages/{message.id}/", {"text": "hijacked"}, format="json"
    ).status_code == 404

    assert authed(member_a).delete(
        f"/api/messages/{message.id}/", {"scope": "everyone"}, format="json"
    ).status_code == 200
    message.refresh_from_db()
    assert message.deleted_at is not None and message.text == ""


@pytest.mark.django_db
def test_stored_xss_payload_is_returned_as_data_not_markup(admin, member_a):
    conversation, _ = private_conversation(admin, member_a)
    payload = "<img src=x onerror=alert(1)>"
    response = authed(member_a).post(
        f"/api/conversations/{conversation.id}/messages/",
        {"client_id": client_id(), "text": payload},
        format="json",
    )
    assert response.status_code == 201
    # Stored verbatim as text; escaping is the renderer's job and the frontend
    # only ever uses textContent for message bodies.
    assert response.json()["data"]["text"] == payload
    assert response["Content-Type"].startswith("application/json")


@pytest.mark.django_db
def test_deactivated_member_cannot_authenticate(admin, member_a):
    authed(admin).post(f"/api/members/{member_a.id}/deactivate/")
    member_a.refresh_from_db()
    assert member_a.is_active is False

    client = APIClient()
    client.get("/api/auth/csrf/")
    token = client.cookies["csrftoken"].value
    assert client.post(
        "/api/auth/login/", {"phone": member_a.phone, "pin": "123456"}, format="json", HTTP_X_CSRFTOKEN=token
    ).status_code == 401


@pytest.mark.django_db
def test_security_headers_present():
    response = APIClient().get("/api/public/config/")
    assert response["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in response
    assert "Permissions-Policy" in response
    assert response["Referrer-Policy"] == "same-origin"
