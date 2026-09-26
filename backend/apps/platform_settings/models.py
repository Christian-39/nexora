"""
NEXORA — organization (database) configuration.

This is the administrator-editable half of the configuration split. It holds
branding, policies, messaging/media limits, group rules and notification
preferences. It must NEVER hold infrastructure secrets: those live in the
environment and are read once in ``config.settings`` (see ``config/env.py``).

Every value here is expected to change backend behaviour, not merely the UI —
see ``apps.platform_settings.services.messaging_policy`` for the cached
accessor used by the messaging, media and group layers.
"""

from django.core.validators import RegexValidator
from django.db import models

from apps.core.models import TimeStampedModel

color = RegexValidator(r"^#[0-9A-Fa-f]{6}$", "Use #RRGGBB.")


class PlatformConfiguration(TimeStampedModel):
    singleton = models.PositiveSmallIntegerField(default=1, unique=True, editable=False)

    # --- identity / branding -------------------------------------------------
    organization_name = models.CharField(max_length=160)
    app_name = models.CharField(max_length=80, default="NEXORA")
    short_app_name = models.CharField(max_length=20, default="NEXORA")
    phone = models.CharField(max_length=18, blank=True)
    address = models.TextField(blank=True)
    contact_email = models.EmailField(blank=True)
    website = models.URLField(blank=True)
    support = models.CharField(max_length=200, blank=True)
    primary_color = models.CharField(max_length=7, validators=[color], default="#315EFB")
    secondary_color = models.CharField(max_length=7, validators=[color], default="#101828")

    # --- policy documents ----------------------------------------------------
    privacy_policy = models.TextField(blank=True)
    terms = models.TextField(blank=True)
    community_rules = models.TextField(blank=True)
    data_policy = models.TextField(blank=True)
    about = models.TextField(blank=True)

    # --- messaging limits ----------------------------------------------------
    max_message_length = models.PositiveIntegerField(default=5000)
    max_image_size_mb = models.PositiveSmallIntegerField(default=15)
    max_video_size_mb = models.PositiveSmallIntegerField(default=250)
    max_voice_size_mb = models.PositiveSmallIntegerField(default=25)
    max_voice_duration_seconds = models.PositiveIntegerField(default=600)
    max_video_duration_seconds = models.PositiveIntegerField(default=1800)
    message_edit_window_minutes = models.PositiveSmallIntegerField(default=15)
    message_delete_window_minutes = models.PositiveSmallIntegerField(default=15)

    # --- feature policy (enforced in the backend, mirrored to the UI) --------
    allow_delete_everyone = models.BooleanField(default=True)
    allow_member_name_edit = models.BooleanField(default=True)
    allow_member_avatar_edit = models.BooleanField(default=True)
    allow_replies = models.BooleanField(default=True)
    allow_reactions = models.BooleanField(default=True)
    allow_message_editing = models.BooleanField(default=True)
    allow_voice_notes = models.BooleanField(default=True)
    allow_video_messages = models.BooleanField(default=True)
    allow_image_messages = models.BooleanField(default=True)

    # --- group rules ---------------------------------------------------------
    default_members_can_send = models.BooleanField(default=True)
    default_members_can_send_media = models.BooleanField(default=True)
    default_members_can_send_voice = models.BooleanField(default=True)
    default_members_can_view_members = models.BooleanField(default=True)
    default_members_can_leave = models.BooleanField(default=False)

    # --- presence / notifications -------------------------------------------
    presence_enabled = models.BooleanField(default=True)
    typing_indicators_enabled = models.BooleanField(default=True)
    push_enabled = models.BooleanField(default=True)
    notification_previews = models.BooleanField(default=True)
    notification_aggregation_window_seconds = models.PositiveIntegerField(default=60)

    # --- security policy -----------------------------------------------------
    login_failure_limit = models.PositiveSmallIntegerField(default=5)
    lockout_minutes = models.PositiveSmallIntegerField(default=15)
    require_pin_change_on_first_login = models.BooleanField(default=True)
    session_idle_minutes = models.PositiveIntegerField(default=1440)
    login_rate_window_minutes = models.PositiveSmallIntegerField(default=15)
    pin_max_age_days = models.PositiveSmallIntegerField(default=0)

    def __str__(self) -> str:  # pragma: no cover - admin convenience
        return self.organization_name or "NEXORA deployment"


class BrandingAsset(TimeStampedModel):
    class Kind(models.TextChoices):
        LOGO = "LOGO"
        FAVICON = "FAVICON"

    kind = models.CharField(max_length=10, choices=Kind.choices, unique=True)
    storage_key = models.CharField(max_length=512, unique=True)
    mime_type = models.CharField(max_length=100)
    size = models.PositiveIntegerField()
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
