"""Resumable upload sessions for very large attachments."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


class UploadSession(TimeStampedModel):
    """A chunked upload in progress.

    Parts are streamed straight to storage; the request process never holds a
    whole file in memory. A session is bound to its owner and to the
    conversation it will post into, so authorization is settled up-front.
    """

    class State(models.TextChoices):
        OPEN = "OPEN"
        COMPLETED = "COMPLETED"
        ABORTED = "ABORTED"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="upload_sessions")
    conversation = models.ForeignKey(
        "conversations.Conversation", on_delete=models.CASCADE, related_name="upload_sessions"
    )
    kind = models.CharField(max_length=10)
    client_id = models.CharField(max_length=64)
    declared_name = models.CharField(max_length=160, blank=True)
    declared_size = models.PositiveBigIntegerField()
    part_size = models.PositiveIntegerField()
    received_bytes = models.PositiveBigIntegerField(default=0)
    next_part = models.PositiveIntegerField(default=0)
    staging_key = models.CharField(max_length=512, blank=True)
    state = models.CharField(max_length=10, choices=State.choices, default=State.OPEN, db_index=True)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "client_id"], name="unique_user_upload_client_id")
        ]
        indexes = [models.Index(fields=["state", "expires_at"])]

    @property
    def is_open(self) -> bool:
        from django.utils import timezone

        return self.state == self.State.OPEN and self.expires_at > timezone.now()
