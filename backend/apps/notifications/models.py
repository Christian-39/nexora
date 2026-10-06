import uuid

from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


class Notification(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    type = models.CharField(max_length=40)
    title = models.CharField(max_length=150)
    message = models.CharField(max_length=500)
    related_id = models.UUIDField(null=True)
    conversation = models.ForeignKey(
        "conversations.Conversation",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    aggregate_count = models.PositiveIntegerField(default=1)
    read_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [
            models.Index(fields=["recipient", "read_at", "-created_at"]),
            models.Index(fields=["recipient", "type", "conversation", "read_at"]),
        ]


class PushSubscription(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    endpoint = models.URLField(max_length=1000)
    endpoint_hash = models.CharField(max_length=64, editable=False)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    user_agent = models.CharField(max_length=200, blank=True)
    device_label = models.CharField(max_length=120, blank=True)
    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["endpoint_hash"], name="unique_push_subscription_endpoint_hash"
            )
        ]

    def save(self, *args, **kwargs):
        from apps.core.hashes import sha256_hex

        self.endpoint_hash = sha256_hex(self.endpoint)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "endpoint" in update_fields:
            kwargs["update_fields"] = set(update_fields) | {"endpoint_hash"}
        return super().save(*args, **kwargs)


class PushDelivery(TimeStampedModel):
    class State(models.TextChoices):
        PENDING = "PENDING"
        SENT = "SENT"
        RETRY = "RETRY"
        FAILED = "FAILED"

    notification = models.ForeignKey(
        Notification, on_delete=models.CASCADE, related_name="push_deliveries"
    )
    subscription = models.ForeignKey(
        PushSubscription, on_delete=models.CASCADE, related_name="deliveries"
    )
    state = models.CharField(max_length=10, choices=State.choices, default=State.PENDING, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_error_code = models.CharField(max_length=40, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["notification", "subscription"], name="unique_notification_push_subscription"
            )
        ]
