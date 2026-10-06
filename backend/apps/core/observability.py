"""
NEXORA — production observability primitives.

Everything in this module exists so that *one* failure in production produces
*one* searchable line (plus a traceback when there is one) on the service's
standard output, which is what Render streams into
``Dashboard → nexora-backend → Logs``.

Design rules
------------
* A request-scoped id is generated once per request, returned in the
  ``X-Request-ID`` response header, included in every log record emitted while
  that request is being served, and included in API error bodies. A user can
  therefore read an id off a failed browser request and grep Render for it.
* Secrets never reach a log record. :func:`scrub` removes PINs, passwords,
  tokens, cookies, authorization headers and storage/database credentials, and
  :func:`sanitize` neutralises log injection (CR/LF, control characters) in
  user-controlled values.
* Nothing here may raise. An observability bug must never become an outage.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from contextvars import ContextVar

#: Request-scoped correlation id, readable from anywhere (including code that
#: has no ``request`` object, such as a service layer or a Channels consumer).
_request_id: ContextVar[str] = ContextVar("nexora_request_id", default="-")
_user_id: ContextVar[str] = ContextVar("nexora_user_id", default="-")
_user_role: ContextVar[str] = ContextVar("nexora_user_role", default="-")
_operation: ContextVar[str] = ContextVar("nexora_operation", default="-")


def new_request_id() -> str:
    return uuid.uuid4().hex[:32]


def set_request_context(*, request_id: str | None = None, user_id=None, user_role=None, operation: str | None = None):
    """Bind correlation values for the current execution context."""
    tokens = {}
    if request_id is not None:
        tokens["request_id"] = _request_id.set(str(request_id)[:64] or "-")
    if user_id is not None:
        tokens["user_id"] = _user_id.set(str(user_id)[:64] or "-")
    if user_role is not None:
        tokens["user_role"] = _user_role.set(str(user_role)[:20] or "-")
    if operation is not None:
        tokens["operation"] = _operation.set(str(operation)[:120] or "-")
    return tokens


def reset_request_context(tokens) -> None:
    for name, token in (tokens or {}).items():
        try:
            {"request_id": _request_id, "user_id": _user_id, "user_role": _user_role, "operation": _operation}[name].reset(token)
        except (KeyError, ValueError):  # pragma: no cover - defensive
            pass


def get_request_id() -> str:
    return _request_id.get()


def get_user_id() -> str:
    return _user_id.get()


def get_user_role() -> str:
    return _user_role.get()


def get_operation() -> str:
    return _operation.get()


# ---------------------------------------------------------------------------
# Scrubbing / sanitisation
# ---------------------------------------------------------------------------

#: Substrings that mark a key as secret. Matching is case-insensitive and
#: partial, so ``HTTP_AUTHORIZATION``, ``new_pin`` and ``STORAGE_SECRET_KEY``
#: are all caught.
SECRET_MARKERS = (
    "pin",
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "cookie",
    "csrf",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "credential",
    "session",
    "signature",
    "signed_url",
    "presigned_url",
    "database_url",
    "redis_url",
)

REDACTED = "[redacted]"

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URL_CREDENTIALS = re.compile(r"\b(redis|rediss|mysql)://[^@\s]+@", re.IGNORECASE)
_KV_SECRET = re.compile(
    r"""(?P<open_quote>['\"]?)\b
       (?P<key>
           (?:[A-Za-z0-9]+[_-])*
           (?:
               pin|current[_-]?pin|new[_-]?pin|confirm[_-]?pin|old[_-]?pin|
               password|passwd|passphrase|secret|token|access[_-]?token|refresh[_-]?token|id[_-]?token|
               authorization|cookie|set[_-]?cookie|csrf(?:[_-]?token|middlewaretoken)?|api[_-]?key|access[_-]?key|
               private[_-]?key|credential(?:s)?|signature|signed[_-]?url|presigned[_-]?url|signed[_-]?uri|
               database[_-]?url|redis[_-]?url|connection[_-]?string|dsn
           )
           (?:[_-][A-Za-z0-9]+)*
       )\b(?P<close_quote>['\"]?)
       (?P<separator>\s*[:=]\s*)
       (?:\"[^\"]*\"|'[^']*'|[^\s,&;}\]]+)
    """,
    re.IGNORECASE | re.VERBOSE,
)
_BEARER_TOKEN = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_JWT_TOKEN = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")


def is_secret_key(key) -> bool:
    lowered = str(key).lower()
    return any(marker in lowered for marker in SECRET_MARKERS)


def sanitize(value, *, limit: int = 500) -> str:
    """Make a user-controlled value safe to put on one log line.

    Newlines and carriage returns become ``\\n``/``\\r`` escapes so a crafted
    value cannot forge additional log entries (log injection), control
    characters and embedded credentials/tokens/PINs are stripped, and the
    result is length-bounded.
    """
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 - __str__ may raise on exotic objects
        return "[unprintable]"
    text = text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    text = _CONTROL.sub("", text)
    text = _URL_CREDENTIALS.sub(r"\1://[redacted]@", text)
    text = _KV_SECRET.sub(
        lambda match: (
            f"{match.group('open_quote')}{match.group('key')}"
            f"{match.group('close_quote')}{match.group('separator')}{REDACTED}"
        ),
        text,
    )
    text = _BEARER_TOKEN.sub(f"Bearer {REDACTED}", text)
    text = _JWT_TOKEN.sub(REDACTED, text)
    if len(text) > limit:
        text = f"{text[:limit]}…(+{len(text) - limit} chars)"
    return text


def scrub(data, *, depth: int = 0):
    """Recursively redact secret-looking values in a mapping/sequence."""
    if depth > 4:
        return "[truncated]"
    if isinstance(data, dict):
        return {
            str(key)[:64]: (REDACTED if is_secret_key(key) else scrub(value, depth=depth + 1))
            for key, value in list(data.items())[:50]
        }
    if isinstance(data, (list, tuple)):
        return [scrub(item, depth=depth + 1) for item in list(data)[:25]]
    if isinstance(data, (int, float, bool)) or data is None:
        return data
    return sanitize(data, limit=200)


# ---------------------------------------------------------------------------
# Logging filter + formatter
# ---------------------------------------------------------------------------


class RequestContextFilter(logging.Filter):
    """Attach the request correlation values to every record.

    Installed on the console handler so *all* records — including third-party
    ones (django.request, channels, botocore) — carry the id.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.request_id = getattr(record, "request_id", None) or get_request_id()
        record.user_id = getattr(record, "user_id", None) or get_user_id()
        record.user_role = getattr(record, "user_role", None) or get_user_role()
        record.operation = getattr(record, "operation", None) or get_operation()
        return True


class BelowErrorFilter(logging.Filter):
    """Pass only records below ERROR.

    Paired with the stdout handler so that each record is written exactly
    once: DEBUG/INFO/WARNING to stdout, ERROR/CRITICAL to stderr. Without it
    a logger wired to both handlers emits every error twice in Render.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        return record.levelno < logging.ERROR


class ProductionFormatter(logging.Formatter):
    """``<ts> <LEVEL> <logger> [req=<id> user=<id> op=<name>] <message>``.

    Deliberately line-oriented (not JSON): Render's log search is a substring
    search, so ``req=<id>`` is the most useful thing a human can paste into it.
    Tracebacks are appended by the base Formatter and kept intact.
    """

    default_msec_format = "%s.%03d"

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "request_id"):
            record.request_id = get_request_id()
        if not hasattr(record, "user_id"):
            record.user_id = get_user_id()
        if not hasattr(record, "user_role"):
            record.user_role = get_user_role()
        if not hasattr(record, "operation"):
            record.operation = get_operation()
        rendered = super().format(record)
        # Scrub fully rendered records too, including exception text emitted by
        # ``exc_info``. Preserve traceback line boundaries while preventing
        # credential-shaped values and control characters from reaching logs.
        return "\n".join(sanitize(line, limit=4000) for line in rendered.splitlines())


# ---------------------------------------------------------------------------
# Timing helper used by the upload pipeline and the slow-query guard
# ---------------------------------------------------------------------------


class Stopwatch:
    """Measure stages of a multi-step operation for a single summary log line."""

    def __init__(self) -> None:
        self._start = time.perf_counter()
        self._mark = self._start
        self.stages: dict[str, int] = {}

    def lap(self, name: str) -> int:
        now = time.perf_counter()
        elapsed_ms = int((now - self._mark) * 1000)
        self._mark = now
        self.stages[name] = elapsed_ms
        return elapsed_ms

    @property
    def total_ms(self) -> int:
        return int((time.perf_counter() - self._start) * 1000)

    def summary(self) -> str:
        parts = " ".join(f"{name}={value}ms" for name, value in self.stages.items())
        return f"{parts} total={self.total_ms}ms" if parts else f"total={self.total_ms}ms"
