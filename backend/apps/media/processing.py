"""
NEXORA — media derivative processing.

Runs out of the request/response cycle (``manage.py media_worker``) so that a
large upload never blocks a worker thread and is never held in request memory.
Files are streamed to and from storage in chunks.

Derivatives produced
--------------------
* IMAGE : thumbnail (``MEDIA_THUMBNAIL_SIZE``) + optimized WebP when enabled.
* VIDEO : poster frame (JPEG) extracted with ffmpeg, plus probed duration.
* VOICE : probed duration only.

A derivative failure never destroys the original: the attachment is marked
FAILED with a short machine code and the original stays downloadable.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone

from .validators import storage_key

logger = logging.getLogger("nexora.media")

FFPROBE_TIMEOUT = 30
FFMPEG_TIMEOUT = 120


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _binary(path: str) -> str | None:
    return shutil.which(path) or (path if os.path.isabs(path) and os.path.exists(path) else None)


def ffprobe_available() -> bool:
    return _binary(settings.FFPROBE_BINARY) is not None


def ffmpeg_available() -> bool:
    return _binary(settings.FFMPEG_BINARY) is not None


def _spool(fileobj) -> str:
    """Write an uploaded/stored file to a temporary path in bounded chunks."""
    handle = tempfile.NamedTemporaryFile(delete=False)
    try:
        fileobj.seek(0)
        while True:
            chunk = fileobj.read(settings.MEDIA_STREAM_CHUNK_SIZE)
            if not chunk:
                break
            handle.write(chunk)
    finally:
        handle.close()
        try:
            fileobj.seek(0)
        except (OSError, ValueError):  # pragma: no cover
            pass
    return handle.name


def probe_media_info(fileobj) -> dict | None:
    """Return probed stream kinds and duration without trusting the browser MIME.

    ``None`` means ffprobe is unavailable or could not parse the container.
    The upload validator uses stream kinds only to disambiguate ambiguous MP4
    brands and WebM headers; raw probe output is never logged.
    """
    binary = _binary(settings.FFPROBE_BINARY)
    if not binary:
        return None
    path = _spool(fileobj)
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                binary,
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT,
            check=False,
        )
        if result.returncode != 0:
            return None
        payload = json.loads(result.stdout or "{}")
        if not isinstance(payload, dict):
            return None
        streams = payload.get("streams", [])
        streams = streams if isinstance(streams, list) else []
        kinds = {
            str(stream.get("codec_type") or "").lower()
            for stream in streams
            if isinstance(stream, dict)
        }
        format_info = payload.get("format", {})
        format_info = format_info if isinstance(format_info, dict) else {}
        raw_duration = format_info.get("duration")
        try:
            duration_ms = int(float(raw_duration) * 1000) if raw_duration not in (None, "N/A", "") else None
        except (TypeError, ValueError):
            duration_ms = None
        return {
            "has_audio": "audio" in kinds,
            "has_video": "video" in kinds,
            "duration_ms": duration_ms,
        }
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None
    finally:
        os.unlink(path)


def probe_duration_ms(fileobj) -> int | None:
    """Duration in milliseconds via ffprobe, or None when unavailable."""
    info = probe_media_info(fileobj)
    return info.get("duration_ms") if info else None


# ---------------------------------------------------------------------------
# derivative generation
# ---------------------------------------------------------------------------


def _image_derivatives(attachment) -> dict:
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = settings.MAX_IMAGE_PIXELS
    updates: dict = {}

    with default_storage.open(attachment.storage_key, "rb") as source:
        image = Image.open(source)
        image = ImageOps.exif_transpose(image)
        image.load()

    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")

    updates["width"], updates["height"] = image.size

    thumbnail = image.copy()
    thumbnail.thumbnail((settings.MEDIA_THUMBNAIL_SIZE, settings.MEDIA_THUMBNAIL_SIZE))
    updates["thumbnail_key"] = _save_image(thumbnail, "media/thumbnails", prefer_webp=True)

    if settings.MEDIA_WEBP_ENABLED:
        optimized = image.copy()
        optimized.thumbnail((settings.MEDIA_OPTIMIZED_SIZE, settings.MEDIA_OPTIMIZED_SIZE))
        updates["optimized_key"] = _save_image(optimized, "media/optimized", prefer_webp=True)

    return updates


def _save_image(image, prefix: str, *, prefer_webp: bool) -> str:
    import io

    buffer = io.BytesIO()
    if prefer_webp and settings.MEDIA_WEBP_ENABLED:
        image.save(buffer, format="WEBP", quality=82, method=4)
        extension = ".webp"
    else:
        if image.mode == "RGBA":
            image = image.convert("RGB")
        image.save(buffer, format="JPEG", quality=85, optimize=True)
        extension = ".jpg"
    buffer.seek(0)
    key = storage_key(prefix, extension)
    return default_storage.save(key, ContentFile(buffer.read()))


def _video_poster(attachment) -> dict:
    binary = _binary(settings.FFMPEG_BINARY)
    if not binary:
        raise RuntimeError("FFMPEG_MISSING")

    with default_storage.open(attachment.storage_key, "rb") as source:
        local = _spool(source)

    poster_path = f"{local}.jpg"
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                binary,
                "-nostdin",
                "-y",
                "-ss",
                "00:00:01",
                "-i",
                local,
                "-frames:v",
                "1",
                "-vf",
                f"scale='min({settings.MEDIA_THUMBNAIL_SIZE},iw)':-2",
                "-f",
                "image2",
                poster_path,
            ],
            capture_output=True,
            timeout=FFMPEG_TIMEOUT,
            check=False,
        )
        if result.returncode != 0 or not os.path.exists(poster_path) or os.path.getsize(poster_path) == 0:
            raise RuntimeError("POSTER_FAILED")
        with open(poster_path, "rb") as poster:
            key = default_storage.save(storage_key("media/posters", ".jpg"), ContentFile(poster.read()))
        return {"thumbnail_key": key}
    finally:
        for path in (local, poster_path):
            if os.path.exists(path):
                os.unlink(path)


def _probe_media_duration(attachment) -> dict:
    with default_storage.open(attachment.storage_key, "rb") as source:
        duration = probe_duration_ms(source)
    return {"duration_ms": duration} if duration else {}


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------


def process_attachment(attachment) -> str:
    """Generate derivatives for one attachment and broadcast ``media.ready``.

    Returns the resulting processing state. Safe to call repeatedly.
    """
    from apps.conversations.models import Attachment

    updated = Attachment.objects.filter(pk=attachment.pk, processing_state__in=["PENDING", "RETRY"]).update(
        processing_state="PROCESSING", processing_error=""
    )
    if not updated:
        return attachment.processing_state
    attachment.refresh_from_db()

    kind = attachment.message.type
    try:
        if kind == "IMAGE":
            updates = _image_derivatives(attachment)
        elif kind == "VIDEO":
            updates = {**_probe_media_duration(attachment), **_video_poster(attachment)}
        elif kind == "VOICE":
            updates = _probe_media_duration(attachment)
        else:
            updates = {}
        updates["processing_state"] = "READY"
        updates["processing_error"] = ""
    except Exception as exc:  # noqa: BLE001 - never lose the original file
        code = str(exc)[:80] if isinstance(exc, RuntimeError) else exc.__class__.__name__[:80]
        logger.warning("media processing failed for %s: %s", attachment.pk, code)
        attachment.processing_attempts += 1
        retryable = attachment.processing_attempts < 3 and code not in {"FFMPEG_MISSING"}
        updates = {
            "processing_state": "RETRY" if retryable else "FAILED",
            "processing_error": code,
            "processing_attempts": attachment.processing_attempts,
            "next_attempt_at": timezone.now() + timezone.timedelta(seconds=30 * attachment.processing_attempts)
            if retryable
            else None,
        }

    for field, value in updates.items():
        setattr(attachment, field, value)
    attachment.save(update_fields=[*updates.keys(), "updated_at"])

    if attachment.processing_state in ("READY", "FAILED"):
        _broadcast_ready(attachment)
    return attachment.processing_state


def _broadcast_ready(attachment) -> None:
    from apps.conversations.realtime import broadcast_media_ready

    broadcast_media_ready(attachment)
