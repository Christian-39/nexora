"""
NEXORA — rate limiting.

DRF's ``SimpleRateThrottle`` reads and writes its counters through
``django.core.cache``. In production that is the managed Redis instance, so a
Redis outage used to raise straight out of ``APIView.initial()`` and turn
*every* throttled endpoint — including ``/api/auth/csrf/`` and
``/api/auth/login/`` — into an HTTP 500.

Throttling is a protection mechanism, not a correctness mechanism: when the
counter store is unavailable the correct behaviour is to let the request
through and make the degradation loudly visible in the logs, not to take the
product offline. ``ResilientThrottleMixin`` implements exactly that.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import threading
import time

from django.conf import settings
from rest_framework.throttling import AnonRateThrottle, SimpleRateThrottle, UserRateThrottle

from .cache import CACHE_ERRORS, should_report
from .observability import sanitize

logger = logging.getLogger("nexora.throttle")

# Process-local sliding-window buckets used only while the shared counter store
# is unreachable. Scoped per worker process, so with WEB_CONCURRENCY=2 the
# effective limit is up to 2x the configured rate — far from perfect, but it
# keeps credential endpoints from becoming completely unlimited during an
# outage. Bounded so a flood of distinct keys cannot grow it without limit.
_LOCAL_BUCKETS: dict[str, list[float]] = {}
_LOCAL_LOCK = threading.Lock()
_LOCAL_MAX_KEYS = 5000
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _local_allow(key: str, num_requests: int, duration: int) -> bool:
    """Sliding-window check against the in-process fallback bucket."""
    now = time.monotonic()
    cutoff = now - duration
    with _LOCAL_LOCK:
        if len(_LOCAL_BUCKETS) > _LOCAL_MAX_KEYS:
            # Cheap pressure valve: drop windows that are entirely expired.
            for stale in [k for k, v in _LOCAL_BUCKETS.items() if not v or v[-1] < cutoff]:
                _LOCAL_BUCKETS.pop(stale, None)
            if len(_LOCAL_BUCKETS) > _LOCAL_MAX_KEYS:
                _LOCAL_BUCKETS.clear()
        hits = [t for t in _LOCAL_BUCKETS.get(key, ()) if t > cutoff]
        if len(hits) >= num_requests:
            _LOCAL_BUCKETS[key] = hits
            return False
        hits.append(now)
        _LOCAL_BUCKETS[key] = hits
        return True


class ResilientThrottleMixin:
    """Fail *open* (and log) when the throttle counter store is unavailable.

    Subclasses that guard credentials set ``local_fallback = True`` so that a
    Redis outage degrades to a weaker per-process limit instead of no limit at
    all.
    """

    #: Use the in-process sliding window when the shared store is unreachable.
    local_fallback = False

    def _report(self, exc, allowed: bool) -> None:
        scope = getattr(self, "scope", "?")
        # Collapsed to one line per scope per 30s: during an outage this path
        # runs on literally every request.
        if not should_report(f"throttle:{scope}:{exc.__class__.__name__}"):
            return
        logger.error(
            "throttle store unavailable for scope=%s (%s: %s) — %s; "
            "check REDIS_URL / the managed Redis instance",
            scope,
            exc.__class__.__name__,
            sanitize(exc, limit=300),
            "falling back to the per-process limiter"
            if self.local_fallback
            else ("request allowed without rate limiting" if allowed else "request blocked"),
            exc_info=logger.isEnabledFor(logging.DEBUG),
        )

    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except CACHE_ERRORS as exc:
            if self.local_fallback:
                # self.rate / num_requests / duration are populated in __init__,
                # so they are available even though the cache read failed.
                try:
                    key = self.get_cache_key(request, view) or getattr(self, "scope", "?")
                    allowed = _local_allow(str(key), self.num_requests, self.duration)
                except Exception:  # noqa: BLE001 - never let the limiter 500 the request
                    allowed = True
                self._report(exc, allowed)
                return allowed
            self._report(exc, True)
            return True

    def throttle_success(self):
        try:
            return super().throttle_success()
        except CACHE_ERRORS:  # pragma: no cover - already reported by allow_request
            return True

    def wait(self):
        try:
            return super().wait()
        except Exception:  # noqa: BLE001 - history may be absent after a cache failure
            return getattr(self, "duration", None)


class SafeAnonRateThrottle(ResilientThrottleMixin, AnonRateThrottle):
    """Use a hashed browser pseudonym as an anonymous throttle partition.

    ``X-Nexora-Client-ID`` is a public browser-supplied nonce, never a user
    identifier. It is validated against a tight regex, hashed with ``SECRET_KEY``,
    and used only as the Redis/local limiter key. Requests without it fall
    back to DRF's direct peer address (the Render reverse proxy address).
    """

    def get_ident(self, request):
        candidate = str(request.META.get("HTTP_X_NEXORA_CLIENT_ID") or "").strip()
        if _CLIENT_ID_RE.fullmatch(candidate):
            message = f"{self.scope}\0{candidate}".encode("utf-8")
            digest = hmac.new(str(settings.SECRET_KEY).encode("utf-8"), message, hashlib.sha256).hexdigest()
            return f"browser:{digest}"
        return super().get_ident(request)


class SafeUserRateThrottle(ResilientThrottleMixin, UserRateThrottle):
    pass


class SafeSimpleRateThrottle(ResilientThrottleMixin, SimpleRateThrottle):
    pass


class MessageThrottle(SafeUserRateThrottle):
    scope = "messages"


class SearchThrottle(SafeUserRateThrottle):
    scope = "search"


class UploadThrottle(SafeUserRateThrottle):
    scope = "uploads"


class PushThrottle(SafeUserRateThrottle):
    scope = "push"


class CredentialThrottle(SafeUserRateThrottle):
    """PIN changes / resets. Keeps limiting (locally) even if Redis is down."""

    scope = "credentials"
    local_fallback = True


class LoginThrottle(SafeAnonRateThrottle):
    """Brute-force protection for ``/api/auth/login/``.

    Declared here rather than in ``apps.accounts.views`` so the login endpoint
    cannot drift back onto a stock DRF throttle that 500s when Redis blinks.
    """

    scope = "login"
    local_fallback = True


class ClientErrorThrottle(SafeAnonRateThrottle):
    """Protects the frontend error-reporting endpoint from log flooding."""

    scope = "client_errors"
