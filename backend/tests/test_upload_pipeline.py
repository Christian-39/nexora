"""Upload pipeline: rate-limit enforcement and transaction/storage boundaries.

These cover two defects found during the production audit of the §9 upload
failures:

1. ``create_media_message`` / ``create_text_message`` called
   ``Throttle().allow_request(request, None)`` and **discarded the boolean**.
   DRF only converts a ``False`` return into HTTP 429 inside
   ``APIView.check_throttles``; calling the throttle by hand and ignoring the
   result means the limit is never enforced.

2. ``apps.media.services.create_attachment`` performed the object-storage PUT
   *inside* an open MySQL transaction, so a slow S3 request held a pooled DB
   connection (and its locks) open for the whole transfer.
"""

from __future__ import annotations

import io

import pytest
from django.db import connection, transaction
from rest_framework.throttling import SimpleRateThrottle

from tests.conftest import authed, client_id, jpeg_bytes


@pytest.fixture
def throttle_rate(monkeypatch):
    """Override a throttle rate for real.

    ``SimpleRateThrottle.THROTTLE_RATES`` is bound to the DRF settings dict at
    *import* time, so overriding ``settings.REST_FRAMEWORK`` in a test has no
    effect on it. The rates dict itself has to be patched.
    """

    def apply(scope: str, rate: str):
        monkeypatch.setitem(SimpleRateThrottle.THROTTLE_RATES, scope, rate)

    return apply


@pytest.fixture(autouse=True)
def _local_storage(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.MEDIA_PROCESS_INLINE = False  # production topology: worker does derivatives
    yield


def _upload(client, conversation, **extra):
    payload = {"kind": "image", "client_id": client_id(), "file": jpeg_bytes(), **extra}
    return client.post(f"/api/conversations/{conversation.id}/messages/", payload, format="multipart")


# ---------------------------------------------------------------------------
# 1. Throttle enforcement
# ---------------------------------------------------------------------------


def test_upload_rate_limit_is_actually_enforced(throttle_rate, admin, private_thread):
    """Exceeding the upload rate must yield HTTP 429, not sail through."""
    throttle_rate("uploads", "3/min")
    client = authed(admin)

    statuses = [_upload(client, private_thread).status_code for _ in range(5)]

    assert 429 in statuses, (
        f"upload throttle never fired; got {statuses}. The view calls "
        "UploadThrottle().allow_request(...) but ignores the result."
    )
    assert statuses[:3] == [201, 201, 201], statuses


def test_message_rate_limit_is_actually_enforced(throttle_rate, admin, private_thread):
    """Same defect on the text-message path."""
    throttle_rate("messages", "3/min")
    client = authed(admin)

    statuses = []
    for _ in range(5):
        response = client.post(
            f"/api/conversations/{private_thread.id}/messages/",
            {"text": "hello", "client_id": client_id()},
            format="json",
        )
        statuses.append(response.status_code)

    assert 429 in statuses, f"message throttle never fired; got {statuses}"


def test_throttled_upload_does_not_create_a_message(throttle_rate, admin, private_thread):
    """A 429 must be refused *before* the file is stored."""
    from apps.conversations.models import Message

    throttle_rate("uploads", "2/min")
    client = authed(admin)
    for _ in range(2):
        _upload(client, private_thread)
    before = Message.objects.count()

    response = _upload(client, private_thread)

    assert response.status_code == 429
    assert Message.objects.count() == before, "a throttled upload still created a message"


# ---------------------------------------------------------------------------
# 2. Storage must not run inside the DB transaction
# ---------------------------------------------------------------------------


def test_object_storage_put_happens_outside_the_db_transaction(admin, private_thread, monkeypatch):
    """The S3 PUT must not hold a MySQL transaction open for its duration.

    Holding one open across a multi-second upload pins a pooled connection and
    its row locks, which is how two concurrent uploads could stall the whole
    web service (WEB_CONCURRENCY=2).
    """
    from apps.media import services as media_services

    # pytest-django already wraps each test in a transaction, so
    # ``in_atomic_block`` is unconditionally True here. What matters is
    # whether the *view* opened a further atomic block around the PUT, which
    # shows up as an extra savepoint.
    baseline = len(connection.savepoint_ids)
    seen: dict[str, int] = {}

    real_save = media_services.default_storage.save

    def spy(key, content, *args, **kwargs):
        seen.setdefault("depth", len(connection.savepoint_ids))
        return real_save(key, content, *args, **kwargs)

    monkeypatch.setattr(media_services.default_storage, "save", spy)

    response = _upload(authed(admin), private_thread)

    assert response.status_code == 201, response.data
    assert seen, "storage save was never called"
    assert seen["depth"] == baseline, (
        f"object storage PUT ran inside a transaction opened by the view "
        f"(savepoint depth {seen['depth']} vs baseline {baseline}); a slow "
        "upload will hold a pooled MySQL connection and its locks for the "
        "whole transfer"
    )


def test_upload_is_idempotent_per_client_id(admin, private_thread):
    """A retried upload must reuse the first message, never duplicate it."""
    from apps.conversations.models import Attachment, Message

    client = authed(admin)
    cid = client_id()

    first = client.post(
        f"/api/conversations/{private_thread.id}/messages/",
        {"kind": "image", "client_id": cid, "file": jpeg_bytes()},
        format="multipart",
    )
    second = client.post(
        f"/api/conversations/{private_thread.id}/messages/",
        {"kind": "image", "client_id": cid, "file": jpeg_bytes()},
        format="multipart",
    )

    assert first.status_code == 201
    assert second.status_code == 200, "retry should be recognised as already-sent"
    assert Message.objects.filter(sender=admin, client_id=cid).count() == 1
    assert Attachment.objects.filter(message__client_id=cid).count() == 1, "retry duplicated the attachment"


def test_failed_storage_leaves_no_orphaned_message(admin, private_thread, monkeypatch):
    """If the PUT fails the message must not survive (no empty bubbles)."""
    from apps.conversations.models import Message
    from apps.media import services as media_services

    def boom(*args, **kwargs):
        raise OSError("simulated object-storage outage")

    monkeypatch.setattr(media_services.default_storage, "save", boom)
    before = Message.objects.count()

    client = authed(admin)
    try:
        response = _upload(client, private_thread)
        status = response.status_code
    except OSError:
        status = 500

    assert status >= 400, "a storage outage reported success to the client"
    assert Message.objects.count() == before, (
        "storage failed but the message row persisted — the user sees an "
        "attachment-less bubble that can never be repaired"
    )
