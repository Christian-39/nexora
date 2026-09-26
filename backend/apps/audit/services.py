"""Administrative audit trail.

Records *who* did *what* to *which object*. Credentials, PINs, tokens and any
other secret material are stripped before a row is written.
"""

from .models import AuditLog

FORBIDDEN_KEYS = {
    "pin",
    "new_pin",
    "current_pin",
    "password",
    "token",
    "access",
    "refresh",
    "secret",
    "authorization",
    "secret_key",
    "storage_secret_key",
    "vapid_private_key",
}


def _scrub(metadata):
    clean = {}
    for key, value in (metadata or {}).items():
        lowered = str(key).lower()
        if lowered in FORBIDDEN_KEYS or any(word in lowered for word in ("pin", "password", "token", "secret")):
            continue
        clean[key] = value
    return clean


def record(actor, action, obj, request=None, metadata=None):
    from apps.security.services import client_ip

    return AuditLog.objects.create(
        actor=actor,
        action=action,
        object_type=obj.__class__.__name__,
        object_id=str(obj.pk),
        ip_address=client_ip(request) if request else None,
        metadata=_scrub(metadata),
    )
