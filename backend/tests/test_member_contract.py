"""Member creation/update contract.

The frontend creates members with ``display_name`` (+ phone, optional email,
optional is_active). The backend serializer maps that explicitly onto the
model's ``full_name`` — the historical bug was a serializer that *required*
``full_name`` while the UI sent ``display_name``, so every creation failed
with a field error the form never displayed.
"""

from __future__ import annotations

import pytest

from apps.accounts.models import User
from tests.conftest import authed


# ---------------------------------------------------------------- creation


@pytest.mark.django_db
def test_valid_member_is_created_from_the_frontend_payload(admin):
    response = authed(admin).post(
        "/api/members/",
        {"display_name": "Ada Obi", "phone": "+234 801 234 5678", "is_active": True},
        format="json",
    )
    assert response.status_code == 201, response.data
    body = response.json()
    assert body["success"] is True
    member = User.objects.get(phone="+2348012345678")
    assert member.full_name == "Ada Obi"
    assert member.role == "MEMBER"
    assert member.is_active is True
    # The response carries the canonical display projection.
    assert body["data"]["display_name"] == "Ada Obi"
    assert body["data"]["phone"] == "+2348012345678"


@pytest.mark.django_db
def test_full_name_is_accepted_as_an_explicit_alias(admin):
    response = authed(admin).post(
        "/api/members/", {"full_name": "Grace Ade", "phone": "+2348023456789"}, format="json"
    )
    assert response.status_code == 201
    assert User.objects.filter(phone="+2348023456789", full_name="Grace Ade").exists()


@pytest.mark.django_db
def test_missing_name_is_a_display_name_field_error(admin):
    response = authed(admin).post("/api/members/", {"phone": "+2348012345678"}, format="json")
    assert response.status_code == 400
    errors = response.json()["errors"]
    assert "display_name" in errors or "full_name" in errors


@pytest.mark.django_db
def test_invalid_phone_is_reported_on_the_phone_field(admin):
    response = authed(admin).post(
        "/api/members/", {"display_name": "A", "phone": "not-a-phone"}, format="json"
    )
    assert response.status_code == 400
    errors = response.json()["errors"]
    assert "phone" in errors


@pytest.mark.django_db
def test_duplicate_phone_is_a_clear_conflict_on_the_phone_field(admin, member_a):
    response = authed(admin).post(
        "/api/members/",
        {"display_name": "Impostor", "phone": member_a.phone},
        format="json",
    )
    assert response.status_code == 400
    errors = response.json()["errors"]
    assert "phone" in errors
    assert any("already exists" in str(item) for item in errors["phone"])


@pytest.mark.django_db
def test_invalid_email_is_reported_on_the_email_field(admin):
    response = authed(admin).post(
        "/api/members/", {"display_name": "A", "phone": "+2348012345678", "email": "nope"}, format="json"
    )
    assert response.status_code == 400
    assert "email" in response.json()["errors"]


@pytest.mark.django_db
def test_is_active_false_creates_an_inactive_account(admin):
    response = authed(admin).post(
        "/api/members/",
        {"display_name": "Paused", "phone": "+2348034567890", "is_active": False},
        format="json",
    )
    assert response.status_code == 201
    assert User.objects.get(phone="+2348034567890").is_active is False


@pytest.mark.django_db
def test_nigerian_and_international_numbers_are_normalized(admin):
    for raw, expected in [
        ("+234 801 234 5678", "+2348012345678"),
        ("0802 345 6789", "+2348023456789"),
        ("0803-456-7890", "+2348034567890"),
        ("+44 20 7946 0958", "+442079460958"),
    ]:
        response = authed(admin).post(
            "/api/members/", {"display_name": "N", "phone": raw}, format="json"
        )
        assert response.status_code == 201, (raw, response.data)
        assert User.objects.filter(phone=expected).exists()


# ------------------------------------------------------------ authorization


@pytest.mark.django_db
def test_a_member_cannot_create_members(member_a):
    response = authed(member_a).post(
        "/api/members/", {"display_name": "X", "phone": "+2348055555555"}, format="json"
    )
    assert response.status_code == 403
    assert not User.objects.filter(phone="+2348055555555").exists()


@pytest.mark.django_db
def test_anonymous_requests_are_refused(db):
    from rest_framework.test import APIClient

    response = APIClient().post(
        "/api/members/", {"display_name": "X", "phone": "+2348055555555"}, format="json"
    )
    assert response.status_code == 401


# ------------------------------------------------------------------ update


@pytest.mark.django_db
def test_member_update_accepts_display_name_and_phone(admin, member_a):
    response = authed(admin).patch(
        f"/api/members/{member_a.id}/",
        {"display_name": "Member A Renamed", "phone": "+2348099988877"},
        format="json",
    )
    assert response.status_code == 200, response.data
    member_a.refresh_from_db()
    assert member_a.full_name == "Member A Renamed"
    assert member_a.phone == "+2348099988877"


@pytest.mark.django_db
def test_member_update_rejects_a_duplicate_phone(admin, member_a, member_b):
    response = authed(admin).patch(
        f"/api/members/{member_a.id}/", {"phone": member_b.phone}, format="json"
    )
    assert response.status_code == 400
    assert "phone" in response.json()["errors"]


@pytest.mark.django_db
def test_member_update_rejects_an_invalid_phone(admin, member_a):
    response = authed(admin).patch(
        f"/api/members/{member_a.id}/", {"phone": "123"}, format="json"
    )
    assert response.status_code == 400
    assert "phone" in response.json()["errors"]


# ------------------------------------------------------- presence batching


@pytest.mark.django_db
def test_member_list_presence_uses_one_batched_cache_read(admin, member_a, member_b, monkeypatch):
    from django.core.cache import cache

    cache.set(f"presence:{member_a.id}", 1, 60)

    seen = {"get_many": 0, "stray_presence_gets": 0, "inside_batch": False}
    real_get_many = cache.get_many
    real_get = cache.get

    def counting_get_many(keys, *args, **kwargs):
        seen["get_many"] += 1
        seen["inside_batch"] = True
        try:
            return real_get_many(keys, *args, **kwargs)
        finally:
            seen["inside_batch"] = False

    def counting_get(key, *args, **kwargs):
        # The local-memory backend implements get_many by looping get(), so
        # gets *inside* a batch are fine — only direct per-row presence
        # lookups outside the batch are the N+1 this test guards against.
        if str(key).startswith("presence:") and not seen["inside_batch"]:
            seen["stray_presence_gets"] += 1
        return real_get(key, *args, **kwargs)

    monkeypatch.setattr(cache, "get_many", counting_get_many)
    monkeypatch.setattr(cache, "get", counting_get)

    response = authed(admin).get("/api/members/")
    assert response.status_code == 200
    body = response.json()
    items = body["data"]["results"] if isinstance(body["data"], dict) else body["data"]
    assert len(items) >= 2
    # The whole page shares ONE batched read — no per-row cache lookups.
    assert seen["get_many"] == 1
    assert seen["stray_presence_gets"] == 0
    online = {row["phone"]: row["online"] for row in items}
    assert online["+2348012345678"] is True
    assert online["+2348098765432"] is False
