"""
NEXORA — the single place server-side realtime events are emitted.

Every event the frontend listens for is emitted from here, and only from here,
so the WebSocket contract has exactly one definition.

Channel groups
--------------
``conversation_{uuid}``  every active participant currently subscribed
``user_{uuid}``          every live connection of one user (fan-out, unread,
                         notifications, conversation lifecycle, presence)

Emissions are deferred with ``transaction.on_commit`` wherever they follow a
database write, so a subscriber can never observe an event for a row that was
rolled back.
"""

from __future__ import annotations

import asyncio
import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction

logger = logging.getLogger("nexora.realtime")


def conversation_group(conversation_id) -> str:
    return f"conversation_{conversation_id}"


def user_group(user_id) -> str:
    return f"user_{user_id}"


async def _async_group_send(layer, group: str, event_type: str, message: dict) -> None:
    try:
        await layer.group_send(group, message)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - realtime must never break a consumer or request
        logger.warning(
            "Failed to publish %s to %s (%s)",
            event_type,
            group,
            exc.__class__.__name__,
            extra={
                "event": "realtime.publish_failed",
                "group": group,
                "event_type": event_type,
                "error_type": exc.__class__.__name__,
            },
        )


def _send(group: str, event_type: str, payload: dict) -> None:
    """Publish one event, from either a sync view or an async consumer.

    Views run in a worker thread with no event loop (``async_to_sync``);
    consumers already run inside one, where ``async_to_sync`` is illegal — the
    coroutine is scheduled on the running loop instead.
    """
    layer = get_channel_layer()
    if layer is None:  # pragma: no cover - misconfiguration
        logger.error("No channel layer configured; dropping %s", event_type)
        return
    message = {"type": "fanout", "event": event_type, "payload": payload}
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(_async_group_send(layer, group, event_type, message))
        else:
            async_to_sync(layer.group_send)(group, message)
    except Exception as exc:  # noqa: BLE001 - realtime must never break a request
        logger.warning(
            "Failed to publish %s to %s (%s)",
            event_type,
            group,
            exc.__class__.__name__,
            extra={
                "event": "realtime.publish_failed",
                "group": group,
                "event_type": event_type,
                "error_type": exc.__class__.__name__,
            },
        )


def emit(group: str, event_type: str, payload: dict, *, on_commit: bool = True) -> None:
    if on_commit and transaction.get_connection().in_atomic_block:
        transaction.on_commit(lambda: _send(group, event_type, payload))
    else:
        _send(group, event_type, payload)


def emit_to_users(user_ids, event_type: str, payload: dict, *, on_commit: bool = True) -> None:
    for user_id in {str(x) for x in user_ids if x}:
        emit(user_group(user_id), event_type, payload, on_commit=on_commit)


def emit_to_conversation(conversation_id, event_type: str, payload: dict, *, on_commit: bool = True) -> None:
    emit(conversation_group(conversation_id), event_type, payload, on_commit=on_commit)


# ---------------------------------------------------------------------------
# Domain events
# ---------------------------------------------------------------------------


def _message_payload(message, *, request=None):
    from .serializers import MessageSerializer

    return MessageSerializer(message, context={"request": request}).data


def unread_summaries(user_ids) -> dict[str, dict]:
    """Return one authoritative unread snapshot per user in two grouped queries.

    Message unread counts remain receipt-derived; notification unread counts are
    separate. This is deliberately aggregate-only and never fetches message or
    notification bodies.
    """
    from django.db.models import Count

    from apps.notifications.models import Notification
    from .models import MessageReceipt

    ids = {str(value) for value in user_ids if value}
    summaries = {
        user_id: {
            "global": 0,  # legacy message-only total
            "total": 0,   # legacy message-only total
            "unread_messages_total": 0,
            "unread_total": 0,  # messages + persistent notifications
            "notifications": 0,
            "notifications_unread": 0,
            "conversations": {},
            "conversation_counts": [],
        }
        for user_id in ids
    }
    if not ids:
        return summaries

    rows = (
        MessageReceipt.objects.filter(
            recipient_id__in=ids,
            read_at__isnull=True,
            message__conversation__is_active=True,
        )
        .values("recipient_id", "message__conversation_id")
        .annotate(count=Count("id"))
        .order_by()
    )
    for row in rows:
        user_id = str(row["recipient_id"])
        conversation_id = str(row["message__conversation_id"])
        count = int(row["count"])
        state = summaries.get(user_id)
        if state is None:
            continue
        state["conversations"][conversation_id] = count
        state["unread_messages_total"] += count
        state["conversation_counts"].append(
            {"conversation_id": conversation_id, "unread_count": count}
        )

    notification_rows = (
        Notification.objects.filter(recipient_id__in=ids, read_at__isnull=True)
        .values("recipient_id")
        .annotate(count=Count("id"))
        .order_by()
    )
    for row in notification_rows:
        user_id = str(row["recipient_id"])
        if user_id in summaries:
            summaries[user_id]["notifications"] = int(row["count"])
            summaries[user_id]["notifications_unread"] = int(row["count"])

    for state in summaries.values():
        state["conversation_counts"].sort(key=lambda item: item["conversation_id"])
        state["global"] = state["unread_messages_total"]
        state["total"] = state["unread_messages_total"]
        state["unread_total"] = state["unread_messages_total"] + state["notifications_unread"]
    return summaries


def unread_summary(user_id, *, conversation_id=None) -> dict:
    """Get the current authoritative unread contract for one authenticated user."""
    key = str(user_id)
    state = unread_summaries([user_id]).get(key, {
        "global": 0,
        "total": 0,
        "unread_messages_total": 0,
        "unread_total": 0,
        "notifications": 0,
        "notifications_unread": 0,
        "conversations": {},
        "conversation_counts": [],
    })
    if conversation_id is not None:
        state = dict(state)
        state["conversation_id"] = str(conversation_id)
        state["unread_count"] = int(state["conversations"].get(str(conversation_id), 0))
    return state


def broadcast_unread_state(user_ids, *, conversation_id=None, on_commit=True) -> None:
    """Publish full global and, when requested, per-conversation counts."""
    conversation_key = str(conversation_id) if conversation_id is not None else None
    for user_id, state in unread_summaries(user_ids).items():
        payload = dict(state)
        if conversation_key is not None:
            payload["conversation_id"] = conversation_key
            payload["unread_count"] = int(state["conversations"].get(conversation_key, 0))
        emit_to_users([user_id], "unread.update", payload, on_commit=on_commit)
        if conversation_key is not None:
            emit_to_users(
                [user_id],
                "conversation.unread",
                payload,
                on_commit=on_commit,
            )


def broadcast_new_message(message, *, request=None, recipient_ids=()) -> None:
    payload = _message_payload(message, request=request)
    emit_to_conversation(message.conversation_id, "message.new", payload)
    # Users not currently inside the thread still need list/unread updates.
    emit_to_users(recipient_ids, "conversation.updated", {
        "conversation_id": str(message.conversation_id),
        "last_message": payload,
    })
    # Each recipient receives the authoritative receipt-derived conversation
    # count and global badge state. A missing count is never interpreted as 0.
    broadcast_unread_state(recipient_ids, conversation_id=message.conversation_id)


def broadcast_message_updated(message, *, request=None) -> None:
    emit_to_conversation(message.conversation_id, "message.updated", _message_payload(message, request=request))
    emit_to_conversation(message.conversation_id, "message.edited", _message_payload(message, request=request))


def broadcast_message_deleted(message) -> None:
    emit_to_conversation(
        message.conversation_id,
        "message.deleted",
        {
            "id": str(message.id),
            "conversation_id": str(message.conversation_id),
            "deleted_at": message.deleted_at.isoformat() if message.deleted_at else None,
        },
    )


def broadcast_reaction(message, *, user_id, reaction, removed=False) -> None:
    emit_to_conversation(
        message.conversation_id,
        "message.reaction",
        {
            "message_id": str(message.id),
            "conversation_id": str(message.conversation_id),
            "user_id": str(user_id),
            "reaction": reaction,
            "removed": removed,
        },
    )


def broadcast_receipts(conversation_id, *, user_id, state, message_ids=(), timestamp=None) -> None:
    """``state`` is 'delivered' or 'read'."""
    emit_to_conversation(
        conversation_id,
        f"message.{state}",
        {
            "conversation_id": str(conversation_id),
            "user_id": str(user_id),
            "message_ids": [str(x) for x in message_ids],
            "at": timestamp.isoformat() if timestamp else None,
        },
    )


def broadcast_typing(conversation_id, *, user_id, typing: bool) -> None:
    emit_to_conversation(
        conversation_id,
        "typing.start" if typing else "typing.stop",
        {"conversation_id": str(conversation_id), "user_id": str(user_id), "typing": typing},
        on_commit=False,
    )


def broadcast_presence(user_id, *, online: bool, last_seen=None, audience=()) -> None:
    payload = {
        "user_id": str(user_id),
        "online": online,
        "last_seen": last_seen.isoformat() if hasattr(last_seen, "isoformat") else last_seen,
    }
    emit_to_users(audience, "presence.online" if online else "presence.offline", payload, on_commit=False)
    emit_to_users(audience, "presence.update", payload, on_commit=False)


def broadcast_conversation_created(conversation, *, participant_ids, request=None) -> None:
    from .serializers import ConversationSerializer

    for user_id in participant_ids:
        payload = ConversationSerializer(conversation, context={"request": request, "for_user_id": user_id}).data
        emit_to_users([user_id], "conversation.created", payload)


def broadcast_group_membership(group, *, added=(), removed=()) -> None:
    payload = {"group_id": str(group.id), "conversation_id": str(group.conversation_id), "name": group.name}
    emit_to_users(added, "group.membership", {**payload, "action": "added"})
    emit_to_users(removed, "group.removed", payload)


def broadcast_notification(notification) -> None:
    from apps.notifications.serializers import NotificationSerializer

    emit_to_users([notification.recipient_id], "notification.new", NotificationSerializer(notification).data)


def broadcast_media_ready(attachment) -> None:
    from .serializers import AttachmentSerializer

    emit_to_conversation(
        attachment.message.conversation_id,
        "media.ready",
        {
            "message_id": str(attachment.message_id),
            "conversation_id": str(attachment.message.conversation_id),
            "media": AttachmentSerializer(attachment).data,
        },
        on_commit=False,
    )
