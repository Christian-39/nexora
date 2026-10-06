"""
NEXORA — resumable (chunked) uploads for very large attachments.

    POST   /api/uploads/                     initiate  -> session
    GET    /api/uploads/{id}/                progress  (resume point)
    PUT    /api/uploads/{id}/part/           append one ordered chunk
    POST   /api/uploads/{id}/complete/       validate + create the message

Chunks are appended to a staging object in bounded blocks, so neither the
request process nor the worker ever holds a whole file in memory. Ownership is
fixed at initiation: only the creator may append to or complete a session, and
only into a conversation they are allowed to post in.
"""

from __future__ import annotations

import logging
import uuid

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.http import Http404
from django.utils import timezone
from rest_framework.decorators import api_view, parser_classes, throttle_classes
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FileUploadParser, FormParser, MultiPartParser
from rest_framework.response import Response

from apps.conversations.models import Conversation, Message
from apps.conversations.serializers import MessageSerializer
from apps.conversations.services import can_access
from apps.core.observability import sanitize
from apps.core.throttles import UploadThrottle

from .models import UploadSession

logger = logging.getLogger("nexora.upload")
from .validators import KIND_TYPES, safe_display_name

DEFAULT_PART_SIZE = 5 * 1024 * 1024
SESSION_TTL_HOURS = 6


def _staging_prefix(session_id) -> str:
    """A private, session-specific prefix makes orphaned parts discoverable."""
    return f"media/staging/{session_id}"


def _session(request, session_id) -> UploadSession:
    session = UploadSession.objects.select_related("conversation").filter(id=session_id, user=request.user).first()
    if not session:
        raise Http404
    return session


def _serialize(session: UploadSession) -> dict:
    return {
        "id": str(session.id),
        "state": session.state,
        "part_size": session.part_size,
        "next_part": session.next_part,
        "received_bytes": session.received_bytes,
        "declared_size": session.declared_size,
        "expires_at": session.expires_at.isoformat(),
    }


@api_view(["POST"])
@throttle_classes([UploadThrottle])
def initiate(request):
    kind = str(request.data.get("kind", "")).upper()
    if kind not in KIND_TYPES:
        raise ValidationError("Unsupported attachment type.")

    client_id = str(request.data.get("client_id", "")).strip()
    if not 1 <= len(client_id) <= 64:
        raise ValidationError("A client_id between 1 and 64 characters is required.")

    try:
        size = int(request.data.get("size", 0))
    except (TypeError, ValueError) as exc:
        raise ValidationError("A numeric size is required.") from exc
    if size <= 0 or size > settings.MAX_UPLOAD_SIZE:
        raise ValidationError("The declared size is outside the permitted range.")

    conversation = Conversation.objects.filter(id=request.data.get("conversation")).first()
    if not conversation:
        raise Http404
    if not can_access(request.user, conversation):
        raise PermissionDenied()

    existing = UploadSession.objects.filter(user=request.user, client_id=client_id).first()
    if existing:
        # Idempotent initiation: a retried request resumes the same session.
        return Response({"success": True, "message": "Upload session resumed", "data": _serialize(existing)})

    session_id = uuid.uuid4()
    session = UploadSession.objects.create(
        id=session_id,
        user=request.user,
        conversation=conversation,
        kind=kind,
        client_id=client_id,
        declared_name=safe_display_name(request.data.get("name", "")),
        declared_size=size,
        part_size=DEFAULT_PART_SIZE,
        staging_key=f"{_staging_prefix(session_id)}/{uuid.uuid4().hex}.part",
        expires_at=timezone.now() + timezone.timedelta(hours=SESSION_TTL_HOURS),
    )
    requested_key = session.staging_key
    saved_key = requested_key
    try:
        saved_key = default_storage.save(requested_key, ContentFile(b""))
        if saved_key != requested_key:
            updated = UploadSession.objects.filter(pk=session.pk, staging_key=requested_key).update(
                staging_key=saved_key
            )
            if not updated:
                raise RuntimeError("Upload session staging key changed during initialization.")
            session.staging_key = saved_key
    except Exception:
        # If Storage.save chose an alternate name, only that returned object is
        # ours; the requested name may have belonged to an existing object.
        _discard_staging(saved_key, session_id=session.id, user_id=request.user.id)
        try:
            session.delete()
        except Exception as cleanup_exc:  # noqa: BLE001 - preserve the storage failure
            logger.warning(
                "failed upload-session row cleanup session=%s user=%s exception=%s message=%s",
                session.id,
                request.user.id,
                cleanup_exc.__class__.__name__,
                sanitize(cleanup_exc, limit=300),
                extra={"user_id": str(request.user.id), "user_role": str(request.user.role)},
            )
        raise
    return Response({"success": True, "message": "Upload session created", "data": _serialize(session)}, status=201)


@api_view(["GET"])
def detail(request, session_id):
    return Response({"success": True, "message": "Upload session", "data": _serialize(_session(request, session_id))})


@api_view(["PUT", "POST"])
@parser_classes([MultiPartParser, FormParser, FileUploadParser])
@throttle_classes([UploadThrottle])
def sign_part(request, session_id):
    """Append one ordered chunk to the staging object."""
    session = _session(request, session_id)
    if not session.is_open:
        raise ValidationError("This upload session is no longer open.")

    try:
        index = int(request.query_params.get("part", request.data.get("part", -1)))
    except (TypeError, ValueError) as exc:
        raise ValidationError("A numeric part index is required.") from exc

    chunk = request.FILES.get("chunk") or request.FILES.get("file")
    if chunk is None:
        raise ValidationError("A chunk is required.")

    if index == session.next_part - 1:
        # Duplicate retry of the last accepted chunk — acknowledge, do not append.
        return Response({"success": True, "message": "Chunk already stored", "data": _serialize(session)})
    if index != session.next_part:
        raise ValidationError({"part": f"Expected part {session.next_part}."})
    if chunk.size <= 0 or chunk.size > session.part_size:
        raise ValidationError("Each upload part must be non-empty and within the session part size.")
    if session.received_bytes + chunk.size > session.declared_size:
        raise ValidationError("The upload exceeds its declared size.")

    previous_key = session.staging_key
    replacement_key = _append(
        previous_key,
        chunk,
        session_id=session.id,
        user_id=request.user.id,
    )
    try:
        updated = UploadSession.objects.filter(
            pk=session.pk,
            state=UploadSession.State.OPEN,
            next_part=session.next_part,
            received_bytes=session.received_bytes,
            staging_key=previous_key,
        ).update(
            staging_key=replacement_key,
            next_part=session.next_part + 1,
            received_bytes=session.received_bytes + chunk.size,
            updated_at=timezone.now(),
        )
    except Exception:
        _discard_staging(replacement_key, session_id=session.id, user_id=request.user.id)
        raise

    if not updated:
        _discard_staging(replacement_key, session_id=session.id, user_id=request.user.id)
        session.refresh_from_db()
        if index == session.next_part - 1:
            return Response({"success": True, "message": "Chunk already stored", "data": _serialize(session)})
        raise ValidationError({"part": f"Expected part {session.next_part}."})

    try:
        default_storage.delete(previous_key)
    except Exception as exc:  # noqa: BLE001 - the new key is committed; report the orphan
        logger.warning(
            "old upload staging object cleanup failed session=%s user=%s storage_op=delete exception=%s message=%s",
            session.id,
            request.user.id,
            exc.__class__.__name__,
            sanitize(exc, limit=300),
            extra={"user_id": str(request.user.id), "user_role": str(request.user.role)},
        )
    session.refresh_from_db()
    return Response({"success": True, "message": "Chunk stored", "data": _serialize(session)})


def _append(key: str, chunk, *, session_id, user_id) -> str:
    """Append in bounded memory, writing a replacement object before swapping.

    The storage abstraction has no generic append operation, so each part still
    rewrites the accumulated object. A spooled file bounds RAM, while the new
    object key keeps the previous complete staging object intact if the write
    or database compare-and-swap fails.
    """
    from tempfile import SpooledTemporaryFile

    from django.core.files import File

    replacement_key = f"{_staging_prefix(session_id)}/{uuid.uuid4().hex}.part"
    try:
        with SpooledTemporaryFile(max_size=max(1, settings.FILE_UPLOAD_MAX_MEMORY_SIZE), mode="w+b") as buffer:
            try:
                with default_storage.open(key, "rb") as existing:
                    while True:
                        block = existing.read(settings.MEDIA_STREAM_CHUNK_SIZE)
                        if not block:
                            break
                        buffer.write(block)
            except FileNotFoundError as exc:
                raise ValidationError("The upload staging data is unavailable. Restart this upload.") from exc
            for block in chunk.chunks(settings.MEDIA_STREAM_CHUNK_SIZE):
                buffer.write(block)
            buffer.seek(0)
            replacement_key = default_storage.save(replacement_key, File(buffer, name=replacement_key))
        return replacement_key
    except Exception:
        _discard_staging(replacement_key, session_id=session_id, user_id=user_id)
        raise


def _discard_staging(key: str, *, session_id, user_id) -> None:
    try:
        default_storage.delete(key)
    except Exception as exc:  # noqa: BLE001 - cleanup is best effort
        logger.warning(
            "replacement upload staging cleanup failed session=%s user=%s storage_op=delete exception=%s message=%s",
            session_id,
            user_id,
            exc.__class__.__name__,
            sanitize(exc, limit=300),
            extra={"user_id": str(user_id)},
        )


@api_view(["POST"])
def complete(request, session_id):
    session = _session(request, session_id)
    if session.state == UploadSession.State.COMPLETED:
        # The first response can be lost after the DB commit. Return the
        # original message on retry rather than turning a successful upload
        # into a misleading 400 (or creating a duplicate).
        existing = Message.objects.filter(sender=request.user, client_id=session.client_id).first()
        if existing and existing.conversation_id == session.conversation_id:
            from apps.conversations.views import _reload

            return Response(
                {
                    "success": True,
                    "message": "Upload already completed",
                    "data": MessageSerializer(_reload(existing), context={"request": request}).data,
                },
                status=200,
            )
        raise ValidationError("This upload session has already completed.")
    if not session.is_open:
        raise ValidationError("This upload session is no longer open.")
    if session.received_bytes != session.declared_size:
        raise ValidationError("The upload is incomplete.")
    if not can_access(request.user, session.conversation):
        raise PermissionDenied()

    from apps.conversations.views import create_message_from_stored_upload

    with default_storage.open(session.staging_key, "rb") as stored:
        response = create_message_from_stored_upload(
            request,
            conversation=session.conversation,
            fileobj=stored,
            kind=session.kind,
            client_id=session.client_id,
            declared_name=session.declared_name,
            caption=str(request.data.get("caption", "")),
            reply_to_id=request.data.get("reply_to"),
        )

    with transaction.atomic():
        UploadSession.objects.filter(pk=session.pk, state=UploadSession.State.OPEN).update(
            state=UploadSession.State.COMPLETED
        )
    # The message/attachment commit is the authoritative success. Staging
    # cleanup is best-effort and retried by finalize_uploads; a storage-delete
    # outage must not turn a committed upload into an HTTP failure.
    try:
        default_storage.delete(session.staging_key)
        UploadSession.objects.filter(pk=session.pk, staging_key=session.staging_key).update(staging_key="")
    except Exception as exc:  # noqa: BLE001 - cleanup is retried asynchronously
        logger.warning(
            "upload staging cleanup deferred session=%s user=%s storage_op=delete exception=%s message=%s",
            session.id,
            request.user.id,
            exc.__class__.__name__,
            sanitize(exc, limit=300),
            extra={"user_id": str(request.user.id), "user_role": str(request.user.role)},
        )
    return response
