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
import re
import time

from django.conf import settings
from django.db import DatabaseError, IntegrityError, OperationalError, connection

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
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _header_request_id(request) -> str:
    """Adopt only the bounded ASCII grammar shared by the browser and Render."""
    raw = str(request.headers.get("X-Request-ID", "") or "").strip()
    if _REQUEST_ID_RE.fullmatch(raw):
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
            user_role="-",
            operation=f"{request.method} {request.path}",
        )
        try:
            response = self.get_response(request)
        finally:
            reset_request_context(tokens)
        response["X-Request-ID"] = request_id
        return response


class RequestLogMiddleware:
    """One structured line per slow / failed request, with safe DB timings."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.slow_ms = int(getattr(settings, "SLOW_REQUEST_MS", SLOW_REQUEST_MS_DEFAULT))
        self.slow_query_ms = int(getattr(settings, "SLOW_QUERY_MS", 500))

    def __call__(self, request):
        started = time.perf_counter()
        db = {"queries": 0, "ms": 0.0, "slow": 0, "slowest_ms": 0.0}

        # Count/time SQL without capturing SQL or parameters (which could
        # contain message bodies or other private data). Connection.execute_wrapper
        # is request-scoped and adds only a small timer around each DB call.
        def measure_query(execute, sql, params, many, context):
            query_started = time.perf_counter()
            try:
                return execute(sql, params, many, context)
            finally:
                elapsed = (time.perf_counter() - query_started) * 1000
                db["queries"] += 1
                db["ms"] += elapsed
                db["slowest_ms"] = max(db["slowest_ms"], elapsed)
                if elapsed >= self.slow_query_ms:
                    db["slow"] += 1

        with connection.execute_wrapper(measure_query):
            response = self.get_response(request)
        duration_ms = int((time.perf_counter() - started) * 1000)

        # DRF authentication populates request.user during the view.
        user = getattr(request, "user", None)
        authenticated = bool(getattr(user, "is_authenticated", False))
        user_id = str(getattr(user, "id", "")) if authenticated else "-"
        role = str(getattr(user, "role", "-")).upper() if authenticated else "-"
        user_role = role if role in {"ADMIN", "MEMBER"} else "-"
        status = getattr(response, "status_code", 0)
        slow = duration_ms >= self.slow_ms

        if status >= 500:
            level = logging.ERROR
        elif status >= 400 or slow:
            level = logging.WARNING
        else:
            return response

        context_tokens = set_request_context(user_id=user_id, user_role=user_role)
        try:
            logger.log(
                level,
                "%s %s -> %s in %dms role=%s db_queries=%d db_ms=%d db_slow=%d db_slowest_ms=%d%s",
                request.method,
                sanitize(getattr(request, "path", "/"), limit=300),
                status,
                duration_ms,
                user_role,
                db["queries"],
                round(db["ms"]),
                db["slow"],
                round(db["slowest_ms"]),
                " [SLOW]" if slow else "",
                extra={"user_id": user_id, "user_role": user_role},
            )
        finally:
            reset_request_context(context_tokens)
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
        authenticated = bool(getattr(user, "is_authenticated", False))
        user_id = str(getattr(user, "id", "")) if authenticated else "-"
        role = str(getattr(user, "role", "-")).upper() if authenticated else "-"
        user_role = role if role in {"ADMIN", "MEMBER"} else "-"

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
            sanitize(getattr(request, "path", "/"), limit=300),
            exception.__class__.__name__,
            sanitize(exception, limit=400),
            exc_info=exception,
            extra={"user_id": user_id, "user_role": user_role, "request_id": get_request_id()},
        )
        return None  # let Django/DRF produce the actual response
