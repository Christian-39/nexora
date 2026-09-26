"""Baseline authorization contract (private chat, member listing, media IDOR)."""

import pytest

from apps.conversations.models import Attachment
from tests.conftest import authed, client_id, jpeg_bytes


@pytest.mark.django_db
def test_member_cannot_list_members(users):
    _, member_a, _ = users
    assert authed(member_a).get("/api/members/").status_code == 403


@pytest.mark.django_db
def test_member_cannot_open_conversation_with_another_member(users):
    _, member_a, member_b = users
    response = authed(member_a).post(
        "/api/conversations/", {"participant": str(member_b.id)}, format="json"
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_private_media_is_not_readable_by_another_member(users, private_thread, settings, tmp_path):
    _, member_a, member_b = users
    settings.MEDIA_ROOT = tmp_path

    response = authed(member_a).post(
        f"/api/conversations/{private_thread.id}/messages/",
        {"kind": "image", "client_id": client_id(), "file": jpeg_bytes()},
        format="multipart",
    )
    assert response.status_code == 201, response.data

    attachment = Attachment.objects.get()
    # The owner can read it...
    assert authed(member_a).get(f"/api/media/{attachment.id}/").status_code == 200
    # ...an outsider cannot even learn that it exists.
    assert authed(member_b).get(f"/api/media/{attachment.id}/").status_code == 404
    assert authed(member_b).get(f"/api/media/{attachment.id}/url/").status_code == 404
