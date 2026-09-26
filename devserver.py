"""
NEXORA — combined development server (optional convenience).

Serves the static frontend and the Django ASGI application from ONE origin,
which mirrors the recommended production layout (a single reverse proxy in
front of both). Useful for demos, previews and same-origin testing.

    python devserver.py            # http://0.0.0.0:5500

You do not need this for normal development: run Django on :8000 and any
static server on :5500 and the frontend's config.js will find the API by
itself. Nothing in the application depends on this file.
"""

from __future__ import annotations

import mimetypes
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from config.asgi import application as django_app  # noqa: E402

API_PREFIXES = ("/api/", "/ws/", "/health/", "/static/", "/admin/")
INDEX = "index.html"


def _resolve(path: str) -> Path | None:
    """Map a URL path to a file inside the frontend directory, safely."""
    relative = path.lstrip("/") or INDEX
    candidate = (FRONTEND / relative).resolve()
    try:
        candidate.relative_to(FRONTEND.resolve())
    except ValueError:  # path traversal attempt
        return None
    if candidate.is_dir():
        candidate = candidate / INDEX
    return candidate if candidate.is_file() else None


async def application(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    path = scope.get("path", "/")
    if scope["type"] == "websocket" or path.startswith(API_PREFIXES):
        await django_app(scope, receive, send)
        return

    target = _resolve(path)
    if target is None:
        target = FRONTEND / "404.html"
        status = 404
    else:
        status = 200

    body = target.read_bytes()
    content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if target.suffix == ".webmanifest":
        content_type = "application/manifest+json"
    if target.suffix == ".js":
        content_type = "text/javascript"

    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", content_type.encode()),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-cache"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(application, host="0.0.0.0", port=int(os.environ.get("PORT", 5500)))
