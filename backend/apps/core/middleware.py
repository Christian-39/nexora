"""
NEXORA — request correlation, access logging and last-resort exception capture.

Three middleware, in the order they are installed:

``RequestIDMiddleware``
    Generates (or adopts) a request id, binds it — together with the
    authenticated user id and the resolved operation name — to the logging
    context, and echoes it back in the ``X-Request-ID`` response header so a
    failed browser request can be matched to its Render log entry.

``RequestLogMiddleware``
    Emits one line per *interesting* request (slow, 4xx or 5xx). Successful
    fast requests are deliberately NOT logged: Render's log view must stay
    readable, and Gunicorn already writes an access log.

``ExceptionLogMiddleware``
    Catches anything that escapes the view — including non-DRF code paths such
    as plain Django views, ``Http404`` handlers and middleware below it — and
    logs the full traceback exactly once. The DRF handler marks exceptions it
    has already reported so nothing is logged twice.
"""

from __future__ import annotations

import logging
import time

from django.conf import settings
from django.db import DatabaseError, IntegrityError, OperationalError

from .observability import (
    get_request_id,
    new_request_id,
    reset_request_context,
    sanitize,
    set_request_context,
)

logger = logging.getLogger("nexora.request")
exception_logger = logging.getLogger("nexora.exception")

#: Set on an exception instance once it has been logged, so the DRF handler and
#: this middleware cannot report the same failure twice.
LOGGED_ATTR = "_nexora_logged"

#: Requests slower than this are reported at WARNING even when they succeed.
SLOW_REQUEST_MS_DEFAULT = 2000


def _header_request_id(request) -> str:
    """Adopt a client-supplied id only when it is a safe, bounded token."""
    raw = str(request.headers.get("X-Request-ID", "") or "")[:64].strip()
    if raw and all(char.isalnum() or char in "-_" for char in raw):
        return raw
    return new_request_id()


class RequestIDMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = _header_request_id(request)
        request.request_id = request_id
        tokens = set_request_context(
            request_id=request_id,
            user_id="-",
            operation=f"{request.method} {request.path}",
        )
        try:
            response = self.get_response(request)
        finally:
            reset_request_context(tokens)
        response["X-Request-ID"] = request_id
        return response


class RequestLogMiddleware:
    """One structured line per slow / failed request."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.slow_ms = int(getattr(settings, "SLOW_REQUEST_MS", SLOW_REQUEST_MS_DEFAULT))

    def __call__(self, request):
        started = time.perf_counter()
        response = self.get_response(request)
        duration_ms = int((time.perf_counter() - started) * 1000)

        # The authenticated user is only known after the view ran.
        user = getattr(request, "user", None)
        user_id = str(getattr(user, "id", "")) if getattr(user, "is_authenticated", False) else "-"
        set_request_context(user_id=user_id)

        status = getattr(response, "status_code", 0)
        slow = duration_ms >= self.slow_ms

        if status >= 500:
            level = logging.ERROR
        elif status >= 400 or slow:
            level = logging.WARNING
        else:
            return response

        logger.log(
            level,
            "%s %s -> %s in %dms%s",
            request.method,
            sanitize(request.get_full_path(), limit=300),
            status,
            duration_ms,
            " [SLOW]" if slow else "",
            extra={"user_id": user_id},
        )
        return response


class ExceptionLogMiddleware:
    """Last-resort capture: nothing may reach the client unlogged."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):  # noqa: D401 - Django hook
        if getattr(exception, LOGGED_ATTR, False):
            return None  # already reported by the DRF handler
        setattr(exception, LOGGED_ATTR, True)

        user = getattr(request, "user", None)
        user_id = str(getattr(user, "id", "")) if getattr(user, "is_authenticated", False) else "-"

        if isinstance(exception, IntegrityError):
            category = "database integrity failure"
        elif isinstance(exception, OperationalError):
            category = "database operational failure"
        elif isinstance(exception, DatabaseError):
            category = "database failure"
        else:
            category = "unhandled exception"

        exception_logger.error(
            "%s during %s %s: %s: %s",
            category,
            request.method,
            sanitize(request.get_full_path(), limit=300),
            exception.__class__.__name__,
            sanitize(exception, limit=400),
            exc_info=exception,
            extra={"user_id": user_id, "request_id": get_request_id()},
        )
        return None  # let Django/DRF produce the actual response
