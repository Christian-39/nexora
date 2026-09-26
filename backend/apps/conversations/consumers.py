"""
NEXORA — Django Channels consumers.

``/ws/app/`` is the single multiplexed application socket used by the frontend:
one connection per tab carries messages, receipts, typing, presence, unread
deltas, notifications and group lifecycle. Multiplexing avoids N sockets per
user and keeps reconnection logic in one place.

Authorization is re-checked on the server for every subscription and every
inbound frame: ``conversation.join`` for a conversation the user does not
participate in is refused, so a guessed UUID gains nothing.

``/ws/conversations/{uuid}/`` remains available as a single-thread socket for
embedded/limited clients and shares the same channel groups and event names.
"""

from __future__ import annotations

import asyncio
import json
import time

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings
from django.core.cache import cache
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

from . import realtime

TYPING_TIMEOUT = 8
MAX_JOINED_CONVERSATIONS = 40


class BaseAuthenticatedConsumer(AsyncJsonWebsocketConsumer):
    """Shared authentication, token-expiry enforcement and fan-out plumbing."""

    @classmethod
    async def encode_json(cls, content):
        # Serializer output legitimately contains UUID/datetime/Decimal values;
        # the default json encoder would raise on them mid-broadcast.
        return json.dumps(content, cls=DjangoJSONEncoder)

    #: Close code reserved for "your credential is not (or no longer) valid".
    #: It is deliberately in the 4400 range so the client can tell an
    #: authentication failure apart from a network failure and stop retrying.
    AUTH_CLOSE_CODE = 4401

    async def _authenticate(self) -> bool:
        """Authenticate the handshake, telling the client *why* it failed.

        A handshake rejected before ``accept()`` reaches the browser as close
        code 1006 with no reason — indistinguishable from a dropped network,
        which is exactly what makes a client reconnect forever against an
        expired session. So the socket is accepted just long enough to deliver
        one typed ``auth.error`` frame and is then closed. No group is joined,
        no data is sent, and no inbound frame is ever processed.
        """
        user = self.scope.get("user")
        if user and user.is_authenticated:
            self.user = user
            return True

        code = self.scope.get("auth_error") or "UNAUTHENTICATED"
        await self.accept()
        await self.send_json({"type": "auth.error", "code": code})
        await self.close(code=self.AUTH_CLOSE_CODE)
        return False

    def _start_expiry_watch(self) -> None:
        self.expiry_task = asyncio.create_task(self._expire())

    async def _expire(self) -> None:
        """Close the socket when the access token it was opened with expires.

        The client refreshes over HTTP and reconnects; a socket can never
        outlive its credential.
        """
        expiry = self.scope.get("token_exp") or int(time.time())
        await asyncio.sleep(max(0, expiry - int(time.time())))
        await self.send_json({"type": "auth.error", "code": "TOKEN_EXPIRED"})
        await self.close(code=4401)

    async def fanout(self, event) -> None:
        await self.send_json({"type": event["event"], "data": event["payload"]})


class AppConsumer(BaseAuthenticatedConsumer):
    """The multiplexed application socket (``/ws/app/``)."""

    async def connect(self):
        if not await self._authenticate():
            return

        self.joined: set[str] = set()
        self.typing_tasks: dict[str, asyncio.Task] = {}
        self.presence_audience: list[str] = []

        await self.channel_layer.group_add(realtime.user_group(self.user.id), self.channel_name)
        await self.accept()
        self._start_expiry_watch()

        self.presence_audience = await self._presence_audience()
        for group in self.presence_audience:
            await self.channel_layer.group_add(group, self.channel_name)

        if await self._presence_enabled():
            first = await self._presence_increment()
            if first:
                realtime.broadcast_presence(
                    self.user.id, online=True, audience=await self._watchers()
                )

        await self.send_json(
            {
                "type": "connection.ready",
                "data": {
                    "user_id": str(self.user.id),
                    "server_time": timezone.now().isoformat(),
                    "presence": await self._presence_snapshot(),
                },
            }
        )

    async def disconnect(self, code):
        # A socket rejected during authentication never reached the setup below,
        # so nothing may be assumed to exist here.
        for task in (*getattr(self, "typing_tasks", {}).values(), getattr(self, "expiry_task", None)):
            if task:
                task.cancel()
        if not hasattr(self, "user"):
            return
        for conversation_id in list(getattr(self, "joined", ())):
            await self.channel_layer.group_discard(realtime.conversation_group(conversation_id), self.channel_name)
        for group in getattr(self, "presence_audience", ()):
            await self.channel_layer.group_discard(group, self.channel_name)
        await self.channel_layer.group_discard(realtime.user_group(self.user.id), self.channel_name)

        if await self._presence_enabled():
            last = await self._presence_decrement()
            if last:
                last_seen = await self._touch_last_seen()
                realtime.broadcast_presence(
                    self.user.id,
                    online=False,
                    last_seen=last_seen if self.user.show_last_seen else None,
                    audience=await self._watchers(),
                )

    # -- inbound ------------------------------------------------------------

    async def receive_json(self, content, **kwargs):
        if not isinstance(content, dict):
            return
        handler = {
            "ping": self._on_ping,
            "conversation.join": self._on_join,
            "conversation.leave": self._on_leave,
            "typing": self._on_typing,
            "message.read": self._on_read,
            "message.delivered": self._on_delivered,
            "presence.ping": self._on_presence_ping,
        }.get(str(content.get("type", "")))
        if handler:
            await handler(content)

    async def _on_ping(self, content):
        await self.send_json({"type": "pong", "t": content.get("t")})

    async def _on_presence_ping(self, _content):
        await self._presence_touch()

    async def _on_join(self, content):
        conversation_id = str(content.get("conversation_id") or "")
        if not conversation_id or conversation_id in self.joined:
            return
        if len(self.joined) >= MAX_JOINED_CONVERSATIONS:
            oldest = next(iter(self.joined))
            await self._leave(oldest)
        if not await self._may_access(conversation_id):
            await self.send_json(
                {"type": "conversation.denied", "data": {"conversation_id": conversation_id}}
            )
            return
        await self.channel_layer.group_add(realtime.conversation_group(conversation_id), self.channel_name)
        self.joined.add(conversation_id)
        delivered = await self._mark_delivered(conversation_id)
        if delivered:
            realtime.broadcast_receipts(
                conversation_id, user_id=self.user.id, state="delivered", message_ids=delivered,
                timestamp=timezone.now(),
            )
        await self.send_json({"type": "conversation.joined", "data": {"conversation_id": conversation_id}})

    async def _on_leave(self, content):
        await self._leave(str(content.get("conversation_id") or ""))

    async def _leave(self, conversation_id):
        if conversation_id not in self.joined:
            return
        await self.channel_layer.group_discard(realtime.conversation_group(conversation_id), self.channel_name)
        self.joined.discard(conversation_id)

    async def _on_typing(self, content):
        conversation_id = str(content.get("conversation_id") or "")
        if conversation_id not in self.joined:
            return
        if not await self._typing_enabled():
            return
        typing = bool(content.get("typing"))
        realtime.broadcast_typing(conversation_id, user_id=self.user.id, typing=typing)

        task = self.typing_tasks.pop(conversation_id, None)
        if task:
            task.cancel()
        if typing:
            self.typing_tasks[conversation_id] = asyncio.create_task(self._auto_clear_typing(conversation_id))

    async def _auto_clear_typing(self, conversation_id):
        await asyncio.sleep(TYPING_TIMEOUT)
        realtime.broadcast_typing(conversation_id, user_id=self.user.id, typing=False)
        self.typing_tasks.pop(conversation_id, None)

    async def _on_read(self, content):
        conversation_id = str(content.get("conversation_id") or "")
        if not await self._may_access(conversation_id):
            return
        now = timezone.now()
        message_ids = await self._mark_read(conversation_id, content.get("message_id"))
        realtime.broadcast_receipts(
            conversation_id, user_id=self.user.id, state="read", message_ids=message_ids, timestamp=now
        )
        await self.send_json(
            {"type": "unread.update", "data": {"conversation_id": conversation_id, "unread": 0}}
        )

    async def _on_delivered(self, content):
        ids = [str(x) for x in (content.get("message_ids") or [])][:200]
        if not ids:
            return
        updated = await self._mark_delivered_ids(ids)
        for conversation_id, message_ids in updated.items():
            realtime.broadcast_receipts(
                conversation_id, user_id=self.user.id, state="delivered", message_ids=message_ids,
                timestamp=timezone.now(),
            )

    # -- database helpers ---------------------------------------------------

    @database_sync_to_async
    def _may_access(self, conversation_id) -> bool:
        from .models import ConversationParticipant

        try:
            return ConversationParticipant.objects.filter(
                conversation_id=conversation_id,
                user=self.user,
                is_active=True,
                conversation__is_active=True,
            ).exists()
        except (ValueError, TypeError, ValidationError_):
            return False

    @database_sync_to_async
    def _mark_delivered(self, conversation_id) -> list[str]:
        from .models import MessageReceipt

        pending = list(
            MessageReceipt.objects.filter(
                message__conversation_id=conversation_id, recipient=self.user, delivered_at__isnull=True
            ).values_list("message_id", flat=True)
        )
        if pending:
            MessageReceipt.objects.filter(
                message_id__in=pending, recipient=self.user, delivered_at__isnull=True
            ).update(delivered_at=timezone.now())
        return [str(x) for x in pending]

    @database_sync_to_async
    def _mark_delivered_ids(self, ids) -> dict:
        from .models import MessageReceipt

        rows = list(
            MessageReceipt.objects.filter(
                message_id__in=ids, recipient=self.user, delivered_at__isnull=True
            ).values_list("message_id", "message__conversation_id")
        )
        if rows:
            MessageReceipt.objects.filter(
                message_id__in=[r[0] for r in rows], recipient=self.user
            ).update(delivered_at=timezone.now())
        grouped: dict = {}
        for message_id, conversation_id in rows:
            grouped.setdefault(str(conversation_id), []).append(str(message_id))
        return grouped

    @database_sync_to_async
    def _mark_read(self, conversation_id, upto_message_id) -> list[str]:
        from .models import ConversationParticipant, MessageReceipt

        now = timezone.now()
        queryset = MessageReceipt.objects.filter(
            message__conversation_id=conversation_id, recipient=self.user, read_at__isnull=True
        )
        ids = [str(x) for x in queryset.values_list("message_id", flat=True)]
        queryset.update(delivered_at=now, read_at=now)
        ConversationParticipant.objects.filter(conversation_id=conversation_id, user=self.user).update(
            last_read_at=now
        )
        return ids

    @database_sync_to_async
    def _presence_audience(self) -> list[str]:
        return [realtime.user_group(x) for x in _presence_peers(self.user)]

    @database_sync_to_async
    def _watchers(self) -> list[str]:
        return [str(x) for x in _presence_peers(self.user)] + [str(self.user.id)]

    @database_sync_to_async
    def _presence_snapshot(self) -> list[dict]:
        peers = _presence_peers(self.user)
        return [{"user_id": str(x), "online": bool(cache.get(f"presence:{x}", 0))} for x in peers]

    @database_sync_to_async
    def _touch_last_seen(self):
        from apps.accounts.models import User

        now = timezone.now()
        User.objects.filter(id=self.user.id).update(last_seen=now)
        return now

    @database_sync_to_async
    def _presence_enabled(self) -> bool:
        from apps.platform_settings.services import messaging_policy

        return messaging_policy()["presence_enabled"]

    @database_sync_to_async
    def _typing_enabled(self) -> bool:
        from apps.platform_settings.services import messaging_policy

        return messaging_policy()["typing_indicators_enabled"]

    @database_sync_to_async
    def _presence_increment(self) -> bool:
        key = f"presence:{self.user.id}"
        cache.add(key, 0, timeout=settings.PRESENCE_TTL_SECONDS)
        try:
            value = cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=settings.PRESENCE_TTL_SECONDS)
            value = 1
        cache.touch(key, settings.PRESENCE_TTL_SECONDS)
        return value == 1

    @database_sync_to_async
    def _presence_decrement(self) -> bool:
        key = f"presence:{self.user.id}"
        try:
            value = cache.decr(key)
        except ValueError:
            value = 0
        if value <= 0:
            cache.delete(key)
            return True
        cache.touch(key, settings.PRESENCE_TTL_SECONDS)
        return False

    @database_sync_to_async
    def _presence_touch(self):
        # Refresh the TTL only — no database write per heartbeat.
        cache.touch(f"presence:{self.user.id}", settings.PRESENCE_TTL_SECONDS)


class ValidationError_(Exception):
    """Local alias so a malformed UUID cannot escape ``_may_access``."""


def _presence_peers(user):
    """Users whose presence this user is allowed to observe.

    Admin sees every active member; a member sees the administrators and the
    members of groups they belong to. Nothing else.
    """
    from apps.accounts.models import User

    if user.role == "ADMIN":
        return list(User.objects.filter(role="MEMBER", is_active=True).values_list("id", flat=True))
    ids = set(User.objects.filter(role="ADMIN", is_active=True).values_list("id", flat=True))
    ids.update(
        User.objects.filter(
            conversation_memberships__conversation__kind="GROUP",
            conversation_memberships__is_active=True,
            conversation_memberships__conversation__participants__user=user,
            conversation_memberships__conversation__participants__is_active=True,
            is_active=True,
        )
        .exclude(id=user.id)
        .values_list("id", flat=True)
    )
    return list(ids)


class ConversationConsumer(BaseAuthenticatedConsumer):
    """Single-conversation socket (``/ws/conversations/{uuid}/``)."""

    async def connect(self):
        if not await self._authenticate():
            return
        self.conversation_id = self.scope["url_route"]["kwargs"]["conversation_id"]
        if not await self._allowed():
            # Authenticated but not a participant: a permanent refusal, so the
            # client is told explicitly instead of being left to reconnect.
            await self.accept()
            await self.send_json({"type": "auth.error", "code": "FORBIDDEN"})
            await self.close(code=4403)
            return
        self.group = realtime.conversation_group(self.conversation_id)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        self._start_expiry_watch()
        delivered = await self._mark_delivered()
        if delivered:
            realtime.broadcast_receipts(
                self.conversation_id, user_id=self.user.id, state="delivered",
                message_ids=delivered, timestamp=timezone.now(),
            )

    async def disconnect(self, code):
        task = getattr(self, "expiry_task", None)
        if task:
            task.cancel()
        if hasattr(self, "group"):
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def receive_json(self, content, **kwargs):
        if isinstance(content, dict) and content.get("type") == "ping":
            await self.send_json({"type": "pong", "t": content.get("t")})
        elif isinstance(content, dict) and content.get("type") == "typing":
            realtime.broadcast_typing(
                self.conversation_id, user_id=self.user.id, typing=bool(content.get("typing"))
            )

    @database_sync_to_async
    def _allowed(self) -> bool:
        from .models import ConversationParticipant

        try:
            return ConversationParticipant.objects.filter(
                conversation_id=self.conversation_id,
                user=self.user,
                is_active=True,
                conversation__is_active=True,
            ).exists()
        except (ValueError, TypeError):
            return False

    @database_sync_to_async
    def _mark_delivered(self) -> list[str]:
        from .models import MessageReceipt

        pending = list(
            MessageReceipt.objects.filter(
                message__conversation_id=self.conversation_id,
                recipient=self.user,
                delivered_at__isnull=True,
            ).values_list("message_id", flat=True)
        )
        if pending:
            MessageReceipt.objects.filter(message_id__in=pending, recipient=self.user).update(
                delivered_at=timezone.now()
            )
        return [str(x) for x in pending]
