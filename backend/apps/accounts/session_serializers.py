from rest_framework import serializers

from .models import DeviceSession


class SessionSerializer(serializers.ModelSerializer):
    """Device sessions. No token material is ever exposed."""

    last_active_at = serializers.DateTimeField(source="last_active", read_only=True)
    is_current = serializers.SerializerMethodField()
    platform = serializers.SerializerMethodField()
    browser = serializers.SerializerMethodField()

    class Meta:
        model = DeviceSession
        fields = [
            "id",
            "device_label",
            "platform",
            "browser",
            "created_at",
            "last_active",
            "last_active_at",
            "expires_at",
            "revoked_at",
            "is_current",
        ]
        read_only_fields = fields

    def get_is_current(self, obj):
        return str(obj.id) == str(self.context.get("current_sid", ""))

    def get_platform(self, obj):
        agent = (obj.user_agent or "").lower()
        for needle, label in (
            ("android", "Android"),
            ("iphone", "iPhone"),
            ("ipad", "iPad"),
            ("windows", "Windows"),
            ("mac os", "macOS"),
            ("linux", "Linux"),
        ):
            if needle in agent:
                return label
        return obj.device_label or "Unknown device"

    def get_browser(self, obj):
        agent = obj.user_agent or ""
        for needle, label in (("Edg/", "Edge"), ("Chrome/", "Chrome"), ("Firefox/", "Firefox"), ("Safari/", "Safari")):
            if needle in agent:
                return label
        return ""
