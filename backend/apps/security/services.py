"""Privacy-conscious request identity helpers for security/audit events."""

from __future__ import annotations

import hashlib

from django.conf import settings

from .models import SecurityEvent


def client_ip(request):
    """Return only a directly observed peer address in non-relay deployments.

    Forwarded headers are attacker-controlled unless a trusted proxy allowlist
    is established. The production relay deliberately strips client IP headers,
    so production audit/security rows store no user or internal relay IP.
    """
    if settings.SECURITY_RELAY_REQUIRED:
        return None
    value = str(request.META.get("REMOTE_ADDR") or "").strip()
    return value or None


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
