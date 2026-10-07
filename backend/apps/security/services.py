"""Privacy-conscious request identity helpers for security/audit events."""

from __future__ import annotations

import hashlib

from django.conf import settings

from .models import SecurityEvent


def client_ip(request):
    """Return the browser's public IP as observed by the Render reverse proxy.

    Render terminates TLS and forwards every request to the backend with an
    ``X-Forwarded-For`` header that contains the connecting peer's address (the
    leftmost entry is the original client). ``REMOTE_ADDR`` on the backend is
    Render's own proxy address and is not useful for anomaly detection.

    Forwarded headers are attacker-controllable when the upstream is not
    authenticated; here the only upstream is Render's reverse proxy and the
    deployment does not expose any other route in front of the ASGI workers.
    """
    if request is None:
        return None
    forwarded = str(request.META.get("HTTP_X_FORWARDED_FOR") or "").strip()
    if forwarded:
        # ``X-Forwarded-For`` is a comma-separated list; the leftmost entry
        # is the original client and subsequent entries are intermediate
        # proxies. Take only the first hop and ignore empty fragments.
        first = forwarded.split(",", 1)[0].strip()
        if first:
            return first
    remote = str(request.META.get("REMOTE_ADDR") or "").strip()
    return remote or None


def ip_hash(request):
    value = client_ip(request) or ""
    return hashlib.sha256(value.encode()).hexdigest() if value else ""


def event(name, request=None, user=None, metadata=None):
    """Record a security event. Secrets are stripped before persistence."""
    from apps.audit.services import _scrub

    safe = _scrub(metadata)
    return SecurityEvent.objects.create(
        user=user,
        event=name,
        ip_address=client_ip(request) if request else None,
        metadata=safe,
    )