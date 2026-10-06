"""
NEXORA — fault-tolerant cache access.

Why this module exists
----------------------
``django.core.cache`` is backed by the managed Redis instance in production.
Redis is used for three *very* different things here:

1. the channel layer (realtime) — genuinely required;
2. presence flags and the organization policy — pure optimisations;
3. DRF throttle counters — a safety mechanism.

Before this module every one of those call sites talked to ``cache`` directly,
so a single Redis hiccup (connection refused, TLS handshake failure, timeout,
provider restart, connection-limit exhaustion) raised out of a serializer or a
throttle and became an **HTTP 500**. Reproduced locally: with Redis
unreachable, ``GET /api/auth/csrf/`` and ``POST /api/auth/login/`` both return
500 — which is exactly the "The sign-in service is temporarily unavailable."
and "Unable to load members." the operators were seeing.

Presence, policy caching and throttling must degrade, not detonate. Every
failure is logged once (with the exception type and, at DEBUG, the traceback)
to ``nexora.cache`` so the outage is visible in Render instead of silent.
"""

from __future__ import annotations

import logging
import time

from django.core.cache import cache

from .observability import sanitize

logger = logging.getLogger("nexora.cache")

#: Exceptions a cache backend may raise. Redis raises its own hierarchy and
#: ``ConnectionError``/``OSError`` escape from the socket layer; a broken
#: pickle surfaces as ValueError. Anything else is a real bug and propagates.
CACHE_ERRORS: tuple[type[BaseException], ...] = (OSError, ConnectionError, TimeoutError, ValueError)

try:  # pragma: no cover - redis is always installed in this deployment
    import redis.exceptions as _redis_exc

    CACHE_ERRORS = (*CACHE_ERRORS, _redis_exc.RedisError)
except Exception:  # pragma: no cover
    pass


#: Collapse a storm of identical failures into one log line per interval, so a
#: Redis outage cannot flood Render with thousands of identical errors while
#: still making the outage impossible to miss.
_LOG_INTERVAL_SECONDS = 30.0
_last_logged: dict[str, float] = {}


def should_report(key: str, interval: float = _LOG_INTERVAL_SECONDS) -> bool:
    """True at most once per ``interval`` for a given key.

    Shared with ``apps.core.throttles`` so that cache *and* throttle failures
    are both collapsed during an outage instead of writing one record per
    request.
    """
    now = time.monotonic()
    previous = _last_logged.get(key, 0.0)
    if now - previous < interval:
        return False
    _last_logged[key] = now
    return True


def _report(operation: str, exc: BaseException) -> None:
    if not should_report(f"{operation}:{exc.__class__.__name__}"):
        return
    logger.error(
        "cache unavailable during %s: %s: %s (degrading gracefully; "
        "check REDIS_URL / the managed Redis instance)",
        operation,
        exc.__class__.__name__,
        sanitize(exc, limit=300),
        exc_info=logger.isEnabledFor(logging.DEBUG),
    )


def available() -> bool:
    """Cheap liveness probe used by the health endpoint."""
    try:
        cache.set("nexora:cache:probe", "1", 10)
        return cache.get("nexora:cache:probe") == "1"
    except CACHE_ERRORS as exc:
        _report("probe", exc)
        return False


def safe_get(key, default=None, *, operation: str = "get"):
    try:
        return cache.get(key, default)
    except CACHE_ERRORS as exc:
        _report(operation, exc)
        return default


def safe_get_many(keys, *, operation: str = "get_many") -> dict:
    try:
        return cache.get_many(list(keys))
    except CACHE_ERRORS as exc:
        _report(operation, exc)
        return {}


def safe_set(key, value, timeout=None, *, operation: str = "set") -> bool:
    try:
        cache.set(key, value, timeout)
        return True
    except CACHE_ERRORS as exc:
        _report(operation, exc)
        return False


def safe_add(key, value, timeout=None, *, operation: str = "add") -> bool:
    try:
        return bool(cache.add(key, value, timeout))
    except CACHE_ERRORS as exc:
        _report(operation, exc)
        return False


def safe_delete(key, *, operation: str = "delete") -> bool:
    try:
        cache.delete(key)
        return True
    except CACHE_ERRORS as exc:
        _report(operation, exc)
        return False


def safe_incr(key, delta: int = 1, *, operation: str = "incr"):
    try:
        return cache.incr(key, delta)
    except ValueError:
        # Documented Django behaviour when the key is absent — not an outage.
        return None
    except CACHE_ERRORS as exc:
        _report(operation, exc)
        return None


def safe_decr(key, delta: int = 1, *, operation: str = "decr"):
    try:
        return cache.decr(key, delta)
    except ValueError:
        return None
    except CACHE_ERRORS as exc:
        _report(operation, exc)
        return None


def safe_touch(key, timeout=None, *, operation: str = "touch") -> bool:
    try:
        return bool(cache.touch(key, timeout))
    except CACHE_ERRORS as exc:
        _report(operation, exc)
        return False
