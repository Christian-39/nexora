"""Media upload validation, storage safety, authorization and processing."""

from __future__ import annotations

import io

import pytest
from django.core.files.storage import default_storage

from apps.conversations.models import Attachment
from apps.core.hashes import sha256_hex
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


def test_webm_video_track_marker_after_512_bytes_is_not_misclassified_as_audio():
    header = bytes([0x1A, 0x45, 0xDF, 0xA3]) + b"metadata" * 80 + b"V_VP9"
    assert signature_mime(header) == "video/webm"


@pytest.mark.django_db
def test_browser_audio_mp4_is_accepted_only_when_probe_confirms_audio_only(monkeypatch):
    from django.core.files.uploadedfile import SimpleUploadedFile
    from rest_framework.exceptions import ValidationError

    from apps.media import validators

    mp4 = bytes([0, 0, 0, 24]) + b"ftypmp42" + bytes([0, 0, 0, 0]) + b"mp42isom" + bytes(32)
    audio = SimpleUploadedFile("voice.m4a", mp4, content_type="audio/mp4;codecs=mp4a.40.2")
    monkeypatch.setattr(
        validators,
        "_probe_media_info",
        lambda _file: {"has_audio": True, "has_video": False, "duration_ms": 1200},
    )
    validated = validate_upload(audio, kind="VOICE")
    assert validated.mime_type == "audio/mp4"
    assert validated.extension == ".m4a"
    assert validated.duration_ms == 1200

    disguised_video = SimpleUploadedFile("voice.m4a", mp4, content_type="audio/mp4")
    monkeypatch.setattr(
        validators,
        "_probe_media_info",
        lambda _file: {"has_audio": True, "has_video": True, "duration_ms": 1200},
    )
    with pytest.raises(ValidationError):
        validate_upload(disguised_video, kind="VOICE")


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
    assert attachment.storage_key_hash == sha256_hex(attachment.storage_key)
    assert len(attachment.storage_key_hash) == 64
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
def test_chunked_upload_session(private_thread, member_a, monkeypatch):
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.core.management import call_command

    from apps.media.models import UploadSession

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

    session = UploadSession.objects.get(pk=session_id)
    first_staging_key = session.staging_key
    real_delete = default_storage.delete
    failed_old_delete = {"value": False}

    def fail_old_part_once(path):
        if path == first_staging_key and not failed_old_delete["value"]:
            failed_old_delete["value"] = True
            raise OSError("temporary cleanup failure for a superseded part")
        return real_delete(path)

    monkeypatch.setattr(default_storage, "delete", fail_old_part_once)

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
        if index == 0:
            # The response may be lost after the part commit. Retrying the same
            # index acknowledges it without appending its bytes a second time.
            duplicate = client.put(
                f"/api/uploads/{session_id}/part/?part=0",
                {"chunk": SimpleUploadedFile("part", blob)},
                format="multipart",
            )
            assert duplicate.status_code == 200, duplicate.data
            assert duplicate.json()["data"]["next_part"] == 1
            assert duplicate.json()["data"]["received_bytes"] == half
            assert default_storage.exists(first_staging_key), "failed superseded-part cleanup must remain retryable"

    # Out-of-order parts are refused.
    assert client.put(
        f"/api/uploads/{session_id}/part/?part=9",
        {"chunk": SimpleUploadedFile("part", b"x")},
        format="multipart",
    ).status_code == 400

    session = UploadSession.objects.get(pk=session_id)
    current_staging_key = session.staging_key
    original_delete = real_delete

    def temporary_delete_failure(_key):
        raise OSError("temporary object-storage outage")

    monkeypatch.setattr(default_storage, "delete", temporary_delete_failure)
    completed = client.post(f"/api/uploads/{session_id}/complete/", {"caption": "big"}, format="json")
    assert completed.status_code in (200, 201), completed.data
    assert Attachment.objects.count() == 1
    attachment_key = Attachment.objects.get().storage_key
    session.refresh_from_db()
    assert session.state == UploadSession.State.COMPLETED
    assert session.staging_key
    assert default_storage.exists(attachment_key)

    # If the original completion response is lost, the same session returns
    # the already-created message rather than a misleading 400 or a duplicate.
    retried = client.post(f"/api/uploads/{session_id}/complete/", {"caption": "big"}, format="json")
    assert retried.status_code == 200, retried.data
    assert retried.json()["data"]["client_id"] == key
    assert Attachment.objects.count() == 1

    monkeypatch.setattr(default_storage, "delete", original_delete)
    call_command("finalize_uploads", "--once")
    session.refresh_from_db()
    assert session.staging_key == ""
    assert not default_storage.exists(first_staging_key), "finalizer retries cleanup for a superseded part"
    assert not default_storage.exists(current_staging_key)
    assert default_storage.exists(attachment_key), "staging cleanup must not delete the committed attachment"


@pytest.mark.django_db
def test_chunked_upload_does_not_acknowledge_a_missing_staging_object(private_thread, member_a):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from apps.media.models import UploadSession

    client = authed(member_a)
    payload = jpeg_bytes(size=(64, 64)).getvalue()
    key = client_id()
    started = client.post(
        "/api/uploads/",
        {
            "kind": "IMAGE",
            "client_id": key,
            "size": len(payload),
            "name": "image.jpg",
            "conversation": str(private_thread.id),
        },
        format="json",
    )
    assert started.status_code == 201, started.data
    session = UploadSession.objects.get(pk=started.json()["data"]["id"])
    default_storage.delete(session.staging_key)

    response = client.put(
        f"/api/uploads/{session.id}/part/?part=0",
        {"chunk": SimpleUploadedFile("part", payload)},
        format="multipart",
    )

    assert response.status_code == 400
    session.refresh_from_db()
    assert session.next_part == 0
    assert session.received_bytes == 0


@pytest.mark.django_db
def test_upload_finalizer_expires_sessions_and_retries_staging_cleanup(member_a, private_thread, settings, monkeypatch):
    from django.core.files.base import ContentFile
    from django.core.management import call_command
    from django.utils import timezone

    from apps.media.models import UploadSession
    from apps.media.validators import storage_key

    settings.MEDIA_PROCESS_INLINE = False
    expired_key = storage_key("media/staging", ".part")
    completed_key = storage_key("media/staging", ".part")
    default_storage.save(expired_key, ContentFile(b"partial"))
    default_storage.save(completed_key, ContentFile(b"leftover"))
    expired = UploadSession.objects.create(
        user=member_a,
        conversation=private_thread,
        kind="IMAGE",
        client_id=client_id(),
        declared_name="partial.jpg",
        declared_size=100,
        part_size=5 * 1024 * 1024,
        received_bytes=7,
        staging_key=expired_key,
        expires_at=timezone.now() - timezone.timedelta(seconds=1),
    )
    completed = UploadSession.objects.create(
        user=member_a,
        conversation=private_thread,
        kind="IMAGE",
        client_id=client_id(),
        declared_name="complete.jpg",
        declared_size=8,
        part_size=5 * 1024 * 1024,
        received_bytes=8,
        staging_key=completed_key,
        state=UploadSession.State.COMPLETED,
        expires_at=timezone.now() + timezone.timedelta(hours=1),
    )

    real_delete = default_storage.delete
    failed_once = {"value": False}

    def flaky_delete(key):
        if key == expired_key and not failed_once["value"]:
            failed_once["value"] = True
            raise OSError("temporary storage cleanup failure")
        return real_delete(key)

    monkeypatch.setattr(default_storage, "delete", flaky_delete)
    call_command("finalize_uploads", "--once")

    expired.refresh_from_db()
    completed.refresh_from_db()
    assert expired.state == UploadSession.State.ABORTED
    assert expired.staging_key == expired_key
    assert default_storage.exists(expired_key), "failed delete remains linked for a later retry"
    assert completed.state == UploadSession.State.COMPLETED
    assert completed.staging_key == ""
    assert not default_storage.exists(completed_key)

    monkeypatch.setattr(default_storage, "delete", real_delete)
    call_command("finalize_uploads", "--once")
    expired.refresh_from_db()
    assert expired.staging_key == ""
    assert not default_storage.exists(expired_key)


@pytest.mark.django_db
def test_validate_upload_rejects_unknown_kind():
    from rest_framework.exceptions import ValidationError

    with pytest.raises(ValidationError):
        validate_upload(jpeg_bytes(), kind="DOCUMENT")
