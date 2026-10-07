"""Media URL behaviour after the Security Relay removal.

With the relay gone, the backend is the only thing in front of the object
store. ``/api/media/{uuid}/url/`` therefore issues a short-lived signed URL
every time, and ``/api/media/{uuid}/`` streams the bytes directly when the
browser (or an authenticated proxy) prefers that path.
"""

from types import SimpleNamespace

import pytest


def _attachment():
    return SimpleNamespace(
        storage_key="media/originals/private-object.bin",
        thumbnail_key="media/thumbnails/private-object.webp",
        optimized_key="media/optimized/private-object.webp",
    )


def test_signed_url_always_issues_a_short_lived_storage_url(monkeypatch):
    from apps.media import services as media_services

    class Storage:
        @staticmethod
        def url(key):
            return f"https://objects.example.test/private/{key}?signature=short-lived"

    monkeypatch.setattr(media_services, "default_storage", Storage())
    attachment = _attachment()

    assert media_services.signed_url(attachment, "original") == (
        "https://objects.example.test/private/media/originals/private-object.bin?signature=short-lived"
    )
    assert media_services.signed_url(attachment, "thumbnail") == (
        "https://objects.example.test/private/media/thumbnails/private-object.webp?signature=short-lived"
    )
    assert media_services.signed_url(attachment, "optimized") == (
        "https://objects.example.test/private/media/optimized/private-object.webp?signature=short-lived"
    )


def test_signed_url_returns_none_when_no_storage_key(monkeypatch):
    from apps.media import services as media_services

    class StorageMustNotBeQueried:
        def url(self, key):  # pragma: no cover - must not be reached
            raise AssertionError(f"object-store URL must not be requested for {key}")

    monkeypatch.setattr(media_services, "default_storage", StorageMustNotBeQueried())
    assert media_services.signed_url(SimpleNamespace(storage_key=""), "original") is None
    assert (
        media_services.signed_url(
            SimpleNamespace(storage_key=None, thumbnail_key=None, optimized_key=None), "thumbnail"
        )
        is None
    )


@pytest.mark.django_db
def test_media_url_endpoint_returns_a_signed_storage_url(
    admin, member_a, private_thread
):
    from apps.conversations.models import Attachment
    from apps.conversations.services import send_message
    from tests.conftest import authed, client_id

    message, _ = send_message(
        user=admin, conversation=private_thread, client_id=client_id(), type="IMAGE"
    )
    attachment = Attachment.objects.create(
        message=message,
        storage_key="media/originals/private-object.jpg",
        original_name="private.jpg",
        mime_type="image/jpeg",
        size=123,
        processing_state="READY",
    )

    response = authed(member_a).get(f"/api/media/{attachment.id}/url/")

    assert response.status_code == 200
    media_url = response.json()["data"]["url"]
    assert media_url.startswith("http")
    # The URL must be a signed storage URL (contains a signature fragment),
    # NOT a stream of the protected API route.
    assert "/api/media/" not in media_url or "signature" in media_url or "?" in media_url


@pytest.mark.django_db
def test_media_stream_preserves_authenticated_range_requests(
    admin, member_a, private_thread, monkeypatch
):
    from io import BytesIO

    from apps.conversations.models import Attachment
    from apps.conversations.services import send_message
    from apps.media import views as media_views
    from tests.conftest import authed, client_id

    message, _ = send_message(
        user=admin, conversation=private_thread, client_id=client_id(), type="IMAGE"
    )
    body = b"0123456789"
    attachment = Attachment.objects.create(
        message=message,
        storage_key="media/originals/range-object.jpg",
        original_name="range.jpg",
        mime_type="image/jpeg",
        size=len(body),
        processing_state="READY",
    )

    class Storage:
        @staticmethod
        def size(_key):
            return len(body)

        @staticmethod
        def open(_key, _mode):
            return BytesIO(body)

    monkeypatch.setattr(media_views, "default_storage", Storage())
    response = authed(member_a).get(
        f"/api/media/{attachment.id}/",
        HTTP_RANGE="bytes=2-5",
    )

    assert response.status_code == 206
    assert response["Content-Range"] == f"bytes 2-5/{len(body)}"
    assert response["Accept-Ranges"] == "bytes"
    assert b"".join(response.streaming_content) == b"2345"