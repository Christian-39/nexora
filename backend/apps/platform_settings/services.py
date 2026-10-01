"""Cached accessors for the organization's database configuration.

``messaging_policy()`` is consulted by the messaging, media, group and
notification layers on every relevant operation — including several times per
serialized message (can_edit / can_delete / can_react) — so it is cached at
two levels:

* a per-process micro-cache (a few seconds) so a page of messages does not
  cost one Redis round trip per policy field;
* a shared Redis cache (60s) so the database is not queried per request.

``invalidate()`` (called on every administrator save) drops both levels, so a
policy change becomes effective immediately on the saving process and within
seconds everywhere else. Nothing here ever caches private/user data.
"""

import time

from apps.core.cache import safe_delete, safe_get, safe_set

from .models import PlatformConfiguration

CACHE_KEY = "platform:messaging_policy"
CACHE_TTL = 60
#: Upper bound on how long another worker may serve a superseded policy.
LOCAL_CACHE_TTL = 5.0

_local = {"at": 0.0, "value": None}

DEFAULTS = {
    "max_message_length": 5000,
    "max_image_size_mb": 15,
    "max_video_size_mb": 250,
    "max_voice_size_mb": 25,
    "max_voice_duration_seconds": 600,
    "max_video_duration_seconds": 1800,
    "message_edit_window_minutes": 15,
    "message_delete_window_minutes": 15,
    "allow_delete_everyone": True,
    "allow_member_name_edit": True,
    "allow_member_avatar_edit": True,
    "allow_replies": True,
    "allow_reactions": True,
    "allow_message_editing": True,
    "allow_voice_notes": True,
    "allow_video_messages": True,
    "allow_image_messages": True,
    "default_members_can_send": True,
    "default_members_can_send_media": True,
    "default_members_can_send_voice": True,
    "default_members_can_view_members": True,
    "default_members_can_leave": False,
    "presence_enabled": True,
    "typing_indicators_enabled": True,
    "push_enabled": True,
    "notification_previews": True,
    "notification_aggregation_window_seconds": 60,
    "login_failure_limit": 5,
    "lockout_minutes": 15,
    "require_pin_change_on_first_login": True,
    "session_idle_minutes": 1440,
    "login_rate_window_minutes": 15,
    "pin_max_age_days": 0,
}


def configuration() -> PlatformConfiguration | None:
    return PlatformConfiguration.objects.first()


def messaging_policy() -> dict:
    now = time.monotonic()
    if _local["value"] is not None and now - _local["at"] < LOCAL_CACHE_TTL:
        return _local["value"]
    # A Redis outage must degrade to "read the row", never to HTTP 500:
    # messaging_policy() is on the login path and on every media upload.
    cached = safe_get(CACHE_KEY, operation="messaging_policy")
    if cached is not None:
        _local["at"], _local["value"] = now, cached
        return cached
    row = configuration()
    value = {key: getattr(row, key, default) if row else default for key, default in DEFAULTS.items()}
    safe_set(CACHE_KEY, value, CACHE_TTL, operation="messaging_policy")
    _local["at"], _local["value"] = now, value
    return value


def invalidate() -> None:
    _local["at"], _local["value"] = 0.0, None
    safe_delete(CACHE_KEY, operation="policy_invalidate")
    safe_delete("platform:public_config", operation="policy_invalidate")
