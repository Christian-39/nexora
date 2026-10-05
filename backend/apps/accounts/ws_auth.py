"""
NEXORA — WebSocket authentication middleware.

The browser cannot set an Authorization header on a WebSocket, so the socket is
authenticated exactly like the REST API: with the HttpOnly access cookie the
handshake carries. Nothing is ever read from the query string, so no credential
can leak into an access log or a Referer.

The middleware never rejects the connection itself — it records the outcome in
the scope and lets the consumer answer, so the client receives a typed
``auth.error`` frame instead of an anonymous transport failure (a rejected
handshake reaches the browser as close code 1006, which is indistinguishable
from a network outage and is what makes clients reconnect forever).

Scope keys set here:
    ``user``        the authenticated user, or AnonymousUser
    ``token_exp``   unix expiry of the access token, or None
    ``auth_error``  None | 'NO_CREDENTIAL' | 'INVALID_CREDENTIAL' | 'SESSION_REVOKED'
"""

import logging

from channels.db import database_sync_to_async
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import DatabaseError
from django.utils import timezone
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import AccessToken

logger = logging.getLogger("nexora.realtime")


def _cookies_from(scope) -> dict[str, str]:
    headers = dict(scope.get("headers", []))
    raw = headers.get(b"cookie", b"").decode("latin-1")
    cookies: dict[str, str] = {}
    for item in raw.split(";"):
        if "=" in item:
            key, value = item.strip().split("=", 1)
            cookies[key] = value
    return cookies


@database_sync_to_async
def auth_for(token: str):
    """Resolve an access token to (user, expiry, error_code)."""
    from .models import DeviceSession, User

    if not token:
        return AnonymousUser(), None, "NO_CREDENTIAL"
    try:
        parsed = AccessToken(token)
        user_id = parsed["user_id"]
    except (TokenError, KeyError, TypeError, ValueError):
        return AnonymousUser(), None, "INVALID_CREDENTIAL"

    try:
        user = User.objects.get(id=user_id, is_active=True)
        # The device session must still exist and not have been revoked: signing
        # out on one device must kill that device's sockets everywhere.
        session_alive = DeviceSession.objects.filter(
            id=parsed.get("sid"),
            user=user,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).exists()
    except (User.DoesNotExist, DjangoValidationError, ValueError, TypeError):
        return AnonymousUser(), None, "INVALID_CREDENTIAL"
    except DatabaseError as exc:
        logger.error(
            "ws.auth database error (%s)",
            exc.__class__.__name__,
            extra={
                "event": "ws.auth.db_error",
                "error_type": exc.__class__.__name__,
            },
            exc_info=exc,
        )
        return AnonymousUser(), None, "SERVICE_UNAVAILABLE"
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "ws.auth unexpected error (%s)",
            exc.__class__.__name__,
            extra={
                "event": "ws.auth.error",
                "error_type": exc.__class__.__name__,
            },
        )
        return AnonymousUser(), None, "SERVICE_UNAVAILABLE"

    if not session_alive:
        return AnonymousUser(), None, "SESSION_REVOKED"

    return user, int(parsed["exp"]), None


class JWTAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        cookies = _cookies_from(scope)
        user, expiry, error = await auth_for(cookies.get(settings.ACCESS_COOKIE, ""))
        scope["user"] = user
        scope["token_exp"] = expiry
        scope["auth_error"] = error
        return await self.app(scope, receive, send)
