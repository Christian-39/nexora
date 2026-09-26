"""
NEXORA — media persistence + authorization services.

Authorization rule (enforced here, never in the frontend):
an attachment is readable only by an active participant of the conversation
that owns its message. Guessing an attachment UUID therefore yields 404.
"""

from __future__ import annotations

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import transaction

from .validators import storage_key


def store_upload(fileobj, validated, *, prefix: str = "media/originals") -> str:
    """Stream an upload to private storage under an unguessable key."""
    key = storage_key(prefix, validated.extension)
    return default_storage.save(key, fileobj)


@transaction.atomic
def create_attachment(*, message, fileobj, validated, poster=None):
    """Persist an ``Attachment`` row for a freshly stored upload."""
    from apps.conversations.models import Attachment

    saved_key = store_upload(fileobj, validated)
    thumbnail_key = ""
    if poster is not None:
        from .validators import signature_mime

        head = poster.read(512)
        poster.seek(0)
        if signature_mime(head) in ("image/jpeg", "image/png", "image/webp"):
            thumbnail_key = default_storage.save(storage_key("media/posters", ".jpg"), poster)

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
    """Issue a short-lived signed URL *after* authorization has been granted.

    With S3-compatible storage this is a pre-signed object URL. With local
    filesystem storage there is no signature mechanism, so the caller keeps
    using the authenticated streaming endpoint instead.
    """
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
