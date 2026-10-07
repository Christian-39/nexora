"""Relay-required media responses never disclose private object-store URLs."""

from types import SimpleNamespace

import pytest

from apps.media import services as media_services


def _attachment():
    return SimpleNamespace(
        storage_key="media/originals/private-object.bin",
        thumbnail_key="media/thumbnails/private-object.webp",
        optimized_key="media/optimized/private-object.webp",
    )


def test_relay_mode_uses_authenticated_media_routes_instead_of_signed_storage_urls(settings, monkeypatch):
    settings.SECURITY_RELAY_REQUIRED = True

    class StorageMustNotBeQueried:
        def url(self, key):
            raise AssertionError(f"object-store URL must not be requested for {key}")

    monkeypatch.setattr(media_services, "default_storage", StorageMustNotBeQueried())
    attachment = _attachment()

    assert media_services.signed_url(attachment, "original") is None
    assert media_services.signed_url(attachment, "thumbnail") is None
    assert media_services.signed_url(attachment, "optimized") is None


@pytest.mark.django_db
def test_relay_required_media_url_stays_on_the_authorized_api_route(admin, member_a, private_thread, settings):
    from apps.conversations.models import Attachment
    from apps.conversations.services import send_message
    from tests.conftest import authed, client_id

    settings.SECURITY_RELAY_REQUIRED = True
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
    assert media_url.endswith(f"/api/media/{attachment.id}/")
    assert "private-object" not in media_url
    assert "signature" not in media_url


@pytest.mark.django_db
def test_relay_required_media_stream_preserves_authenticated_range_requests(admin, member_a, private_thread, settings, monkeypatch):
    from io import BytesIO

    from apps.conversations.models import Attachment
    from apps.conversations.services import send_message
    from apps.media import views as media_views
    from tests.conftest import authed, client_id

    settings.SECURITY_RELAY_REQUIRED = True
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


def test_non_relay_mode_keeps_existing_signed_url_support(settings, monkeypatch):
    settings.SECURITY_RELAY_REQUIRED = False

    class Storage:
        @staticmethod
        def url(key):
            return f"https://objects.example.test/private/{key}?signature=short-lived"

    monkeypatch.setattr(media_services, "default_storage", Storage())

    result = media_services.signed_url(_attachment(), "original")

    assert result == "https://objects.example.test/private/media/originals/private-object.bin?signature=short-lived"
