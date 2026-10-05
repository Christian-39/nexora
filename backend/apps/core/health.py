"""
NEXORA — liveness and readiness probes.

``/health/live/``   the process is up (Render's cheapest check)
``/health/ready/``  the process can actually serve: database + cache + storage

The previous implementation swallowed every exception into a bare
``'failed'`` string, so an operator saw ``{"database": "failed"}`` with no way
to learn *why*. Each check now logs the underlying exception (type, message
and — for the database — the traceback) to the ``nexora.health`` logger, which
means the real cause is one line away in the Render log.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny

from .cache import CACHE_ERRORS
from .cache import available as cache_available
from .observability import get_request_id, sanitize

logger = logging.getLogger("nexora.health")


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def live(request):
    return JsonResponse({"success": True, "message": "alive", "data": {"status": "ok"}})


def _check_database() -> tuple[str, str]:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return "ok", ""
    except Exception as exc:  # noqa: BLE001 - a readiness probe reports everything
        logger.error(
            "readiness: database check failed: %s: %s",
            exc.__class__.__name__,
            sanitize(exc, limit=300),
            exc_info=exc,
        )
        return "failed", exc.__class__.__name__


def _check_cache() -> tuple[str, str]:
    try:
        return ("ok", "") if cache_available() else ("failed", "ProbeMismatch")
    except CACHE_ERRORS as exc:  # pragma: no cover - cache_available already guards
        logger.error("readiness: cache check failed: %s: %s", exc.__class__.__name__, sanitize(exc, limit=300))
        return "failed", exc.__class__.__name__


def _check_channels() -> tuple[str, str]:
    """Verify the Django Channels layer independently of the cache backend."""
    import asyncio
    import uuid

    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    layer = get_channel_layer()
    if layer is None:
        logger.error("readiness: channels check failed: channel layer not configured")
        return "failed", "NotConfigured"

    channel_name = f"healthcheck.{uuid.uuid4().hex}"

    async def _probe() -> bool:
        await asyncio.wait_for(layer.send(channel_name, {"type": "health.ping"}), timeout=3.0)
        msg = await asyncio.wait_for(layer.receive(channel_name), timeout=3.0)
        return isinstance(msg, dict) and msg.get("type") == "health.ping"

    try:
        ok = async_to_sync(_probe)()
        return ("ok", "") if ok else ("failed", "ProbeMismatch")
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "readiness: channels check failed: %s: %s",
            exc.__class__.__name__,
            sanitize(exc, limit=300),
            exc_info=logger.isEnabledFor(logging.DEBUG),
        )
        return "failed", exc.__class__.__name__


def _check_storage() -> tuple[str, str]:
    """Only meaningful when an object-storage bucket is configured."""
    if not getattr(settings, "STORAGE_BUCKET", ""):
        return "skipped", ""
    try:
        from django.core.files.storage import default_storage

        # ``exists`` on a key that will never exist is the cheapest round trip
        # that still proves credentials + bucket + network are all working.
        default_storage.exists("healthcheck/.probe")
        return "ok", ""
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "readiness: object storage check failed: %s: %s",
            exc.__class__.__name__,
            sanitize(exc, limit=300),
            exc_info=logger.isEnabledFor(logging.DEBUG),
        )
        return "failed", exc.__class__.__name__


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def ready(request):
    checks: dict[str, str] = {}
    reasons: dict[str, str] = {}
    for name, check in (
        ("database", _check_database),
        ("cache", _check_cache),
        ("channels", _check_channels),
        ("storage", _check_storage),
    ):
        state, reason = check()
        checks[name] = state
        if reason:
            reasons[name] = reason

    # Only the database is fatal for readiness: the application degrades
    # gracefully without Redis (see apps/core/cache.py) and object storage is
    # only needed for media, so a storage blip must not take the whole service
    # out of the Render load balancer.
    healthy = checks["database"] == "ok"
    degraded = sorted(k for k, v in checks.items() if v == "failed")
    overall_status = "healthy" if healthy and not degraded else ("degraded" if healthy else "unavailable")
    payload = {**checks, "status": overall_status, "degraded": degraded}
    if reasons:
        payload["reasons"] = reasons
    payload["request_id"] = get_request_id()

    return JsonResponse(
        {
            "success": healthy,
            "message": "ready" if healthy and not degraded else ("degraded" if healthy else "not ready"),
            "data": payload,
        },
        status=200 if healthy else 503,
    )
