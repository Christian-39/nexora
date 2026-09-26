"""
NEXORA — persistent notifications and durable web push.

Two distinct concepts, deliberately kept separate:

* **Transient realtime events** (``message.new``, ``typing`` …) are delivered
  over WebSockets and are not stored.
* **Persistent notifications** are database rows that survive a reload, drive
  the notification centre, and are the source of the push deliveries.

Burst aggregation
-----------------
Within ``notification_aggregation_window_seconds`` further messages from the
same sender in the same conversation update the existing unread notification
("5 new messages from John") instead of creating a new one. Unread *message*
counts are unaffected — they always come from message receipts.
"""

from __future__ import annotations

import json
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Notification, PushDelivery, PushSubscription

logger = logging.getLogger("nexora.notifications")


def create_notification(*, recipient_id, type, title, message, related_id=None, conversation_id=None):
    with transaction.atomic():
        notification = Notification.objects.create(
            recipient_id=recipient_id,
            type=type,
            title=title,
            message=message,
            related_id=related_id,
            conversation_id=conversation_id,
        )
        _queue_push(notification)
    _broadcast(notification)
    return notification


def notify_new_message(*, message, recipient_ids) -> None:
    """Create/aggregate a NEW_MESSAGE notification for each recipient."""
    from apps.platform_settings.services import messaging_policy

    policy = messaging_policy()
    window = timezone.timedelta(seconds=policy["notification_aggregation_window_seconds"])
    preview_allowed = policy["notification_previews"]
    sender_name = message.sender.full_name
    is_group = message.conversation.kind == "GROUP"
    group_name = getattr(getattr(message.conversation, "group", None), "name", "")

    for recipient_id in recipient_ids:
        recipient = _recipient_preferences(recipient_id)
        if not recipient:
            continue
        if is_group and not recipient["notify_groups"]:
            continue
        if not is_group and not recipient["notify_messages"]:
            continue

        show_preview = preview_allowed and recipient["notification_preview"]
        existing = (
            Notification.objects.select_for_update()
            .filter(
                recipient_id=recipient_id,
                type="NEW_MESSAGE",
                conversation_id=message.conversation_id,
                sender_id=message.sender_id,
                read_at__isnull=True,
                created_at__gte=timezone.now() - window,
            )
            .order_by("-created_at")
            .first()
            if window.total_seconds() > 0
            else None
        )

        with transaction.atomic():
            if existing:
                existing.aggregate_count += 1
                existing.related_id = message.id
                existing.title = f"{sender_name} in {group_name}" if is_group and group_name else sender_name
                existing.message = _body(existing.aggregate_count, sender_name, message, show_preview)
                existing.created_at = timezone.now()
                existing.save(
                    update_fields=["aggregate_count", "related_id", "title", "message", "created_at", "updated_at"]
                )
                notification = existing
                PushDelivery.objects.filter(notification=notification, state="PENDING").delete()
                _queue_push(notification)
            else:
                notification = Notification.objects.create(
                    recipient_id=recipient_id,
                    type="NEW_MESSAGE",
                    title=f"{sender_name} in {group_name}" if is_group and group_name else sender_name,
                    message=_body(1, sender_name, message, show_preview),
                    related_id=message.id,
                    conversation_id=message.conversation_id,
                    sender_id=message.sender_id,
                )
                _queue_push(notification)
        _broadcast(notification)


def _body(count: int, sender_name: str, message, show_preview: bool) -> str:
    if count > 1:
        return f"{count} new messages from {sender_name}"
    if not show_preview:
        return "You have a new message."
    if message.type == "TEXT":
        return (message.text or "")[:200] or "New message"
    return {"IMAGE": "Sent a photo", "VIDEO": "Sent a video", "VOICE": "Sent a voice note"}.get(
        message.type, "New message"
    )


def _recipient_preferences(recipient_id):
    from apps.accounts.models import User

    row = User.objects.filter(id=recipient_id, is_active=True).values(
        "notify_messages", "notify_groups", "notification_preview"
    ).first()
    return row


def _broadcast(notification) -> None:
    from apps.conversations.realtime import broadcast_notification, emit_to_users

    broadcast_notification(notification)
    emit_to_users(
        [notification.recipient_id],
        "unread.update",
        {"conversation_id": str(notification.conversation_id) if notification.conversation_id else None},
    )


def _queue_push(notification) -> None:
    from apps.platform_settings.services import messaging_policy

    if not messaging_policy()["push_enabled"] or not settings.PUSH_PRIVATE_KEY:
        return
    subscriptions = PushSubscription.objects.filter(
        user_id=notification.recipient_id, user__push_enabled=True, is_active=True
    )
    PushDelivery.objects.bulk_create(
        [PushDelivery(notification=notification, subscription=s) for s in subscriptions],
        ignore_conflicts=True,
    )


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def payload_for(notification) -> str:
    from apps.conversations.services import unread_map

    unread = unread_map_total(notification.recipient_id)
    return json.dumps(
        {
            "notification_id": str(notification.id),
            "type": notification.type,
            "title": notification.title,
            "body": notification.message,
            "conversation_id": str(notification.conversation_id) if notification.conversation_id else None,
            "related_id": str(notification.related_id) if notification.related_id else None,
            "unread_total": unread,
            "tag": f"conv-{notification.conversation_id}" if notification.conversation_id else "nexora",
            "renotify": notification.aggregate_count > 1,
            "url": (
                f"chat.html?c={notification.conversation_id}" if notification.conversation_id else "chat.html"
            ),
            "timestamp": int(notification.created_at.timestamp() * 1000),
        }
    )


def unread_map_total(user_id) -> int:
    from apps.conversations.models import MessageReceipt

    return MessageReceipt.objects.filter(
        recipient_id=user_id, read_at__isnull=True, message__conversation__is_active=True
    ).count()


def deliver(delivery) -> None:
    """Send one push delivery, applying retry/backoff and pruning dead endpoints."""
    from pywebpush import WebPushException, webpush

    subscription = delivery.subscription
    try:
        webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
            },
            data=payload_for(delivery.notification),
            vapid_private_key=settings.PUSH_PRIVATE_KEY,
            vapid_claims={"sub": settings.PUSH_CONTACT} if settings.PUSH_CONTACT else None,
            ttl=300,
        )
        delivery.state = PushDelivery.State.SENT
        delivery.last_error_code = ""
        subscription.last_used_at = timezone.now()
        subscription.save(update_fields=["last_used_at", "updated_at"])
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        delivery.attempts += 1
        delivery.last_error_code = str(status or "WEBPUSH_ERROR")[:40]
        if status in (400, 404, 410):
            # Gone/invalid: prune the subscription rather than retrying forever.
            subscription.is_active = False
            subscription.save(update_fields=["is_active", "updated_at"])
            delivery.state = PushDelivery.State.FAILED
        elif delivery.attempts >= settings.PUSH_MAX_ATTEMPTS:
            delivery.state = PushDelivery.State.FAILED
        else:
            delivery.state = PushDelivery.State.RETRY
            delivery.next_attempt_at = timezone.now() + timezone.timedelta(
                seconds=min(3600, 30 * (2**delivery.attempts))
            )
    except Exception as exc:  # noqa: BLE001 - a bad endpoint must not kill the worker
        logger.warning("push delivery error: %s", exc.__class__.__name__)
        delivery.attempts += 1
        delivery.last_error_code = exc.__class__.__name__[:40]
        delivery.state = (
            PushDelivery.State.FAILED
            if delivery.attempts >= settings.PUSH_MAX_ATTEMPTS
            else PushDelivery.State.RETRY
        )
        delivery.next_attempt_at = timezone.now() + timezone.timedelta(
            seconds=min(3600, 30 * (2**delivery.attempts))
        )

    delivery.save(
        update_fields=["state", "attempts", "next_attempt_at", "last_error_code", "updated_at"]
    )
