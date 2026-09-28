from rest_framework import serializers

from .models import Notification, PushSubscription


class NotificationSerializer(serializers.ModelSerializer):
    # The FK id column is already on the notification row; reading
    # ``conversation.id`` forced one SELECT per notification (N+1).
    conversation_id = serializers.SerializerMethodField()
    body = serializers.CharField(source="message", read_only=True)

    def get_conversation_id(self, obj):
        return str(obj.conversation_id) if obj.conversation_id else None

    class Meta:
        model = Notification
        fields = [
            "id",
            "type",
            "title",
            "message",
            "body",
            "related_id",
            "conversation_id",
            "aggregate_count",
            "read_at",
            "created_at",
        ]
        read_only_fields = fields


class PushSerializer(serializers.ModelSerializer):
    class Meta:
        model = PushSubscription
        fields = ["id", "endpoint", "p256dh", "auth", "device_label", "created_at", "last_used_at"]
        read_only_fields = ["id", "created_at", "last_used_at"]
        extra_kwargs = {"p256dh": {"write_only": True}, "auth": {"write_only": True}}
