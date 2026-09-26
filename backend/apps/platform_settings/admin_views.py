"""
NEXORA — administrator settings.

These endpoints write the DATABASE half of the configuration split. They can
never touch environment/infrastructure configuration: there is no code path
from here to SECRET_KEY, database credentials, Redis, storage credentials or
the VAPID private key, and no such value is ever returned.

The payload is grouped (``organization``/``branding``/``messaging``) to match
the admin UI, and flat keys are accepted too so either shape works.
"""

from __future__ import annotations

from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import record

from .models import PlatformConfiguration
from .serializers import PlatformSerializer
from .services import invalidate

#: request-key -> model-field. Anything not listed here is ignored outright.
FIELD_MAP = {
    "organization_name": "organization_name",
    "contact_phone": "phone",
    "phone": "phone",
    "contact_email": "contact_email",
    "website": "website",
    "address": "address",
    "about": "about",
    "support": "support",
    "app_name": "app_name",
    "app_short_name": "short_app_name",
    "short_app_name": "short_app_name",
    "primary_color": "primary_color",
    "secondary_color": "secondary_color",
    "max_message_length": "max_message_length",
    "max_voice_duration": "max_voice_duration_seconds",
    "max_voice_duration_seconds": "max_voice_duration_seconds",
    "max_video_duration_seconds": "max_video_duration_seconds",
    "delete_for_everyone": "allow_delete_everyone",
    "allow_delete_everyone": "allow_delete_everyone",
    "message_editing": "allow_message_editing",
    "allow_message_editing": "allow_message_editing",
    "edit_window_minutes": "message_edit_window_minutes",
    "message_edit_window_minutes": "message_edit_window_minutes",
    "delete_window_minutes": "message_delete_window_minutes",
    "message_delete_window_minutes": "message_delete_window_minutes",
    "reactions": "allow_reactions",
    "allow_reactions": "allow_reactions",
    "replies": "allow_replies",
    "allow_replies": "allow_replies",
    "voice_notes": "allow_voice_notes",
    "allow_voice_notes": "allow_voice_notes",
    "video_messages": "allow_video_messages",
    "allow_video_messages": "allow_video_messages",
    "image_messages": "allow_image_messages",
    "allow_image_messages": "allow_image_messages",
    "member_leave_group": "default_members_can_leave",
    "member_name_edit": "allow_member_name_edit",
    "allow_member_name_edit": "allow_member_name_edit",
    "allow_member_avatar_edit": "allow_member_avatar_edit",
    "presence_enabled": "presence_enabled",
    "typing_indicators_enabled": "typing_indicators_enabled",
    "push_enabled": "push_enabled",
    "notification_previews": "notification_previews",
    "notification_aggregation_window_seconds": "notification_aggregation_window_seconds",
    "privacy_policy": "privacy_policy",
    "terms": "terms",
    "community_rules": "community_rules",
    "data_policy": "data_policy",
}

#: Sizes arrive from the UI in bytes and are stored in whole megabytes.
BYTE_FIELDS = {
    "max_image_size": "max_image_size_mb",
    "max_video_size": "max_video_size_mb",
    "max_voice_size": "max_voice_size_mb",
}

GROUPS = ("organization", "branding", "messaging", "notifications", "groups")


def flatten(payload: dict) -> dict:
    """Accept both grouped and flat request bodies."""
    flat = {}
    for key, value in payload.items():
        if key in GROUPS and isinstance(value, dict):
            flat.update(value)
        else:
            flat[key] = value
    return flat


def to_model_fields(payload: dict) -> dict:
    flat = flatten(payload)
    data = {}
    for key, value in flat.items():
        if key in BYTE_FIELDS:
            try:
                data[BYTE_FIELDS[key]] = max(1, round(int(value) / 1024**2))
            except (TypeError, ValueError):
                raise ValidationError({key: "Provide the size in bytes."}) from None
        elif key in FIELD_MAP:
            data[FIELD_MAP[key]] = value
    return data


def grouped_representation(row, request) -> dict:
    from .views import build_public_config

    public = build_public_config(request)
    return {
        "organization": {
            "organization_name": row.organization_name,
            "contact_phone": row.phone,
            "contact_email": row.contact_email,
            "website": row.website,
            "address": row.address,
            "about": row.about,
            "support": row.support,
        },
        "branding": {
            "app_name": row.app_name,
            "app_short_name": row.short_app_name,
            "primary_color": row.primary_color,
            "secondary_color": row.secondary_color,
            "logo_url": public["logo_url"],
            "favicon_url": public["favicon_url"],
        },
        "messaging": {
            "max_message_length": row.max_message_length,
            "max_image_size": row.max_image_size_mb * 1024**2,
            "max_video_size": row.max_video_size_mb * 1024**2,
            "max_voice_size": row.max_voice_size_mb * 1024**2,
            "max_voice_duration": row.max_voice_duration_seconds,
            "delete_for_everyone": row.allow_delete_everyone,
            "message_editing": row.allow_message_editing,
            "edit_window_minutes": row.message_edit_window_minutes,
            "delete_window_minutes": row.message_delete_window_minutes,
            "reactions": row.allow_reactions,
            "replies": row.allow_replies,
            "voice_notes": row.allow_voice_notes,
            "video_messages": row.allow_video_messages,
            "member_leave_group": row.default_members_can_leave,
        },
        "notifications": {
            "push_enabled": row.push_enabled,
            "notification_previews": row.notification_previews,
            "aggregation_window_seconds": row.notification_aggregation_window_seconds,
        },
        # Flat mirror so simple consumers need no unwrapping.
        **{k: getattr(row, k) for k in ("organization_name", "app_name", "primary_color", "secondary_color")},
    }


def ensure_configuration(default_name="Organization") -> PlatformConfiguration:
    row, _ = PlatformConfiguration.objects.get_or_create(
        singleton=1, defaults={"organization_name": default_name}
    )
    return row


class PlatformSettingsView(APIView):
    def _require_admin(self, request):
        if request.user.role != "ADMIN":
            raise PermissionDenied()

    def get(self, request):
        self._require_admin(request)
        row = ensure_configuration()
        return Response(
            {"success": True, "message": "Settings retrieved", "data": grouped_representation(row, request)}
        )

    @transaction.atomic
    def patch(self, request):
        self._require_admin(request)
        row = ensure_configuration(str(request.data.get("organization_name") or "Organization"))
        data = to_model_fields(request.data)
        if not data:
            raise ValidationError("No recognised settings were supplied.")
        serializer = PlatformSerializer(row, data=data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        invalidate()
        record(
            request.user,
            "PLATFORM_SETTINGS_UPDATED",
            row,
            request,
            {"fields": sorted(serializer.validated_data)},
        )
        row.refresh_from_db()
        return Response(
            {"success": True, "message": "Settings updated", "data": grouped_representation(row, request)}
        )


POLICY_FIELDS = {
    "privacy": ("privacy_policy", "Privacy Policy"),
    "terms": ("terms", "Terms of Use"),
    "community": ("community_rules", "Community Rules"),
    "data": ("data_policy", "Data Policy"),
}


class PolicyView(APIView):
    """Policy documents. Readable by any signed-in user, writable by admins."""

    def get(self, request):
        row = ensure_configuration()
        return Response(
            {
                "success": True,
                "message": "Policies retrieved",
                "data": {
                    "policies": [
                        {"key": key, "title": title, "body": getattr(row, field) or ""}
                        for key, (field, title) in POLICY_FIELDS.items()
                    ]
                },
            }
        )

    @transaction.atomic
    def patch(self, request):
        if request.user.role != "ADMIN":
            raise PermissionDenied()
        row = ensure_configuration()
        entries = request.data.get("policies")
        if not isinstance(entries, list):
            raise ValidationError({"policies": "Provide a list of policy documents."})
        changed = []
        for entry in entries:
            key = str((entry or {}).get("key", ""))
            if key not in POLICY_FIELDS:
                continue
            field = POLICY_FIELDS[key][0]
            body = str(entry.get("body", ""))[:20000]
            setattr(row, field, body)
            changed.append(field)
        if not changed:
            raise ValidationError({"policies": "No recognised policy keys were supplied."})
        row.save(update_fields=[*changed, "updated_at"])
        invalidate()
        record(request.user, "PLATFORM_POLICIES_UPDATED", row, request, {"fields": sorted(changed)})
        return self.get(request)
