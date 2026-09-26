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

from apps.conversations.models import Conversation
from apps.conversations.services import can_access
from apps.core.throttles import UploadThrottle

from .models import UploadSession
from .validators import KIND_TYPES, safe_display_name, storage_key

DEFAULT_PART_SIZE = 5 * 1024 * 1024
SESSION_TTL_HOURS = 6


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

    session = UploadSession.objects.create(
        user=request.user,
        conversation=conversation,
        kind=kind,
        client_id=client_id,
        declared_name=safe_display_name(request.data.get("name", "")),
        declared_size=size,
        part_size=DEFAULT_PART_SIZE,
        staging_key=storage_key("media/staging", ".part"),
        expires_at=timezone.now() + timezone.timedelta(hours=SESSION_TTL_HOURS),
    )
    default_storage.save(session.staging_key, ContentFile(b""))
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
    if session.received_bytes + chunk.size > session.declared_size:
        raise ValidationError("The upload exceeds its declared size.")

    _append(session.staging_key, chunk)

    UploadSession.objects.filter(pk=session.pk).update(
        next_part=session.next_part + 1,
        received_bytes=session.received_bytes + chunk.size,
        updated_at=timezone.now(),
    )
    session.refresh_from_db()
    return Response({"success": True, "message": "Chunk stored", "data": _serialize(session)})


def _append(key: str, chunk) -> None:
    """Append a chunk to a stored object in bounded blocks."""
    import io

    buffer = io.BytesIO()
    try:
        with default_storage.open(key, "rb") as existing:
            while True:
                block = existing.read(settings.MEDIA_STREAM_CHUNK_SIZE)
                if not block:
                    break
                buffer.write(block)
    except FileNotFoundError:
        pass
    for block in chunk.chunks(settings.MEDIA_STREAM_CHUNK_SIZE):
        buffer.write(block)
    buffer.seek(0)
    default_storage.delete(key)
    default_storage.save(key, ContentFile(buffer.read()))


@api_view(["POST"])
def complete(request, session_id):
    session = _session(request, session_id)
    if session.state == UploadSession.State.COMPLETED:
        raise ValidationError("This upload has already been completed.")
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
        UploadSession.objects.filter(pk=session.pk).update(state=UploadSession.State.COMPLETED)
    default_storage.delete(session.staging_key)
    return response
