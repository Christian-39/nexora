"""NEXORA — account serialization.

Field names follow the frontend contract (``display_name``, ``phone_visible``,
``must_change_pin`` …) while the database keeps its own names. The backend
remains the single source of truth for what a member may edit: the
``editable_fields`` list is computed from the organization policy, and PATCH
re-checks it — the list is a UX hint, never the control.
"""

from rest_framework import serializers

from apps.platform_settings.services import messaging_policy

from .models import User


def avatar_url_for(user, request=None):
    if not user.avatar_key:
        return None
    path = f"/api/members/{user.id}/avatar/"
    return request.build_absolute_uri(path) if request else path


class UserSerializer(serializers.ModelSerializer):
    """Administrative projection of a member (admin-only listings)."""

    display_name = serializers.CharField(source="full_name", read_only=True)
    avatar_url = serializers.SerializerMethodField()
    is_admin = serializers.SerializerMethodField()
    must_change_pin = serializers.SerializerMethodField()
    online = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "phone",
            "full_name",
            "display_name",
            "email",
            "role",
            "is_admin",
            "is_active",
            "credential_state",
            "must_change_pin",
            "avatar_url",
            "online",
            "last_login",
            "last_seen",
            "created_at",
        ]
        read_only_fields = fields

    def get_avatar_url(self, obj):
        return avatar_url_for(obj, self.context.get("request"))

    def get_is_admin(self, obj):
        return obj.role == User.Role.ADMIN

    def get_must_change_pin(self, obj):
        return obj.credential_state != User.Credential.CHANGED

    def get_online(self, obj):
        from django.core.cache import cache

        return bool(cache.get(f"presence:{obj.id}", 0))


class MemberCreateSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=150)
    display_name = serializers.CharField(max_length=150, required=False)
    phone = serializers.CharField(max_length=18)
    email = serializers.EmailField(required=False, allow_blank=True)

    def validate(self, attrs):
        if not attrs.get("full_name") and attrs.get("display_name"):
            attrs["full_name"] = attrs["display_name"]
        attrs.pop("display_name", None)
        return attrs


class MemberUpdateSerializer(serializers.ModelSerializer):
    display_name = serializers.CharField(source="full_name", required=False, max_length=150)

    class Meta:
        model = User
        fields = ["full_name", "display_name", "email"]


class PinSerializer(serializers.Serializer):
    current_pin = serializers.RegexField(r"^\d{6}$", error_messages={"invalid": "Enter your current six-digit PIN."})
    new_pin = serializers.RegexField(r"^\d{6}$", error_messages={"invalid": "The new PIN must be six digits."})

    def validate_new_pin(self, value):
        if value == self.initial_data.get("current_pin"):
            raise serializers.ValidationError("The new PIN must differ from the current one.")
        if value in {"000000", "111111", "123456", "654321", "999999"}:
            raise serializers.ValidationError("Choose a less predictable PIN.")
        if len(set(value)) == 1:
            raise serializers.ValidationError("Choose a less predictable PIN.")
        return value


class ProfileSerializer(serializers.ModelSerializer):
    """The ``/api/me/`` document."""

    display_name = serializers.CharField(source="full_name", max_length=150, required=False)
    phone_visible = serializers.BooleanField(source="show_phone", required=False)
    presence_visible = serializers.BooleanField(source="show_last_seen", required=False)
    avatar_url = serializers.SerializerMethodField()
    is_admin = serializers.SerializerMethodField()
    must_change_pin = serializers.SerializerMethodField()
    editable_fields = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "phone",
            "full_name",
            "display_name",
            "email",
            "role",
            "is_admin",
            "credential_state",
            "must_change_pin",
            "theme",
            "phone_visible",
            "presence_visible",
            "push_enabled",
            "avatar_url",
            "last_seen",
            "editable_fields",
        ]
        read_only_fields = ["id", "phone", "role", "credential_state", "last_seen", "email", "full_name"]

    def get_avatar_url(self, obj):
        return avatar_url_for(obj, self.context.get("request"))

    def get_is_admin(self, obj):
        return obj.role == User.Role.ADMIN

    def get_must_change_pin(self, obj):
        return obj.credential_state != User.Credential.CHANGED

    def get_editable_fields(self, obj):
        policy = messaging_policy()
        fields = ["phone_visible", "presence_visible", "theme"]
        if obj.role == User.Role.ADMIN or policy["allow_member_name_edit"]:
            fields.append("display_name")
        if obj.role == User.Role.ADMIN or policy["allow_member_avatar_edit"]:
            fields.append("avatar")
        return fields

    def validate(self, attrs):
        allowed = set(self.get_editable_fields(self.instance))
        if "full_name" in attrs and "display_name" not in allowed:
            raise serializers.ValidationError(
                {"display_name": "Display-name editing is disabled by the administrator."}
            )
        return attrs


class PreferencesSerializer(serializers.ModelSerializer):
    notification_previews = serializers.BooleanField(source="notification_preview", required=False)
    group_notifications = serializers.BooleanField(source="notify_groups", required=False)
    message_notifications = serializers.BooleanField(source="notify_messages", required=False)
    notification_sound = serializers.BooleanField(required=False)

    class Meta:
        model = User
        fields = [
            "theme",
            "push_enabled",
            "notification_previews",
            "notification_sound",
            "group_notifications",
            "message_notifications",
        ]

    #: Sound is a purely client-side preference; accepted and echoed back so
    #: the UI has one place to store it, but it has no backend effect.
    notification_sound_default = True

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["notification_sound"] = True
        return data

    def update(self, instance, validated_data):
        validated_data.pop("notification_sound", None)
        return super().update(instance, validated_data)
