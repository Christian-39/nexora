"""Media upload validation, storage safety, authorization and processing."""

from __future__ import annotations

import io

import pytest
from django.core.files.storage import default_storage

from apps.conversations.models import Attachment
from apps.media.validators import safe_display_name, signature_mime, storage_key, validate_upload
from tests.conftest import authed, client_id, jpeg_bytes


def noisy_jpeg_over_mb(megabytes: float) -> io.BytesIO:
    """An incompressible JPEG guaranteed to exceed ``megabytes``."""
    import os

    from PIL import Image

    side = 900
    while True:
        image = Image.frombytes("RGB", (side, side), os.urandom(side * side * 3))
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=100, subsampling=0)
        if buffer.tell() > megabytes * 1024**2:
            buffer.seek(0)
            buffer.name = "big.jpg"
            return buffer
        side = int(side * 1.5)


@pytest.fixture(autouse=True)
def _local_storage(settings, tmp_path, django_capture_on_commit_callbacks):
    settings.MEDIA_ROOT = tmp_path
    settings.MEDIA_PROCESS_INLINE = True
    yield


@pytest.fixture
def run_commit_hooks(django_capture_on_commit_callbacks):
    """Execute ``transaction.on_commit`` work the way a real request would."""

    def runner(fn):
        with django_capture_on_commit_callbacks(execute=True):
            return fn()

    return runner


def upload(client, conversation, **extra):
    payload = {"kind": "image", "client_id": client_id(), "file": jpeg_bytes(), **extra}
    return client.post(f"/api/conversations/{conversation.id}/messages/", payload, format="multipart")


# ---------------------------------------------------------------------------
# Signature detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"\xff\xd8\xff\xe0" + b"0" * 32, "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n" + b"0" * 32, "image/png"),
        (b"RIFF\x00\x00\x00\x00WEBP" + b"0" * 32, "image/webp"),
        (b"GIF89a" + b"0" * 32, "image/gif"),
        (b"\x00\x00\x00\x20ftypisom" + b"0" * 32, "video/mp4"),
        (b"OggS" + b"0" * 32, "audio/ogg"),
        (b"MZ\x90\x00" + b"0" * 32, None),
    ],
)
def test_signature_detection(head, expected):
    assert signature_mime(head) == expected


def test_storage_keys_are_unguessable_and_traversal_proof():
    key = storage_key("media/originals", ".jpg")
    assert key.startswith("media/originals/")
    assert ".." not in key and key.endswith(".jpg")
    assert storage_key("media/originals", ".jpg") != key


def test_display_names_are_sanitised():
    assert safe_display_name("../../etc/passwd") == "passwd"
    assert safe_display_name("photo\x00.jpg") == "photo_.jpg"
    assert safe_display_name("") == "attachment"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_executable_disguised_as_image_is_rejected(private_thread, member_a):
    payload = io.BytesIO(b"MZ\x90\x00" + b"A" * 1024)
    payload.name = "photo.jpg"
    response = authed(member_a).post(
        f"/api/conversations/{private_thread.id}/messages/",
        {"kind": "image", "client_id": client_id(), "file": payload},
        format="multipart",
    )
    assert response.status_code == 400
    assert Attachment.objects.count() == 0


@pytest.mark.django_db
def test_extension_must_match_content(private_thread, member_a):
    image = jpeg_bytes(name="photo.png")
    response = authed(member_a).post(
        f"/api/conversations/{private_thread.id}/messages/",
        {"kind": "image", "client_id": client_id(), "file": image},
        format="multipart",
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_declared_kind_must_match_content(private_thread, member_a):
    response = authed(member_a).post(
        f"/api/conversations/{private_thread.id}/messages/",
        {"kind": "video", "client_id": client_id(), "file": jpeg_bytes()},
        format="multipart",
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_oversized_upload_is_rejected(private_thread, member_a):
    from apps.platform_settings.admin_views import ensure_configuration
    from apps.platform_settings.services import invalidate

    row = ensure_configuration()
    row.max_image_size_mb = 1
    row.save(update_fields=["max_image_size_mb"])
    invalidate()

    big = noisy_jpeg_over_mb(1.5)
    response = authed(member_a).post(
        f"/api/conversations/{private_thread.id}/messages/",
        {"kind": "image", "client_id": client_id(), "file": big},
        format="multipart",
    )
    assert response.status_code in (400, 413)


@pytest.mark.django_db
def test_empty_file_is_rejected(private_thread, member_a):
    empty = io.BytesIO(b"")
    empty.name = "empty.jpg"
    response = authed(member_a).post(
        f"/api/conversations/{private_thread.id}/messages/",
        {"kind": "image", "client_id": client_id(), "file": empty},
        format="multipart",
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Happy path + processing
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_image_upload_creates_message_attachment_and_derivatives(private_thread, member_a, run_commit_hooks):
    response = run_commit_hooks(lambda: upload(authed(member_a), private_thread, caption="On site"))
    assert response.status_code == 201, response.data

    data = response.json()["data"]
    assert data["type"] == "IMAGE"
    assert data["text"] == "On site"
    assert data["media"]["mime_type"] == "image/jpeg"
    assert data["media"]["url"].endswith(f"/api/media/{data['media']['id']}/")

    attachment = Attachment.objects.get()
    # MEDIA_PROCESS_INLINE runs the same code path as the worker.
    attachment.refresh_from_db()
    assert attachment.processing_state == "READY"
    assert attachment.thumbnail_key and default_storage.exists(attachment.thumbnail_key)
    assert attachment.optimized_key and default_storage.exists(attachment.optimized_key)
    assert attachment.width == 8 and attachment.height == 8
    # The client filename never becomes the storage path.
    assert "photo.jpg" not in attachment.storage_key


@pytest.mark.django_db
def test_media_upload_is_idempotent(private_thread, member_a):
    client = authed(member_a)
    key = client_id()
    first = client.post(
        f"/api/conversations/{private_thread.id}/messages/",
        {"kind": "image", "client_id": key, "file": jpeg_bytes()},
        format="multipart",
    )
    second = client.post(
        f"/api/conversations/{private_thread.id}/messages/",
        {"kind": "image", "client_id": key, "file": jpeg_bytes()},
        format="multipart",
    )
    assert first.status_code == 201 and second.status_code == 200
    assert Attachment.objects.count() == 1


@pytest.mark.django_db
def test_media_variants_and_range_requests(private_thread, member_a, run_commit_hooks):
    run_commit_hooks(lambda: upload(authed(member_a), private_thread))
    attachment = Attachment.objects.get()
    client = authed(member_a)

    full = client.get(f"/api/media/{attachment.id}/")
    assert full.status_code == 200
    assert full["Accept-Ranges"] == "bytes"
    assert full["Cache-Control"].startswith("private")

    ranged = client.get(f"/api/media/{attachment.id}/", HTTP_RANGE="bytes=0-9")
    assert ranged.status_code == 206
    assert ranged["Content-Length"] == "10"
    assert ranged["Content-Range"].startswith("bytes 0-9/")

    assert client.get(f"/api/media/{attachment.id}/?variant=thumbnail").status_code == 200
    assert client.get(f"/api/media/{attachment.id}/?variant=evil").status_code == 400

    unsatisfiable = client.get(f"/api/media/{attachment.id}/", HTTP_RANGE="bytes=999999999-")
    assert unsatisfiable.status_code == 416


@pytest.mark.django_db
def test_media_url_endpoint_requires_authorization(private_thread, member_a, member_b):
    upload(authed(member_a), private_thread)
    attachment = Attachment.objects.get()

    allowed = authed(member_a).get(f"/api/media/{attachment.id}/url/")
    assert allowed.status_code == 200
    assert allowed.json()["data"]["expires_at"]

    assert authed(member_b).get(f"/api/media/{attachment.id}/url/").status_code == 404


@pytest.mark.django_db
def test_group_media_policy_is_enforced(group_thread, member_a):
    group_thread.members_can_send_media = False
    group_thread.save(update_fields=["members_can_send_media"])
    response = authed(member_a).post(
        f"/api/conversations/{group_thread.conversation_id}/messages/",
        {"kind": "image", "client_id": client_id(), "file": jpeg_bytes()},
        format="multipart",
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_conversation_media_listing(private_thread, member_a):
    upload(authed(member_a), private_thread)
    results = authed(member_a).get(f"/api/conversations/{private_thread.id}/media/").json()["data"]["results"]
    assert len(results) == 1 and results[0]["media"]["mime_type"] == "image/jpeg"


@pytest.mark.django_db
def test_media_worker_retries_then_marks_failed(private_thread, member_a, monkeypatch, settings):
    settings.MEDIA_PROCESS_INLINE = False
    response = upload(authed(member_a), private_thread)
    assert response.status_code == 201
    attachment = Attachment.objects.get()
    assert attachment.processing_state == "PENDING"

    from apps.media import processing

    monkeypatch.setattr(processing, "_image_derivatives", lambda a: (_ for _ in ()).throw(RuntimeError("BOOM")))
    for _ in range(3):
        attachment.refresh_from_db()
        attachment.processing_state = "PENDING"
        attachment.save(update_fields=["processing_state"])
        processing.process_attachment(attachment)

    attachment.refresh_from_db()
    assert attachment.processing_state == "FAILED"
    assert attachment.processing_error == "BOOM"
    # The original is untouched and still downloadable.
    assert default_storage.exists(attachment.storage_key)
    assert authed(member_a).get(f"/api/media/{attachment.id}/").status_code == 200


@pytest.mark.django_db
def test_chunked_upload_session(private_thread, member_a):
    from django.core.files.uploadedfile import SimpleUploadedFile

    client = authed(member_a)
    payload = jpeg_bytes(size=(64, 64)).getvalue()
    key = client_id()

    started = client.post(
        "/api/uploads/",
        {
            "kind": "IMAGE",
            "client_id": key,
            "size": len(payload),
            "name": "big.jpg",
            "conversation": str(private_thread.id),
        },
        format="json",
    )
    assert started.status_code == 201, started.data
    session_id = started.json()["data"]["id"]

    # Resuming with the same client_id must not create a second session.
    assert client.post(
        "/api/uploads/",
        {"kind": "IMAGE", "client_id": key, "size": len(payload), "conversation": str(private_thread.id)},
        format="json",
    ).json()["data"]["id"] == session_id

    half = len(payload) // 2
    for index, blob in enumerate((payload[:half], payload[half:])):
        response = client.put(
            f"/api/uploads/{session_id}/part/?part={index}",
            {"chunk": SimpleUploadedFile("part", blob)},
            format="multipart",
        )
        assert response.status_code == 200, response.data

    # Out-of-order parts are refused.
    assert client.put(
        f"/api/uploads/{session_id}/part/?part=9",
        {"chunk": SimpleUploadedFile("part", b"x")},
        format="multipart",
    ).status_code == 400

    completed = client.post(f"/api/uploads/{session_id}/complete/", {"caption": "big"}, format="json")
    assert completed.status_code in (200, 201), completed.data
    assert Attachment.objects.count() == 1


@pytest.mark.django_db
def test_validate_upload_rejects_unknown_kind():
    from rest_framework.exceptions import ValidationError

    with pytest.raises(ValidationError):
        validate_upload(jpeg_bytes(), kind="DOCUMENT")
