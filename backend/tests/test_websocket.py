"""WebSocket contract: authentication, authorization, and every emitted event."""

from __future__ import annotations

import pytest
from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.utils import timezone

from apps.accounts.models import DeviceSession
from config.asgi import application
from tests.conftest import client_id


@database_sync_to_async
def _issue_cookie(user):
    from rest_framework_simplejwt.tokens import RefreshToken

    refresh = RefreshToken.for_user(user)
    session = DeviceSession.objects.create(
        user=user, jti=str(refresh["jti"]), expires_at=timezone.now() + timezone.timedelta(days=1)
    )
    refresh["sid"] = str(session.id)
    return f"nexora_access={refresh.access_token}".encode()


async def socket(user, path="/ws/app/"):
    """A communicator carrying the same HttpOnly cookie a browser would send."""
    communicator = WebsocketCommunicator(application, path)
    communicator.scope["headers"] = [(b"cookie", await _issue_cookie(user))]
    return communicator


async def drain(communicator, wanted, limit=12):
    """Collect frames until ``wanted`` arrives (events may interleave)."""
    seen = []
    for _ in range(limit):
        if await communicator.receive_nothing(timeout=1.5):
            break
        frame = await communicator.receive_json_from(timeout=2)
        seen.append(frame)
        if frame.get("type") == wanted:
            return frame, seen
    return None, seen


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_unauthenticated_socket_is_told_why_and_closed():
    """An unauthenticated socket gets one typed frame and is then closed.

    A handshake refused before ``accept()`` reaches a browser as close code
    1006 with no reason, which is indistinguishable from a network outage —
    that is what made the client reconnect forever against a dead session. The
    socket is therefore accepted only long enough to deliver ``auth.error``
    and is closed with 4401; it joins no group and receives no data.
    """
    communicator = WebsocketCommunicator(application, "/ws/app/")
    connected, _ = await communicator.connect()
    assert connected

    frame = await communicator.receive_json_from(timeout=3)
    assert frame["type"] == "auth.error"
    assert frame["code"] == "NO_CREDENTIAL"

    closed = await communicator.receive_output(timeout=3)
    assert closed["type"] == "websocket.close"
    assert closed["code"] == 4401
    await communicator.disconnect()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_revoked_device_session_cannot_open_a_socket(member_a):
    """Signing out on a device kills that device's sockets everywhere."""
    from rest_framework_simplejwt.tokens import RefreshToken

    @database_sync_to_async
    def revoked_cookie():
        refresh = RefreshToken.for_user(member_a)
        session = DeviceSession.objects.create(
            user=member_a,
            jti=str(refresh["jti"]),
            expires_at=timezone.now() + timezone.timedelta(days=1),
            revoked_at=timezone.now(),
        )
        refresh["sid"] = str(session.id)
        return f"nexora_access={refresh.access_token}".encode()

    communicator = WebsocketCommunicator(application, "/ws/app/")
    communicator.scope["headers"] = [(b"cookie", await revoked_cookie())]
    connected, _ = await communicator.connect()
    assert connected

    frame = await communicator.receive_json_from(timeout=3)
    assert frame["type"] == "auth.error"
    assert frame["code"] == "SESSION_REVOKED"
    await communicator.disconnect()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_foreign_origin_cannot_open_an_authenticated_socket(member_a, settings):
    """Cross-site WebSocket hijacking guard.

    The same-origin policy does not apply to WebSocket handshakes, so with
    SameSite=None cookies any site could otherwise open an authenticated
    socket for a signed-in visitor.
    """
    settings.WEBSOCKET_ALLOWED_ORIGINS = ["https://nexora-eight-lilac.vercel.app"]

    cookie = await _issue_cookie(member_a)
    communicator = WebsocketCommunicator(application, "/ws/app/")
    communicator.scope["headers"] = [(b"cookie", cookie), (b"origin", b"https://evil.example")]
    connected, code = await communicator.connect()
    assert not connected
    assert code == 4403


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_the_configured_frontend_origin_is_accepted(member_a, settings):
    settings.WEBSOCKET_ALLOWED_ORIGINS = ["https://nexora-eight-lilac.vercel.app"]

    cookie = await _issue_cookie(member_a)
    communicator = WebsocketCommunicator(application, "/ws/app/")
    communicator.scope["headers"] = [
        (b"cookie", cookie),
        (b"origin", b"https://nexora-eight-lilac.vercel.app"),
    ]
    connected, _ = await communicator.connect()
    assert connected
    ready = await communicator.receive_json_from(timeout=3)
    assert ready["type"] == "connection.ready"
    await communicator.disconnect()


def test_the_frontend_websocket_path_is_routed():
    """`/ws/app/` (the path the frontend opens) must exist in Channels routing."""
    import re

    from apps.conversations.routing import websocket_urlpatterns

    assert any(re.compile(str(entry.pattern)).match("ws/app/") for entry in websocket_urlpatterns)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_connection_ready_and_ping(member_a):
    communicator = await socket(member_a)
    connected, _ = await communicator.connect()
    assert connected

    ready = await communicator.receive_json_from(timeout=3)
    assert ready["type"] == "connection.ready"
    assert ready["data"]["user_id"] == str(member_a.id)

    await communicator.send_json_to({"type": "ping", "t": 42})
    pong, _ = await drain(communicator, "pong")
    assert pong["t"] == 42
    await communicator.disconnect()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_join_is_refused_for_a_conversation_the_user_is_not_in(member_b, private_thread):
    communicator = await socket(member_b)
    await communicator.connect()
    await communicator.receive_json_from(timeout=3)

    await communicator.send_json_to({"type": "conversation.join", "conversation_id": str(private_thread.id)})
    denied, seen = await drain(communicator, "conversation.denied")
    assert denied is not None, seen
    await communicator.disconnect()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_new_message_reaches_the_other_participant(admin, member_a, private_thread):
    from apps.conversations.realtime import broadcast_new_message
    from apps.conversations.services import recipients_of, send_message

    communicator = await socket(member_a)
    await communicator.connect()
    await communicator.receive_json_from(timeout=3)
    await communicator.send_json_to({"type": "conversation.join", "conversation_id": str(private_thread.id)})
    await drain(communicator, "conversation.joined")

    @database_sync_to_async
    def publish():
        message, _ = send_message(
            user=admin, conversation=private_thread, client_id=client_id(), text="live hello"
        )
        broadcast_new_message(message, recipient_ids=recipients_of(message))
        return message

    message = await publish()
    frame, seen = await drain(communicator, "message.new")
    assert frame is not None, seen
    assert frame["data"]["id"] == str(message.id)
    assert frame["data"]["text"] == "live hello"
    await communicator.disconnect()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_typing_is_broadcast_and_read_receipts_flow_back(admin, member_a, private_thread):
    from apps.conversations.services import recipients_of, send_message
    from apps.conversations.realtime import broadcast_new_message

    listener = await socket(admin)
    await listener.connect()
    await listener.receive_json_from(timeout=3)
    await listener.send_json_to({"type": "conversation.join", "conversation_id": str(private_thread.id)})
    await drain(listener, "conversation.joined")

    sender = await socket(member_a)
    await sender.connect()
    await sender.receive_json_from(timeout=3)
    await sender.send_json_to({"type": "conversation.join", "conversation_id": str(private_thread.id)})
    await drain(sender, "conversation.joined")

    await sender.send_json_to(
        {"type": "typing", "conversation_id": str(private_thread.id), "typing": True}
    )
    typing, seen = await drain(listener, "typing.start")
    assert typing is not None, seen
    assert typing["data"]["user_id"] == str(member_a.id)
    assert await sender.receive_nothing(timeout=0.1), "the sender never receives its own typing indicator"

    @database_sync_to_async
    def publish():
        message, _ = send_message(
            user=admin, conversation=private_thread, client_id=client_id(), text="please read"
        )
        broadcast_new_message(message, recipient_ids=recipients_of(message))
        return message

    await publish()
    await drain(sender, "message.new")

    await sender.send_json_to({"type": "message.read", "conversation_id": str(private_thread.id)})
    read, seen = await drain(listener, "message.read")
    assert read is not None, seen
    assert read["data"]["user_id"] == str(member_a.id)

    await sender.disconnect()
    await listener.disconnect()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_typing_echo_is_suppressed_on_every_sender_device_but_reaches_an_independent_peer(admin, member_a, private_thread):
    listener = await socket(admin)
    first_sender_device = await socket(member_a)
    second_sender_device = await socket(member_a)
    communicators = [listener, first_sender_device, second_sender_device]

    try:
        for communicator in communicators:
            connected, _ = await communicator.connect()
            assert connected
            await communicator.receive_json_from(timeout=3)
            await communicator.send_json_to({
                "type": "conversation.join",
                "conversation_id": str(private_thread.id),
            })
            joined, seen = await drain(communicator, "conversation.joined")
            assert joined is not None, seen

        await first_sender_device.send_json_to({
            "type": "typing",
            "conversation_id": str(private_thread.id),
            "typing": True,
        })
        frame, seen = await drain(listener, "typing.start")
        assert frame is not None, seen
        assert frame["data"]["user_id"] == str(member_a.id)
        assert await first_sender_device.receive_nothing(timeout=0.1)
        assert await second_sender_device.receive_nothing(timeout=0.1)

        await first_sender_device.send_json_to({
            "type": "typing",
            "conversation_id": str(private_thread.id),
            "typing": False,
        })
        stopped, seen = await drain(listener, "typing.stop")
        assert stopped is not None, seen
        assert stopped["data"]["user_id"] == str(member_a.id)
        assert await first_sender_device.receive_nothing(timeout=0.1)
        assert await second_sender_device.receive_nothing(timeout=0.1)
    finally:
        for communicator in communicators:
            await communicator.disconnect()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_media_ready_is_broadcast(admin, member_a, private_thread, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    from apps.conversations.realtime import broadcast_media_ready
    from apps.conversations.services import send_message
    from apps.media.services import create_attachment, stage_upload
    from apps.media.validators import validate_upload
    from tests.conftest import jpeg_bytes

    communicator = await socket(member_a)
    await communicator.connect()
    await communicator.receive_json_from(timeout=3)
    await communicator.send_json_to({"type": "conversation.join", "conversation_id": str(private_thread.id)})
    await drain(communicator, "conversation.joined")

    @database_sync_to_async
    def publish():
        message, _ = send_message(
            user=admin, conversation=private_thread, client_id=client_id(), type="IMAGE"
        )
        image = jpeg_bytes()
        validated = validate_upload(image, kind="IMAGE", declared_name="p.jpg")
        # Storage is staged outside the transaction, then referenced by the row.
        attachment = create_attachment(
            message=message, validated=validated, staged=stage_upload(image, validated)
        )
        attachment.processing_state = "READY"
        attachment.save(update_fields=["processing_state"])
        broadcast_media_ready(attachment)
        return attachment

    attachment = await publish()
    frame, seen = await drain(communicator, "media.ready")
    assert frame is not None, seen
    assert frame["data"]["media"]["id"] == str(attachment.id)
    await communicator.disconnect()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_user_group_receives_unread_and_notification_events(admin, member_a, private_thread):
    from apps.conversations.realtime import broadcast_new_message
    from apps.conversations.services import recipients_of, send_message

    communicator = await socket(member_a)  # deliberately does NOT join the thread
    await communicator.connect()
    await communicator.receive_json_from(timeout=3)

    @database_sync_to_async
    def publish():
        message, _ = send_message(
            user=admin, conversation=private_thread, client_id=client_id(), text="ping"
        )
        broadcast_new_message(message, recipient_ids=recipients_of(message))

    await publish()
    seen = []
    wanted = {"conversation.updated", "notification.new", "unread.update"}
    for _ in range(12):
        if await communicator.receive_nothing(timeout=1):
            break
        seen.append(await communicator.receive_json_from(timeout=2))
        if wanted.issubset({frame.get("type") for frame in seen}):
            break
    types = [frame["type"] for frame in seen]
    # A user not currently viewing the thread still gets the notification,
    # list update, and authoritative count snapshot on their personal channel.
    assert wanted.issubset(set(types)), seen
    summary = next(frame["data"] for frame in seen if frame["type"] == "unread.update")
    assert summary["conversation_id"] == str(private_thread.id)
    assert summary["unread_count"] == 1
    assert summary["global"] == 1
    assert summary["unread_messages_total"] == 1
    assert summary["unread_total"] >= summary["unread_messages_total"]
    await communicator.disconnect()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_single_conversation_socket_rejects_outsiders(member_b, private_thread):
    """A non-participant is refused — and told so, instead of being left to
    reconnect against a 1006 with no explanation. No conversation group is
    joined and no conversation data is ever sent."""
    communicator = await socket(member_b, f"/ws/conversations/{private_thread.id}/")
    connected, _ = await communicator.connect()
    assert connected

    frame = await communicator.receive_json_from(timeout=3)
    assert frame == {"type": "auth.error", "code": "FORBIDDEN"}

    closed = await communicator.receive_output(timeout=3)
    assert closed["type"] == "websocket.close"
    assert closed["code"] == 4403
    await communicator.disconnect()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_channel_layer_is_configured():
    assert get_channel_layer() is not None
