"""
NEXORA — ASGI entry point (this is what production must run).

    gunicorn config.asgi:application -k uvicorn.workers.UvicornWorker

``config.wsgi`` exists only for management tooling that insists on WSGI; it
cannot serve WebSockets. The protocol router below is the single place where
HTTP and WebSocket traffic is separated:

    websocket → OriginAllowlist → JWTAuthMiddleware → URLRouter → consumers
    http      → the ordinary Django application

Why the origin allowlist: the same-origin policy does NOT apply to WebSocket
handshakes. With cross-site cookies (SameSite=None, required when the frontend
is hosted on another origin) any website could otherwise open an authenticated
socket for a signed-in visitor. Only the configured frontend origins may
connect; requests with no Origin header at all are non-browser clients
(tests, server-to-server) and are passed through to authentication, which is
still the real authorization boundary.
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from django.core.asgi import get_asgi_application  # noqa: E402

django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from django.conf import settings  # noqa: E402

from apps.accounts.ws_auth import JWTAuthMiddleware  # noqa: E402
from apps.conversations.routing import websocket_urlpatterns  # noqa: E402
from apps.security.relay import SecurityRelayBoundary  # noqa: E402


class OriginAllowlist:
    """Reject WebSocket handshakes from origins the deployment does not know."""

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _allowed() -> list[str]:
        return [str(origin).rstrip("/") for origin in getattr(settings, "WEBSOCKET_ALLOWED_ORIGINS", [])]

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "websocket":
            return await self.app(scope, receive, send)

        allowed = self._allowed()
        if not allowed:
            # No frontend origin configured at all (development/tests). The
            # production guard rails make this impossible with DEBUG=False.
            return await self.app(scope, receive, send)

        origin = ""
        for name, value in scope.get("headers", []):
            if name == b"origin":
                origin = value.decode("latin-1").strip().rstrip("/")
                break

        # No Origin header → not a browser → authentication decides.
        if origin and origin not in allowed:
            await send({"type": "websocket.close", "code": 4403})
            return

        return await self.app(scope, receive, send)


application = SecurityRelayBoundary(
    ProtocolTypeRouter(
        {
            "http": django_asgi_app,
            "websocket": OriginAllowlist(JWTAuthMiddleware(URLRouter(websocket_urlpatterns))),
        }
    )
)
