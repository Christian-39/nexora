from rest_framework import serializers

from .models import Notification, PushSubscription


class NotificationSerializer(serializers.ModelSerializer):
    conversation_id = serializers.CharField(source="conversation.id", read_only=True, default=None)
    body = serializers.CharField(source="message", read_only=True)

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
