"""
NEXORA — media persistence + authorization services.

Authorization rule (enforced here, never in the frontend):
an attachment is readable only by an active participant of the conversation
that owns its message. Guessing an attachment UUID therefore yields 404.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import transaction

from .validators import storage_key


logger = logging.getLogger("nexora.upload")
_SAFE_PROVIDER_TOKEN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def storage_failure_context(exc) -> dict:
    """Extract only safe provider status/code/request-id fields for upload logs.

    Storage exception messages and response bodies can contain object keys,
    endpoints, request data, or credentials; they are deliberately never
    copied into logs. The returned mapping is suitable for ``LogRecord.extra``.
    """
    response = getattr(exc, "response", None)
    metadata = response.get("ResponseMetadata", {}) if isinstance(response, dict) else {}
    error = response.get("Error", {}) if isinstance(response, dict) else {}
    result = {}

    status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
    if status is None and response is not None and not isinstance(response, dict):
        status = getattr(response, "status_code", None)
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = None
    if status is not None and 100 <= status <= 599:
        result["storage_http_status"] = status

    code = error.get("Code") if isinstance(error, dict) else None
    if code is None:
        code = getattr(exc, "code", None)
    if code is not None and _SAFE_PROVIDER_TOKEN.fullmatch(str(code)):
        result["storage_error_code"] = str(code)

    provider_request_id = metadata.get("RequestId") if isinstance(metadata, dict) else None
    if provider_request_id and _SAFE_PROVIDER_TOKEN.fullmatch(str(provider_request_id)):
        result["storage_request_id"] = str(provider_request_id)
    return result


def store_upload(fileobj, validated, *, prefix: str = "media/originals") -> str:
    """Stream an upload to private storage under an unguessable key."""
    key = storage_key(prefix, validated.extension)
    try:
        return default_storage.save(key, fileobj)
    except Exception:
        # A backend can fail after writing some bytes; the candidate name is
        # known even when Storage.save never returns its canonical key.
        try:
            default_storage.delete(key)
        except Exception as cleanup_exc:  # noqa: BLE001 - preserve original error
            logger.warning(
                "failed upload object cleanup deferred storage_op=delete exception=%s",
                cleanup_exc.__class__.__name__,
            )
        raise


@dataclass
class StagedUpload:
    """Objects already written to storage, not yet referenced by any row.

    Staging is deliberately performed *outside* the database transaction: an
    object-storage PUT can take tens of seconds for a large file, and holding
    a pooled MySQL connection (plus its row locks) open for that long is what
    let two concurrent uploads stall the whole web service.

    The trade-off is that a failure after staging could leave an unreferenced
    object, so the caller MUST invoke :meth:`discard` on any error path. The
    ``finalize_uploads`` worker only retries resumable-session staging cleanup;
    it does not scan arbitrary private-storage objects.
    """

    storage_key: str
    thumbnail_key: str = ""

    def discard(self) -> None:
        """Best-effort cleanup of staged objects after a failed commit."""
        for key in (self.storage_key, self.thumbnail_key):
            if not key:
                continue
            try:
                default_storage.delete(key)
            except Exception as exc:  # noqa: BLE001 - cleanup must never mask the original error
                logger.warning(
                    "could not remove staged object after a failed upload storage_op=delete exception=%s",
                    exc.__class__.__name__,
                )


def stage_upload(fileobj, validated, *, poster=None) -> StagedUpload:
    """Write the original (and optional poster) to storage. No DB work."""
    staged = StagedUpload(storage_key=store_upload(fileobj, validated))
    try:
        if poster is not None:
            from .validators import signature_mime

            head = poster.read(512)
            poster.seek(0)
            if signature_mime(head) in ("image/jpeg", "image/png", "image/webp"):
                staged.thumbnail_key = default_storage.save(storage_key("media/posters", ".jpg"), poster)
        return staged
    except Exception:
        # The original was already stored but no caller received the handle to
        # clean it up. Remove it before re-raising the staging failure.
        staged.discard()
        raise


@transaction.atomic
def create_attachment(*, message, validated, staged: StagedUpload):
    """Persist an ``Attachment`` row for an already-staged upload."""
    from apps.conversations.models import Attachment

    saved_key = staged.storage_key
    thumbnail_key = staged.thumbnail_key

    attachment = Attachment.objects.create(
        message=message,
        storage_key=saved_key,
        original_name=validated.display_name,
        mime_type=validated.mime_type,
        size=validated.size,
        duration_ms=validated.duration_ms,
        width=validated.width,
        height=validated.height,
        thumbnail_key=thumbnail_key,
        processing_state="PENDING",
    )
    _schedule(attachment)
    return attachment


def _schedule(attachment) -> None:
    """Hand the attachment to the processing pipeline.

    Normally the row simply stays PENDING and ``manage.py media_worker`` picks
    it up, which keeps request latency independent of file size. Deployments
    without a worker (and the test-suite) can set ``MEDIA_PROCESS_INLINE`` to
    generate derivatives synchronously instead.
    """
    if not settings.MEDIA_PROCESS_INLINE:
        return
    from .processing import process_attachment

    if transaction.get_connection().in_atomic_block:
        transaction.on_commit(lambda: process_attachment(attachment))
    else:  # pragma: no cover - defensive
        process_attachment(attachment)


def can_read(user, attachment) -> bool:
    """True when ``user`` is an active participant of the owning conversation."""
    if not getattr(user, "is_authenticated", False):
        return False
    return attachment.message.conversation.participants.filter(
        user=user, is_active=True, conversation__is_active=True
    ).exists()


def variant_key(attachment, variant: str) -> str | None:
    if variant == "thumbnail":
        return attachment.thumbnail_key or None
    if variant == "optimized":
        return attachment.optimized_key or None
    return attachment.storage_key


def signed_url(attachment, variant: str = "original") -> str | None:
    """Return a signed URL only when no production relay privacy boundary exists.

    Production media must stay on the authenticated `/api/media/` route so a
    private object-store endpoint or signed object URL is never exposed to the
    browser. The relay streams this response (including Range requests) over
    the private backend hop. Local/non-relay deployments retain the existing
    short-lived S3 URL behaviour.
    """
    if settings.SECURITY_RELAY_REQUIRED:
        return None
    key = variant_key(attachment, variant)
    if not key:
        return None
    try:
        url = default_storage.url(key)
    except (NotImplementedError, ValueError):
        return None
    return url if str(url).lower().startswith(("http://", "https://")) else None


def delete_attachment_files(attachment) -> None:
    for key in filter(None, [attachment.storage_key, attachment.thumbnail_key, attachment.optimized_key]):
        try:
            default_storage.delete(key)
        except (OSError, NotImplementedError):  # pragma: no cover - storage best effort
            pass
