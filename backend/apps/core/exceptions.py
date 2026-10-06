"""
NEXORA — the single DRF exception handler.

Responsibilities
----------------
1. Keep the documented error envelope exactly as it was:
   ``{"success": false, "message": ..., "code": ..., "errors": {...}}``.
   Existing frontend code and the contract tests depend on this shape.
2. Add ``request_id`` to every error body so a user can quote it and an
   operator can grep Render for the matching server-side entry.
3. Translate infrastructure failures that used to escape as bare HTTP 500s
   (database integrity/operational errors, object-storage failures, Redis
   failures) into the right status code with a *safe* user-facing message.
4. **Log every failure exactly once**, with the full traceback for anything
   unexpected. Previously, a handled DRF error was never logged at all and an
   unhandled one was only logged by ``django.request`` with no correlation id,
   which is why production failures were invisible.

Internal details (tracebacks, SQL, exception text) are never put into the
response body unless DEBUG is on.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import DatabaseError, IntegrityError, OperationalError
from django.http import Http404
from rest_framework import status as http_status
from rest_framework.response import Response
from rest_framework.views import exception_handler

from .cache import CACHE_ERRORS
from .middleware import LOGGED_ATTR
from .observability import get_request_id, sanitize

logger = logging.getLogger("nexora.api")

#: Safe, operation-agnostic fallbacks. Views and serializers supply better
#: wording; these only fill the gaps.
STATUS_MESSAGES = {
    400: "Some of the information provided is not valid.",
    401: "Your session has ended. Please sign in again.",
    403: "You are not authorized to perform this action.",
    404: "That item could not be found.",
    405: "That action is not supported here.",
    409: "This action conflicts with the current state of the data.",
    413: "That file is larger than the allowed limit.",
    415: "That file type is not supported.",
    429: "Too many attempts. Please wait a moment and try again.",
    500: "The server encountered a problem. Please try again shortly.",
    503: "The service is temporarily unavailable. Please try again shortly.",
}


def _infrastructure_response(exc):
    """Map an infrastructure failure onto a correct, safe API response."""
    if isinstance(exc, IntegrityError):
        return Response(
            {
                "success": False,
                "message": "That change conflicts with existing data. Refresh and try again.",
                "code": "DATA_CONFLICT",
                "errors": {},
            },
            status=http_status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, (OperationalError, DatabaseError)):
        return Response(
            {
                "success": False,
                "message": "The service is temporarily unavailable. Please try again shortly.",
                "code": "DATABASE_UNAVAILABLE",
                "errors": {},
            },
            status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    if isinstance(exc, CACHE_ERRORS):
        return Response(
            {
                "success": False,
                "message": "The service is temporarily unavailable. Please try again shortly.",
                "code": "CACHE_UNAVAILABLE",
                "errors": {},
            },
            status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    try:  # botocore is only importable when object storage is configured
        from botocore.exceptions import BotoCoreError, ClientError

        if isinstance(exc, (BotoCoreError, ClientError)):
            return Response(
                {
                    "success": False,
                    "message": "The file service is temporarily unavailable. Please try again shortly.",
                    "code": "STORAGE_UNAVAILABLE",
                    "errors": {},
                },
                status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            )
    except Exception:  # pragma: no cover - botocore absent
        pass
    return None


def _log(exc, context, status_code: int) -> None:
    """Report a failure once, at the level its status code deserves."""
    if getattr(exc, LOGGED_ATTR, False):
        return
    setattr(exc, LOGGED_ATTR, True)

    request = context.get("request")
    view = context.get("view")
    operation = view.__class__.__name__ if view is not None else "-"
    method = getattr(request, "method", "-")
    path = sanitize(getattr(request, "path", "-"), limit=200)
    user = getattr(request, "user", None)
    authenticated = bool(getattr(user, "is_authenticated", False))
    user_id = str(getattr(user, "id", "")) if authenticated else "-"
    role = str(getattr(user, "role", "-")).upper() if authenticated else "-"
    user_role = role if role in {"ADMIN", "MEMBER"} else "-"
    log_context = {"user_id": user_id, "user_role": user_role, "request_id": get_request_id()}

    if status_code >= 500:
        logger.error(
            "%s %s (%s) failed with %s: %s: %s",
            method,
            path,
            operation,
            status_code,
            exc.__class__.__name__,
            sanitize(exc, limit=400),
            exc_info=exc,
            extra=log_context,
        )
    elif status_code in (401, 403, 429):
        # Security-relevant but expected: no traceback, still searchable.
        logger.warning(
            "%s %s (%s) refused with %s: %s",
            method,
            path,
            operation,
            status_code,
            exc.__class__.__name__,
            extra=log_context,
        )
    elif status_code >= 400:
        logger.info(
            "%s %s (%s) rejected with %s: %s",
            method,
            path,
            operation,
            status_code,
            exc.__class__.__name__,
            extra=log_context,
        )


def api_exception_handler(exc, context):
    """The ``EXCEPTION_HANDLER`` configured in ``REST_FRAMEWORK``."""
    # Django-native exceptions DRF already understands.
    if isinstance(exc, Http404):
        pass
    elif isinstance(exc, DjangoPermissionDenied):
        pass
    elif isinstance(exc, DjangoValidationError):
        # A model/service-level ValidationError must surface as 400, not 500.
        from rest_framework.exceptions import ValidationError as DRFValidationError

        exc = DRFValidationError(getattr(exc, "message_dict", None) or list(exc.messages))

    response = exception_handler(exc, context)

    if response is None:
        # Not a DRF-recognised exception: either infrastructure (translate it)
        # or a genuine bug (let it bubble so Django returns 500 and
        # ExceptionLogMiddleware captures the traceback).
        infrastructure = _infrastructure_response(exc)
        if infrastructure is None:
            _log(exc, context, 500)
            return None
        response = infrastructure

    status_code = response.status_code
    _log(exc, context, status_code)

    details = response.data
    code = getattr(exc, "default_code", None) or f"HTTP_{status_code}"
    message = STATUS_MESSAGES.get(status_code, "Request failed.")

    if isinstance(details, dict) and "success" in details:
        # Already an envelope (the infrastructure responses above).
        payload = dict(details)
    else:
        errors = {}
        if isinstance(details, dict):
            if "detail" in details:
                message = str(details["detail"])
            else:
                errors = details
                first = next(iter(details.values()), None)
                if isinstance(first, (list, tuple)) and first:
                    message = str(first[0])
                elif isinstance(first, str):
                    message = first
        elif isinstance(details, list) and details:
            message = str(details[0])
            errors = {"__all__": [str(item) for item in details]}
        payload = {
            "success": False,
            "message": message,
            "code": str(code).upper(),
            "errors": errors,
        }

    payload["request_id"] = get_request_id()
    if settings.DEBUG and status_code >= 500:
        payload["debug"] = f"{exc.__class__.__name__}: {exc}"
    response.data = payload
    return response
