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


def presence_online_map(user_ids) -> dict:
    """One ``cache.get_many`` for many users instead of N ``cache.get`` calls.

    With a remote Redis, a per-member lookup inside a serializer turns a
    25-member page into 25 sequential network round trips. Returns
    ``{user_id: bool}``.
    """
    from apps.core.cache import safe_get_many

    ids = [str(x) for x in {str(i) for i in user_ids if i}]
    if not ids:
        return {}
    # Presence is a decoration. Before this guard a Redis hiccup raised out of
    # the serializer and turned GET /api/members/ into a 500 ("Unable to load
    # members. The server encountered a problem."). Everyone simply shows as
    # offline while the cache is unavailable; the outage is logged once.
    raw = safe_get_many([f"presence:{i}" for i in ids], operation="presence_online_map")
    return {i: bool(raw.get(f"presence:{i}", 0)) for i in ids}


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
        # List views pre-compute the whole page with one get_many and pass it
        # through the context; the single-get path remains for detail views.
        batched = self.context.get("presence_online_map")
        if batched is not None:
            return bool(batched.get(str(obj.id), False))
        return presence_online_map([obj.id]).get(str(obj.id), False)


class MemberCreateSerializer(serializers.Serializer):
    """The canonical member-creation contract.

    The public API field is ``display_name`` (what the UI and the rest of the
    API surface use); it maps explicitly onto the model's ``full_name``.
    ``full_name`` is accepted as a direct alias so API callers written against
    the model field keep working — exactly one of the two is required.

    Every accepted field is consumed: ``is_active`` and ``email`` are applied
    by the service, ``phone`` is normalized and validated here so a bad value
    is reported against the ``phone`` field (and a duplicate too, which the
    service re-checks authoritatively). Nothing is silently discarded.
    """

    display_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    full_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=32)
    email = serializers.EmailField(required=False, allow_blank=True)
    is_active = serializers.BooleanField(required=False)

    def validate(self, attrs):
        name = (attrs.get("display_name") or "").strip() or (attrs.get("full_name") or "").strip()
        if not name:
            raise serializers.ValidationError(
                {"display_name": ["A display name is required."]}
            )
        attrs["full_name"] = name
        attrs.pop("display_name", None)
        attrs["email"] = (attrs.get("email") or "").strip() or ""

        # Normalize here (not only in the service) so invalid numbers surface
        # as field errors the form can pin to the phone input.
        from .services import normalize_phone

        try:
            attrs["phone"] = normalize_phone(attrs["phone"])
        except Exception:
            raise serializers.ValidationError(
                {"phone": ["Enter a valid international phone number, e.g. +2348012345678."]}
            )
        return attrs


class MemberUpdateSerializer(serializers.ModelSerializer):
    """Administrative member updates.

    ``display_name`` is the public API field (model: ``full_name``). ``phone``
    is accepted and re-normalized here so the edit form's phone input is real,
    not decorative; duplicates are refused against every *other* user.
    Activation state deliberately has its own endpoints (activate/deactivate,
    which also revoke sessions) and is not patchable here.
    """

    display_name = serializers.CharField(source="full_name", required=False, max_length=150)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)

    class Meta:
        model = User
        fields = ["full_name", "display_name", "email", "phone"]

    def validate_phone(self, value):
        from .services import normalize_phone

        value = str(value or "").strip()
        if not value:
            return value
        try:
            return normalize_phone(value)
        except Exception:
            raise serializers.ValidationError(
                "Enter a valid international phone number, e.g. +2348012345678."
            )

    def validate(self, attrs):
        phone = attrs.get("phone")
        if phone:
            sister = User.objects.filter(phone=phone).exclude(pk=self.instance.pk).exists()
            if sister:
                raise serializers.ValidationError({"phone": ["A user with that phone number already exists."]})
        # A blanked display name is never acceptable when the field is present.
        name = attrs.get("full_name")
        if name is not None and not str(name).strip():
            raise serializers.ValidationError({"display_name": ["A display name is required."]})
        return attrs


class PinSerializer(serializers.Serializer):
    current_pin = serializers.CharField(
        required=False,
        allow_blank=True,
        error_messages={"invalid": "Enter your current six-digit PIN."},
    )
    old_pin = serializers.CharField(required=False, allow_blank=True, write_only=True)
    new_pin = serializers.RegexField(
        r"^\d{6}$",
        error_messages={
            "required": "Enter a new six-digit PIN.",
            "blank": "Enter a new six-digit PIN.",
            "invalid": "The new PIN must be six digits.",
        },
    )
    confirm_pin = serializers.CharField(required=False, allow_blank=True, write_only=True)

    WEAK_PINS = {
        "000000",
        "111111",
        "123456",
        "654321",
        "999999",
        "112233",
        "121212",
        "123123",
        "000123",
    }

    def validate_new_pin(self, value):
        if value in self.WEAK_PINS:
            raise serializers.ValidationError("Choose a less predictable PIN.")
        if len(set(value)) == 1:
            raise serializers.ValidationError("Choose a less predictable PIN.")
        if value in "01234567890" or value in "09876543210":
            raise serializers.ValidationError("Choose a less predictable PIN.")
        return value

    def validate(self, attrs):
        import re

        from .services import initial_pin

        user = self.context.get("user") or getattr(self.context.get("request"), "user", None)
        must_change = bool(
            user
            and getattr(user, "is_authenticated", False)
            and getattr(user, "must_change_pin", False)
        )

        raw_current = attrs.get("current_pin") or attrs.get("old_pin") or ""
        current_pin = str(raw_current).strip()
        new_pin = attrs["new_pin"]

        if not must_change:
            if not current_pin:
                raise serializers.ValidationError({"current_pin": ["Enter your current six-digit PIN."]})
            if not re.fullmatch(r"^\d{6}$", current_pin):
                raise serializers.ValidationError({"current_pin": ["Enter your current six-digit PIN."]})
        elif current_pin and not re.fullmatch(r"^\d{6}$", current_pin):
            raise serializers.ValidationError({"current_pin": ["Enter your current six-digit PIN."]})

        attrs["current_pin"] = current_pin

        if "confirm_pin" in self.initial_data:
            confirm_pin = str(self.initial_data.get("confirm_pin") or "").strip()
            if not confirm_pin:
                raise serializers.ValidationError({"confirm_pin": ["Confirm the new PIN."]})
            if confirm_pin != new_pin:
                raise serializers.ValidationError({"confirm_pin": ["Confirmation PIN does not match."]})

        if current_pin and new_pin == current_pin:
            raise serializers.ValidationError({"new_pin": ["The new PIN must differ from the current one."]})

        if user and getattr(user, "is_authenticated", False):
            if user.check_password(new_pin):
                raise serializers.ValidationError(
                    {"new_pin": ["The new PIN must differ from the current one."]}
                )
            try:
                if getattr(user, "phone", "") and new_pin == initial_pin(user.phone):
                    raise serializers.ValidationError(
                        {"new_pin": ["The new PIN cannot match your initial phone-derived PIN."]}
                    )
            except ValueError:
                pass

        return attrs


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
