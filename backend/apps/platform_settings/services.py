"""Cached accessors for the organization's database configuration.

``messaging_policy()`` is consulted by the messaging, media, group and
notification layers on every relevant operation, so it is cached briefly and
invalidated whenever the administrator saves settings.
"""

from django.core.cache import cache

from .models import PlatformConfiguration

CACHE_KEY = "platform:messaging_policy"
CACHE_TTL = 60

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
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached
    row = configuration()
    value = {key: getattr(row, key, default) if row else default for key, default in DEFAULTS.items()}
    cache.set(CACHE_KEY, value, CACHE_TTL)
    return value


def invalidate() -> None:
    cache.delete(CACHE_KEY)
    cache.delete("platform:public_config")
