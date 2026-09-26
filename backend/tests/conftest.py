"""Shared fixtures for the NEXORA suite."""

from __future__ import annotations

import io
import uuid

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import DeviceSession, User
from apps.conversations.services import private_conversation


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def admin(db):
    return User.objects.create_superuser("+2348030000000", "987654", full_name="Administrator")


@pytest.fixture
def member_a(db):
    return User.objects.create_user("+2348012345678", "123456", full_name="Member A", role="MEMBER")


@pytest.fixture
def member_b(db):
    return User.objects.create_user("+2348098765432", "123456", full_name="Member B", role="MEMBER")


@pytest.fixture
def users(admin, member_a, member_b):
    return admin, member_a, member_b


def authed(user) -> APIClient:
    """An API client carrying a valid cookie session + CSRF pair for ``user``."""
    from rest_framework_simplejwt.tokens import RefreshToken

    if user.credential_state != "CHANGED":
        user.credential_state = "CHANGED"
        user.save(update_fields=["credential_state"])

    refresh = RefreshToken.for_user(user)
    session = DeviceSession.objects.create(
        user=user, jti=str(refresh["jti"]), expires_at=timezone.now() + timezone.timedelta(days=1)
    )
    refresh["sid"] = str(session.id)

    client = APIClient()
    client.cookies["nexora_access"] = str(refresh.access_token)
    client.cookies["nexora_refresh"] = str(refresh)
    client.cookies["csrftoken"] = "test-csrf-token"
    client.credentials(HTTP_X_CSRFTOKEN="test-csrf-token")
    client.nexora_session = session
    return client


@pytest.fixture
def api():
    return authed


@pytest.fixture
def private_thread(admin, member_a):
    conversation, _ = private_conversation(admin, member_a)
    return conversation


@pytest.fixture
def group_thread(admin, member_a, member_b):
    from apps.conversations.models import Conversation, ConversationParticipant
    from apps.groups.models import Group, GroupMembership

    conversation = Conversation.objects.create(kind="GROUP", admin=admin)
    group = Group.objects.create(name="Field Team", creator=admin, conversation=conversation)
    GroupMembership.objects.create(group=group, user=member_a)
    ConversationParticipant.objects.bulk_create(
        [
            ConversationParticipant(conversation=conversation, user=admin),
            ConversationParticipant(conversation=conversation, user=member_a),
        ]
    )
    return group


def jpeg_bytes(size=(8, 8), name="photo.jpg") -> io.BytesIO:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, "red").save(buffer, "JPEG")
    buffer.seek(0)
    buffer.name = name
    return buffer


def client_id() -> str:
    return f"msg_{uuid.uuid4()}"
