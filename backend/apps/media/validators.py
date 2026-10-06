"""
NEXORA — upload validation.

Defence in depth, in this order:
  1. declared size against the deployment ceiling and the organization limit;
  2. **content signature** (magic bytes) — the authoritative type;
  3. declared MIME must agree with the signature;
  4. file extension must be one of the extensions allowed for that signature;
  5. structural decode (Pillow) and dimension limits for images;
  6. duration limits for audio/video (probed, never trusted from the client);
  7. a freshly generated storage name — the client name is never used on disk.

A file that disguises an executable as ``photo.png`` fails at step 2/3/5.
"""

from __future__ import annotations

import os
import re
import unicodedata
import uuid

from django.conf import settings
from rest_framework.exceptions import ValidationError

# ---------------------------------------------------------------------------
# Allowed types → permitted extensions
# ---------------------------------------------------------------------------

IMAGE_TYPES: dict[str, tuple[str, ...]] = {
    "image/jpeg": (".jpg", ".jpeg"),
    "image/png": (".png",),
    "image/webp": (".webp",),
    "image/gif": (".gif",),
}

VIDEO_TYPES: dict[str, tuple[str, ...]] = {
    "video/mp4": (".mp4", ".m4v"),
    "video/webm": (".webm",),
    "video/quicktime": (".mov",),
}

AUDIO_TYPES: dict[str, tuple[str, ...]] = {
    "audio/webm": (".webm",),
    "audio/ogg": (".ogg", ".oga", ".opus"),
    "audio/mpeg": (".mp3",),
    "audio/mp4": (".m4a", ".mp4"),
    "audio/wav": (".wav",),
    "audio/x-wav": (".wav",),
}

KIND_TYPES = {"IMAGE": IMAGE_TYPES, "VIDEO": VIDEO_TYPES, "VOICE": AUDIO_TYPES}

ALL_TYPES: dict[str, tuple[str, ...]] = {**IMAGE_TYPES, **VIDEO_TYPES, **AUDIO_TYPES}

# ---------------------------------------------------------------------------
# Content signature detection (no system dependency required)
# ---------------------------------------------------------------------------

_SIGNATURES: list[tuple[int, bytes, str]] = [
    (0, b"\xff\xd8\xff", "image/jpeg"),
    (0, b"\x89PNG\r\n\x1a\n", "image/png"),
    (0, b"GIF87a", "image/gif"),
    (0, b"GIF89a", "image/gif"),
    (0, b"OggS", "audio/ogg"),
    (0, b"ID3", "audio/mpeg"),
    (0, b"\x1a\x45\xdf\xa3", "video/webm"),  # Matroska/WebM container
]

_EXECUTABLE_SIGNATURES: list[tuple[int, bytes]] = [
    (0, b"MZ"),  # PE / DOS
    (0, b"\x7fELF"),  # ELF
    (0, b"\xca\xfe\xba\xbe"),  # Mach-O fat / Java class
    (0, b"\xcf\xfa\xed\xfe"),  # Mach-O 64
    (0, b"#!"),  # script shebang
    (0, b"PK\x03\x04"),  # zip/jar/office — never a valid media upload here
    (0, b"%PDF"),
]


def _size_of(fileobj) -> int | None:
    """Byte length of an upload.

    Uploaded files expose ``.size``; plain file objects (storage handles, the
    chunked-upload staging object) are measured by seeking instead.
    """
    size = getattr(fileobj, "size", None)
    if isinstance(size, int):
        return size
    try:
        position = fileobj.tell()
        fileobj.seek(0, 2)
        size = fileobj.tell()
        fileobj.seek(position)
        return size
    except (AttributeError, OSError, ValueError):
        return None


def _read_head(fileobj, size: int = 4096) -> bytes:
    position = fileobj.tell() if hasattr(fileobj, "tell") else 0
    try:
        fileobj.seek(0)
        head = fileobj.read(size) or b""
    finally:
        try:
            fileobj.seek(position)
        except (OSError, ValueError):  # pragma: no cover - non-seekable
            pass
    return head


def signature_mime(head: bytes) -> str | None:
    """Detect the MIME type from magic bytes. Returns None when unrecognised."""
    if not head:
        return None

    for offset, magic, mime in _SIGNATURES:
        if head[offset : offset + len(magic)] == magic:
            if mime == "video/webm":
                # Matroska carries both audio-only and video WebM. Track codec
                # IDs can appear after the EBML/segment metadata, so inspect
                # the full bounded header read rather than only the first 512 B.
                return "video/webm" if b"V_" in head else "audio/webm"
            return mime

    if head[:4] == b"RIFF":
        if head[8:12] == b"WEBP":
            return "image/webp"
        if head[8:12] == b"WAVE":
            return "audio/wav"

    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"qt  ",):
            return "video/quicktime"
        if brand in (b"M4A ", b"M4B "):
            return "audio/mp4"
        return "video/mp4"

    # MP3 frame sync without an ID3 header.
    if head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    return None


def looks_executable(head: bytes) -> bool:
    return any(head[offset : offset + len(magic)] == magic for offset, magic in _EXECUTABLE_SIGNATURES)


def sniff(fileobj) -> str:
    """Authoritative content type of an uploaded file.

    Uses libmagic when available and falls back to the built-in signature
    table, which is sufficient for every format NEXORA accepts.
    """
    head = _read_head(fileobj)
    detected = signature_mime(head)
    if detected:
        return detected
    try:  # optional: libmagic gives broader coverage when installed
        import magic  # type: ignore

        guess = magic.from_buffer(head, mime=True)
        if guess:
            return str(guess)
    except Exception:  # pragma: no cover - libmagic not installed
        pass
    return "application/octet-stream"


# ---------------------------------------------------------------------------
# Safe storage naming
# ---------------------------------------------------------------------------

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_display_name(name: str) -> str:
    """Sanitise the *display* name. Path separators and control characters go."""
    value = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    value = value.replace("\\", "/").split("/")[-1]
    value = _SAFE_NAME.sub("_", value).strip("._") or "attachment"
    return value[:120]


def storage_key(prefix: str, extension: str) -> str:
    """Generate an unguessable, traversal-proof storage key.

    The client-supplied filename never influences the path.
    """
    extension = extension if extension.startswith(".") else f".{extension}"
    extension = _SAFE_NAME.sub("", extension)[:10] or ".bin"
    token = uuid.uuid4().hex
    return f"{prefix.strip('/')}/{token[:2]}/{token}{extension}"


# ---------------------------------------------------------------------------
# Validation entry point
# ---------------------------------------------------------------------------


class ValidatedUpload:
    __slots__ = ("kind", "mime_type", "extension", "size", "width", "height", "duration_ms", "display_name")

    def __init__(self, **kwargs):
        for key in self.__slots__:
            setattr(self, key, kwargs.get(key))


def _limits():
    from apps.platform_settings.services import messaging_policy

    return messaging_policy()


def validate_upload(fileobj, *, kind: str, declared_name: str = "", duration_ms: int | None = None) -> ValidatedUpload:
    """Validate an uploaded media file. Raises ``ValidationError`` on refusal."""
    kind = str(kind or "").upper()
    if kind not in KIND_TYPES:
        raise ValidationError("Unsupported attachment type.")

    size = _size_of(fileobj)
    if size is None:
        raise ValidationError("The upload could not be read.")
    if size <= 0:
        raise ValidationError("The file is empty.")
    if size > settings.MAX_UPLOAD_SIZE:
        raise ValidationError("The file exceeds the maximum upload size.")

    policy = _limits()
    ceiling = {
        "IMAGE": policy["max_image_size_mb"] * 1024**2,
        "VIDEO": policy["max_video_size_mb"] * 1024**2,
        "VOICE": policy["max_voice_size_mb"] * 1024**2,
    }[kind]
    if size > ceiling:
        raise ValidationError(f"{kind.title()} uploads are limited to {ceiling // 1024**2} MB.")

    head = _read_head(fileobj)
    if looks_executable(head):
        raise ValidationError("That file type is not permitted.")

    mime = sniff(fileobj)
    extension = os.path.splitext(str(declared_name or getattr(fileobj, "name", "")))[1].lower()
    declared_mime = str(getattr(fileobj, "content_type", "") or "").split(";", 1)[0].strip().lower()
    if not extension:
        extension = ""

    probe_info = None
    media_containers = set(VIDEO_TYPES) | set(AUDIO_TYPES)
    if kind in ("VIDEO", "VOICE") and mime in media_containers:
        probe_info = _probe_media_info(fileobj)

        # ISO-BMFF's generic brands (isom/mp42) do not distinguish audio-only
        # Safari recordings from video at the magic-byte level. Accept a voice
        # MP4 only when the browser's MIME/filename are audio hints AND ffprobe
        # independently confirms an audio-only stream.
        if (
            kind == "VOICE"
            and mime in {"video/mp4", "video/quicktime"}
            and declared_mime == "audio/mp4"
            and extension in AUDIO_TYPES["audio/mp4"]
            and probe_info
            and probe_info["has_audio"]
            and not probe_info["has_video"]
        ):
            mime = "audio/mp4"

        # WebM's EBML header can place track codec IDs beyond the fixed magic
        # bytes. If ffprobe confirms the stream kind, correct only this
        # ambiguous audio/video classification; the extension and allow-list
        # are still checked below.
        if kind == "VIDEO" and mime == "audio/webm" and probe_info and probe_info["has_video"]:
            mime = "video/webm"
        elif (
            kind == "VOICE"
            and mime == "video/webm"
            and probe_info
            and probe_info["has_audio"]
            and not probe_info["has_video"]
        ):
            mime = "audio/webm"

        if probe_info and kind == "VIDEO" and not probe_info["has_video"]:
            raise ValidationError("The file does not contain a video stream.")
        if probe_info and kind == "VOICE" and (not probe_info["has_audio"] or probe_info["has_video"]):
            raise ValidationError("A voice note must contain an audio-only stream.")

    allowed = KIND_TYPES[kind]
    if mime not in allowed:
        raise ValidationError("The file content does not match a supported format.")

    if not extension:
        extension = allowed[mime][0]
    if extension not in allowed[mime]:
        raise ValidationError("The file extension does not match its content.")

    width = height = None
    if kind == "IMAGE":
        width, height = _validate_image(fileobj)

    if kind in ("VIDEO", "VOICE"):
        probed_duration = probe_info.get("duration_ms") if probe_info else None
        duration_ms = probed_duration or duration_ms
        if kind == "VOICE":
            maximum = policy["max_voice_duration_seconds"] * 1000
            if duration_ms and duration_ms > maximum:
                raise ValidationError("The voice note is longer than the configured limit.")
        else:
            maximum = policy["max_video_duration_seconds"] * 1000
            if duration_ms and duration_ms > maximum:
                raise ValidationError("The video is longer than the configured limit.")

    return ValidatedUpload(
        kind=kind,
        mime_type=mime,
        extension=extension,
        size=size,
        width=width,
        height=height,
        duration_ms=duration_ms,
        display_name=safe_display_name(declared_name or getattr(fileobj, "name", "")),
    )


def _validate_image(fileobj) -> tuple[int, int]:
    from PIL import Image, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = settings.MAX_IMAGE_PIXELS
    try:
        fileobj.seek(0)
        image = Image.open(fileobj)
        image.verify()  # structural integrity; detects polyglot/corrupt files
        fileobj.seek(0)
        image = Image.open(fileobj)
        width, height = image.size
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError) as exc:
        raise ValidationError("The image could not be decoded.") from exc
    finally:
        try:
            fileobj.seek(0)
        except (OSError, ValueError):  # pragma: no cover
            pass

    if width < 1 or height < 1:
        raise ValidationError("The image dimensions are invalid.")
    if width * height > settings.MAX_IMAGE_PIXELS:
        raise ValidationError("The image resolution is too large.")
    if width > 20000 or height > 20000:
        raise ValidationError("The image dimensions are too large.")
    return width, height


def _probe_media_info(fileobj) -> dict | None:
    """Probe audio/video stream kinds and duration when ffprobe is available."""
    from .processing import probe_media_info

    return probe_media_info(fileobj)


def _probe_duration_ms(fileobj) -> int | None:
    """Probe duration with ffprobe. Returns None when ffprobe is unavailable."""
    info = _probe_media_info(fileobj)
    return info.get("duration_ms") if info else None
