"""
NEXORA — public configuration.

``GET /api/public/config/`` is the only unauthenticated configuration surface.
It exposes organization branding, public policy text, the backend-enforced
limits/feature flags the UI must mirror, and the VAPID *public* key.

It must never expose SECRET_KEY, database credentials, Redis credentials,
storage credentials, the VAPID private key or any other infrastructure secret.
The payload is assembled from an explicit allow-list for exactly that reason.
"""

from django.conf import settings
from django.core.cache import cache
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.media.validators import AUDIO_TYPES, IMAGE_TYPES, VIDEO_TYPES

from .models import BrandingAsset, PlatformConfiguration
from .services import messaging_policy

#: Explicit allow-list. Nothing outside this list can ever leak.
PUBLIC_FIELDS = (
    "organization_name",
    "app_name",
    "address",
    "website",
    "support",
    "primary_color",
    "secondary_color",
    "privacy_policy",
    "terms",
    "community_rules",
    "about",
)

CACHE_KEY = "platform:public_config"


def build_public_config(request) -> dict:
    row: PlatformConfiguration | None = PlatformConfiguration.objects.first()
    policy = messaging_policy()

    data = {field: getattr(row, field, "") if row else "" for field in PUBLIC_FIELDS}
    data["app_short_name"] = getattr(row, "short_app_name", "") if row else ""
    data["contact_phone"] = getattr(row, "phone", "") if row else ""
    data["contact_email"] = getattr(row, "contact_email", "") if row else ""

    assets = set(BrandingAsset.objects.values_list("kind", flat=True))
    data["logo_url"] = request.build_absolute_uri("/api/public/branding/logo/") if "LOGO" in assets else None
    data["favicon_url"] = (
        request.build_absolute_uri("/api/public/branding/favicon/") if "FAVICON" in assets else None
    )

    brand = data["app_name"] or "NEXORA"
    data["pwa"] = {
        "name": data["organization_name"] or brand,
        "short_name": data["app_short_name"] or brand[:12],
        "theme_color": data["primary_color"] or "#315EFB",
        "background_color": data["secondary_color"] or "#101828",
        "icons": [
            {"src": data["logo_url"], "sizes": "any", "type": "image/png"}
        ]
        if data["logo_url"]
        else [],
    }

    data["limits"] = {
        "max_message_length": policy["max_message_length"],
        "max_image_size": policy["max_image_size_mb"] * 1024**2,
        "max_video_size": policy["max_video_size_mb"] * 1024**2,
        "max_voice_size": policy["max_voice_size_mb"] * 1024**2,
        "max_voice_duration": policy["max_voice_duration_seconds"],
        "max_video_duration": policy["max_video_duration_seconds"],
        "message_edit_window_minutes": policy["message_edit_window_minutes"],
        "message_delete_window_minutes": policy["message_delete_window_minutes"],
        "allowed_image_types": sorted(IMAGE_TYPES) if policy["allow_image_messages"] else [],
        "allowed_video_types": sorted(VIDEO_TYPES) if policy["allow_video_messages"] else [],
        "allowed_audio_types": sorted(AUDIO_TYPES) if policy["allow_voice_notes"] else [],
    }

    data["features"] = {
        "replies": policy["allow_replies"],
        "reactions": policy["allow_reactions"],
        "message_editing": policy["allow_message_editing"],
        "delete_for_everyone": policy["allow_delete_everyone"],
        "voice_notes": policy["allow_voice_notes"],
        "video_messages": policy["allow_video_messages"],
        "image_messages": policy["allow_image_messages"],
        "presence": policy["presence_enabled"],
        "typing": policy["typing_indicators_enabled"],
        "push": policy["push_enabled"] and bool(settings.PUSH_PUBLIC_KEY),
        "member_leave_group": policy["default_members_can_leave"],
        "member_name_edit": policy["allow_member_name_edit"],
        "member_avatar_edit": policy["allow_member_avatar_edit"],
    }

    # Published policy documents (plain text, rendered as text by the UI).
    data["policies"] = [
        {"key": key, "title": title, "body": getattr(row, field, "") or ""}
        for key, field, title in (
            ("privacy", "privacy_policy", "Privacy Policy"),
            ("terms", "terms", "Terms of Use"),
            ("community", "community_rules", "Community Rules"),
            ("data", "data_policy", "Data Policy"),
        )
        if row and (getattr(row, field, "") or "")
    ]

    # Public VAPID key only. The private key never leaves the backend.
    data["push"] = {"vapid_public_key": settings.PUSH_PUBLIC_KEY or None}
    data["vapid_public_key"] = settings.PUSH_PUBLIC_KEY or None
    return data


@api_view(["GET"])
@permission_classes([AllowAny])
def public_config(request):
    cached = cache.get(CACHE_KEY)
    if cached is None:
        cached = build_public_config(request)
        cache.set(CACHE_KEY, cached, 60)
    return Response({"success": True, "message": "Configuration retrieved", "data": cached})
