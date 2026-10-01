"""
NEXORA — frontend error ingestion.

``POST /api/client-errors/``

Browser JavaScript failures never reach Render on their own. The frontend's
``assets/js/errors.js`` captures uncaught exceptions, unhandled promise
rejections, failed API requests, upload failures, WebSocket failures and
service-worker failures, and posts them here in small batches. Each one is
written to stdout by the ``nexora.client`` logger, so it lands in the Render
service log next to the backend's own entries and can be correlated through
the request id.

Safety properties
-----------------
* **Unauthenticated but rate-limited** — the login screen must be able to
  report that it crashed, but an anonymous visitor must not be able to flood
  production logs. ``ClientErrorThrottle`` (``THROTTLE_CLIENT_ERRORS``,
  default 30/hour) and a hard per-request batch/field size limit apply.
* **Sanitised** — every value is length-bounded and stripped of control
  characters and newlines (log-injection safe) by ``observability.sanitize``.
* **Secret-free** — the payload is passed through ``observability.scrub``, so
  a field accidentally named ``pin``/``token``/``authorization`` is redacted
  rather than logged.
* **Loop-proof** — the endpoint itself never reports its own failures back to
  the client as an error the client would report again: it always answers 202.
"""

from __future__ import annotations

import logging

from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.accounts.authentication import CookieJWTAuthentication

from .observability import get_request_id, sanitize, scrub
from .throttles import ClientErrorThrottle

logger = logging.getLogger("nexora.client")

#: Hard ceilings. A browser has no business sending more than this.
MAX_EVENTS_PER_REQUEST = 10
MAX_MESSAGE_CHARS = 500
MAX_STACK_CHARS = 2000
MAX_URL_CHARS = 300

ALLOWED_KINDS = {
    "exception",
    "unhandledrejection",
    "api",
    "network",
    "timeout",
    "parse",
    "auth-refresh",
    "upload",
    "media",
    "websocket",
    "service-worker",
    "push",
    "init",
    "other",
}

LEVELS = {"warning": logging.WARNING, "error": logging.ERROR, "critical": logging.CRITICAL}


class _OptionalCookieJWT(CookieJWTAuthentication):
    """Associate the report with a user when possible; never refuse it.

    A frontend error very often happens *because* the session broke, so an
    invalid or missing cookie must not stop the report from being recorded.
    """

    def authenticate(self, request):
        try:
            return super().authenticate(request)
        except Exception:  # noqa: BLE001 - reporting must survive a bad session
            return None


def _normalize(event, index: int) -> dict | None:
    if not isinstance(event, dict):
        return None
    kind = str(event.get("kind") or "other").lower()
    if kind not in ALLOWED_KINDS:
        kind = "other"
    message = sanitize(event.get("message") or "(no message)", limit=MAX_MESSAGE_CHARS)
    if message == "(no message)" and not event.get("stack"):
        return None
    return {
        "index": index,
        "kind": kind,
        "level": str(event.get("level") or "error").lower(),
        "message": message,
        "source": sanitize(event.get("source") or "-", limit=120),
        "page": sanitize(event.get("page") or "-", limit=MAX_URL_CHARS),
        "stack": sanitize(event.get("stack") or "", limit=MAX_STACK_CHARS),
        "status": event.get("status") if isinstance(event.get("status"), int) else None,
        "code": sanitize(event.get("code") or "-", limit=60),
        "operation": sanitize(event.get("operation") or "-", limit=120),
        # The id the frontend attached to the failing backend request, which is
        # what ties this entry to the server-side one.
        "correlates": sanitize(event.get("request_id") or "-", limit=64),
        "context": scrub(event.get("context") or {}),
    }


@api_view(["POST"])
@authentication_classes([_OptionalCookieJWT])
@permission_classes([AllowAny])
@throttle_classes([ClientErrorThrottle])
def report(request):
    """Accept a small batch of frontend error reports and log them."""
    payload = request.data if isinstance(request.data, dict) else {}
    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        raw_events = [payload]

    user = getattr(request, "user", None)
    user_id = str(getattr(user, "id", "")) if getattr(user, "is_authenticated", False) else "anonymous"
    agent = sanitize(request.headers.get("User-Agent", "-"), limit=200)
    accepted = 0

    for index, raw in enumerate(raw_events[:MAX_EVENTS_PER_REQUEST]):
        event = _normalize(raw, index)
        if event is None:
            continue
        accepted += 1
        level = LEVELS.get(event["level"], logging.ERROR)
        logger.log(
            level,
            "FRONTEND %s | %s | page=%s source=%s op=%s status=%s code=%s "
            "correlates=%s agent=%s context=%s%s",
            event["kind"],
            event["message"],
            event["page"],
            event["source"],
            event["operation"],
            event["status"],
            event["code"],
            event["correlates"],
            agent,
            event["context"],
            f"\n{event['stack']}" if event["stack"] else "",
            extra={"user_id": user_id},
        )

    # 202 with the server-side request id: the frontend prints it to the
    # console so a user can read it out to support.
    return Response(
        {
            "success": True,
            "message": "Reported",
            "data": {"accepted": accepted, "request_id": get_request_id()},
        },
        status=202,
    )
