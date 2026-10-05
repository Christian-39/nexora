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


def broadcast_new_message(message, *, request=None, recipient_ids=()) -> None:
    payload = _message_payload(message, request=request)
    emit_to_conversation(message.conversation_id, "message.new", payload)
    # Users not currently inside the thread still need list/unread updates.
    emit_to_users(recipient_ids, "conversation.updated", {
        "conversation_id": str(message.conversation_id),
        "last_message": payload,
    })
    for recipient_id in recipient_ids:
        # Both names are published: 'unread.update' is the global badge signal,
        # 'conversation.unread' is the per-thread one the chat list listens to.
        payload = {"conversation_id": str(message.conversation_id)}
        emit_to_users([recipient_id], "unread.update", payload)
        emit_to_users([recipient_id], "conversation.unread", payload)


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
