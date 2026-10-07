"""
NEXORA — media delivery.

``GET /api/media/{uuid}/``      authenticated streaming download (range-aware)
``GET /api/media/{uuid}/url/``  authorize, then return the configured media URL

``/api/media/{uuid}/url/`` returns a short-lived signed object-storage URL
that the browser uses directly. The signature expires after
``SIGNED_URL_TTL_SECONDS`` (default five minutes). The streaming endpoint
remains available for callers that prefer authenticated range-aware delivery
(such as older media pipelines or proxies that want to keep access logging on
the backend). Both endpoints authorize first; an attachment belonging to
somebody else's conversation is indistinguishable from a non-existent one
(404), so UUIDs cannot be probed.
"""

from __future__ import annotations

import re

from django.conf import settings
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse
from django.utils.http import http_date, quote_etag
from rest_framework.decorators import api_view, parser_classes, throttle_classes
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from apps.conversations.models import Attachment
from apps.core.throttles import UploadThrottle

from .services import can_read, signed_url, variant_key

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
VARIANTS = ("original", "thumbnail", "optimized")


def _load(request, attachment_id) -> Attachment:
    attachment = (
        Attachment.objects.select_related("message", "message__conversation").filter(id=attachment_id).first()
    )
    if not attachment or not can_read(request.user, attachment):
        raise Http404
    if attachment.message.deleted_at:
        raise Http404
    return attachment


def _variant(request) -> str:
    variant = str(request.query_params.get("variant", "original")).lower()
    if variant not in VARIANTS:
        raise ValidationError("Unsupported media variant.")
    return variant


def _file_size(key: str) -> int:
    try:
        return default_storage.size(key)
    except (OSError, NotImplementedError):  # pragma: no cover
        return 0


def _iter_range(handle, start: int, length: int, chunk: int):
    """Yield at most ``length`` bytes from ``start`` without buffering the file."""
    handle.seek(start)
    remaining = length
    while remaining > 0:
        data = handle.read(min(chunk, remaining))
        if not data:
            break
        remaining -= len(data)
        yield data
    handle.close()


@api_view(["GET"])
def download(request, attachment_id):
    attachment = _load(request, attachment_id)
    variant = _variant(request)
    key = variant_key(attachment, variant)
    if not key:
        raise Http404

    content_type = "image/webp" if variant in ("thumbnail", "optimized") and key.endswith(".webp") else (
        "image/jpeg" if variant in ("thumbnail", "optimized") else attachment.mime_type
    )
    etag = quote_etag(f"{attachment.id}-{variant}-{attachment.updated_at.timestamp()}")
    if request.headers.get("If-None-Match") == etag:
        response = HttpResponse(status=304)
        response["ETag"] = etag
        return response

    size = _file_size(key)
    range_header = request.headers.get("Range", "")
    match = RANGE_RE.match(range_header) if range_header and size else None

    try:
        handle = default_storage.open(key, "rb")
    except FileNotFoundError as exc:
        raise Http404 from exc

    if match:
        start_raw, end_raw = match.groups()
        if start_raw:
            start = int(start_raw)
            end = int(end_raw) if end_raw else size - 1
        else:  # suffix range: bytes=-500
            length = int(end_raw or 0)
            start = max(size - length, 0)
            end = size - 1
        if start >= size or start > end:
            handle.close()
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{size}"
            return response
        end = min(end, size - 1)
        length = end - start + 1
        response = StreamingHttpResponse(
            _iter_range(handle, start, length, settings.MEDIA_STREAM_CHUNK_SIZE),
            status=206,
            content_type=content_type,
        )
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
        response["Content-Length"] = str(length)
    else:
        response = FileResponse(handle, content_type=content_type)
        if size:
            response["Content-Length"] = str(size)

    response["Accept-Ranges"] = "bytes"
    response["ETag"] = etag
    response["Last-Modified"] = http_date(attachment.updated_at.timestamp())
    # Private media must never enter a shared/proxy cache.
    response["Cache-Control"] = "private, max-age=300, no-transform"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Disposition"] = (
        f'inline; filename="{attachment.original_name}"'
        if request.query_params.get("download") != "1"
        else f'attachment; filename="{attachment.original_name}"'
    )
    return response


@api_view(["GET"])
def media_url(request, attachment_id):
    """Authorize and return a short-lived signed URL for the original variant."""
    attachment = _load(request, attachment_id)
    from django.utils import timezone

    expires_at = timezone.now() + timezone.timedelta(seconds=settings.SIGNED_URL_TTL_SECONDS)
    payload = {
        "id": str(attachment.id),
        "url": signed_url(attachment, "original") or request.build_absolute_uri(f"/api/media/{attachment.id}/"),
        "thumbnail_url": (
            signed_url(attachment, "thumbnail")
            or (request.build_absolute_uri(f"/api/media/{attachment.id}/?variant=thumbnail") if attachment.thumbnail_key else None)
        ),
        "optimized_url": (
            signed_url(attachment, "optimized")
            or (request.build_absolute_uri(f"/api/media/{attachment.id}/?variant=optimized") if attachment.optimized_key else None)
        ),
        "mime_type": attachment.mime_type,
        "processing_state": attachment.processing_state,
        "expires_at": expires_at.isoformat(),
    }
    return Response({"success": True, "message": "Media URL issued", "data": payload})


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
@throttle_classes([UploadThrottle])
def upload(request):
    """Standalone upload endpoint.

    The primary path for sending media is a multipart POST to
    ``/api/conversations/{id}/messages/`` (one atomic operation: message +
    attachment + receipts + realtime event). This endpoint exists for the
    branding/profile flows that need a validated file without a message, and
    requires an explicit conversation so authorization is never ambiguous.
    """
    conversation_id = request.data.get("conversation")
    if not conversation_id:
        raise ValidationError("A conversation is required for media uploads.")
    from apps.conversations.models import Conversation
    from apps.conversations.views import create_media_message

    conversation = Conversation.objects.filter(id=conversation_id).first()
    if not conversation:
        raise Http404
    return create_media_message(request, conversation)
