"""
NEXORA — conversation/message serialization.

The backend is the source of truth for every field the UI renders, including
delivery state, per-message permissions and unread counts: the frontend never
invents them.
"""

from __future__ import annotations

from django.core.cache import cache
from django.utils import timezone
from rest_framework import serializers

from apps.platform_settings.services import messaging_policy

from .models import Attachment, Conversation, Message, MessageReaction


# ---------------------------------------------------------------------------
# Participants
# ---------------------------------------------------------------------------


def avatar_url(user, request=None) -> str | None:
    if not getattr(user, "avatar_key", ""):
        return None
    path = f"/api/members/{user.id}/avatar/"
    return request.build_absolute_uri(path) if request else path


def participant_payload(user, *, viewer=None, request=None, presence=None) -> dict:
    """Public projection of a user, honouring their privacy settings.

    The administrator always sees phone numbers (they manage membership);
    members see another user's phone only when that user allows it.

    ``presence`` is an optional precomputed ``{user_id: bool}`` map: list views
    pass one built with a single ``get_many`` so a page of participants does
    not cost one Redis round trip each.
    """
    if user is None:
        return None
    is_admin_viewer = getattr(viewer, "role", None) == "ADMIN"
    show_phone = is_admin_viewer or user.show_phone or (viewer is not None and viewer.id == user.id)
    presence_visible = user.show_last_seen or is_admin_viewer
    if presence is not None:
        online = bool(presence.get(str(user.id), False)) if presence_visible else None
    else:
        online = bool(cache.get(f"presence:{user.id}", 0)) if presence_visible else None
    return {
        "id": str(user.id),
        "display_name": user.full_name,
        "phone": user.phone if show_phone else "",
        "avatar_url": avatar_url(user, request),
        "is_admin": user.role == "ADMIN",
        "is_active": user.is_active,
        "presence_visible": presence_visible,
        "online": online,
        "last_seen": user.last_seen.isoformat() if (presence_visible and user.last_seen) else None,
    }


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


class AttachmentSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()
    thumbnail_url = serializers.SerializerMethodField()
    optimized_url = serializers.SerializerMethodField()
    download_url = serializers.SerializerMethodField()
    name = serializers.CharField(source="original_name", read_only=True)
    status = serializers.CharField(source="processing_state", read_only=True)
    duration = serializers.SerializerMethodField()

    class Meta:
        model = Attachment
        fields = [
            "id",
            "name",
            "mime_type",
            "size",
            "duration",
            "duration_ms",
            "width",
            "height",
            "processing_state",
            "status",
            "url",
            "thumbnail_url",
            "optimized_url",
            "download_url",
        ]
        read_only_fields = fields

    def _url(self, obj, variant="original", download=False):
        request = self.context.get("request")
        path = f"/api/media/{obj.id}/"
        params = []
        if variant != "original":
            params.append(f"variant={variant}")
        if download:
            params.append("download=1")
        if params:
            path = f"{path}?{'&'.join(params)}"
        return request.build_absolute_uri(path) if request else path

    def get_url(self, obj):
        return self._url(obj)

    def get_download_url(self, obj):
        return self._url(obj, download=True)

    def get_thumbnail_url(self, obj):
        return self._url(obj, "thumbnail") if obj.thumbnail_key else None

    def get_optimized_url(self, obj):
        return self._url(obj, "optimized") if obj.optimized_key else None

    def get_duration(self, obj):
        return round(obj.duration_ms / 1000, 2) if obj.duration_ms else None


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


class ReactionSerializer(serializers.ModelSerializer):
    user_id = serializers.CharField(source="user.id", read_only=True)

    class Meta:
        model = MessageReaction
        fields = ["id", "message", "user_id", "reaction", "created_at"]
        read_only_fields = fields


class MessageCreateSerializer(serializers.Serializer):
    """Text messages. Media messages are validated by the media pipeline."""

    client_id = serializers.CharField(max_length=64)
    type = serializers.ChoiceField(choices=["TEXT"], default="TEXT", required=False)
    kind = serializers.CharField(required=False, write_only=True)
    text = serializers.CharField(allow_blank=False, trim_whitespace=False)
    reply_to = serializers.PrimaryKeyRelatedField(
        queryset=Message.objects.all(), required=False, allow_null=True
    )

    def validate(self, attrs):
        attrs.pop("kind", None)
        attrs["type"] = "TEXT"
        return attrs


class ReplyPreviewSerializer(serializers.ModelSerializer):
    author_name = serializers.CharField(source="sender.full_name", read_only=True)
    preview = serializers.SerializerMethodField()
    kind = serializers.CharField(source="type", read_only=True)

    class Meta:
        model = Message
        fields = ["id", "author_name", "preview", "kind"]
        read_only_fields = fields

    def get_preview(self, obj):
        if obj.deleted_at:
            return ""
        return (obj.text or "")[:140]


class MessageSerializer(serializers.ModelSerializer):
    sender = serializers.SerializerMethodField()
    sender_id = serializers.CharField(source="sender.id", read_only=True)
    conversation_id = serializers.CharField(source="conversation.id", read_only=True)
    attachment = AttachmentSerializer(read_only=True)
    media = AttachmentSerializer(source="attachment", read_only=True)
    reply_to = ReplyPreviewSerializer(read_only=True)
    delivery = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    reactions = serializers.SerializerMethodField()
    is_deleted = serializers.SerializerMethodField()
    is_edited = serializers.SerializerMethodField()
    can_edit = serializers.SerializerMethodField()
    can_delete_for_everyone = serializers.SerializerMethodField()
    can_react = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = [
            "id",
            "conversation",
            "conversation_id",
            "sender",
            "sender_id",
            "client_id",
            "type",
            "text",
            "reply_to",
            "attachment",
            "media",
            "reactions",
            "delivery",
            "status",
            "is_deleted",
            "is_edited",
            "can_edit",
            "can_delete_for_everyone",
            "can_react",
            "created_at",
            "updated_at",
            "edited_at",
            "deleted_at",
        ]
        read_only_fields = fields

    # -- helpers ------------------------------------------------------------

    @property
    def _viewer(self):
        request = self.context.get("request")
        return getattr(request, "user", None) if request else self.context.get("viewer")

    def get_sender(self, obj):
        return participant_payload(
            obj.sender,
            viewer=self._viewer,
            request=self.context.get("request"),
            presence=self.context.get("presence_online_map"),
        )

    def get_reactions(self, obj):
        return [
            {"user_id": str(r.user_id), "reaction": r.reaction}
            for r in obj.reactions.all()
        ]

    def get_delivery(self, obj):
        receipts = list(obj.receipts.all())
        total = len(receipts)
        read = sum(1 for r in receipts if r.read_at)
        delivered = sum(1 for r in receipts if r.delivered_at)
        state = "READ" if total and read == total else "DELIVERED" if total and delivered == total else "SENT"
        return {"state": state, "recipients": total, "delivered": delivered, "read": read}

    def get_status(self, obj):
        """Lower-case status the frontend store understands directly."""
        return self.get_delivery(obj)["state"].lower()

    def get_is_deleted(self, obj):
        return bool(obj.deleted_at)

    def get_is_edited(self, obj):
        return bool(obj.edited_at)

    def get_can_edit(self, obj):
        viewer = self._viewer
        policy = messaging_policy()
        if not viewer or obj.sender_id != viewer.id or obj.type != "TEXT" or obj.deleted_at:
            return False
        if not policy["allow_message_editing"]:
            return False
        return timezone.now() - obj.created_at <= timezone.timedelta(
            minutes=policy["message_edit_window_minutes"]
        )

    def get_can_delete_for_everyone(self, obj):
        viewer = self._viewer
        policy = messaging_policy()
        if not viewer or obj.sender_id != viewer.id or obj.deleted_at:
            return False
        if not policy["allow_delete_everyone"]:
            return False
        return timezone.now() - obj.created_at <= timezone.timedelta(
            minutes=policy["message_delete_window_minutes"]
        )

    def get_can_react(self, obj):
        policy = messaging_policy()
        if not policy["allow_reactions"] or obj.deleted_at:
            return False
        viewer = self._viewer
        conversation = obj.conversation
        if conversation.kind == Conversation.Kind.GROUP and getattr(viewer, "role", None) == "MEMBER":
            group = getattr(conversation, "group", None)
            return bool(group and group.members_can_react)
        return True


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


class ConversationSerializer(serializers.ModelSerializer):
    type = serializers.SerializerMethodField()
    name = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    group_id = serializers.SerializerMethodField()
    counterpart = serializers.SerializerMethodField()
    participants = serializers.SerializerMethodField()
    member_count = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()
    unread = serializers.SerializerMethodField()
    can_send = serializers.SerializerMethodField()
    read_only_reason = serializers.SerializerMethodField()
    is_admin_thread = serializers.SerializerMethodField()
    last_activity_at = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = Conversation
        fields = [
            "id",
            "kind",
            "type",
            "name",
            "description",
            "group_id",
            "counterpart",
            "participants",
            "member_count",
            "last_message",
            "unread",
            "can_send",
            "read_only_reason",
            "is_admin_thread",
            "is_active",
            "last_activity_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    @property
    def _viewer(self):
        request = self.context.get("request")
        viewer = getattr(request, "user", None) if request else None
        if viewer is None and self.context.get("for_user_id"):
            from apps.accounts.models import User

            viewer = User.objects.filter(id=self.context["for_user_id"]).first()
        return viewer

    @property
    def _presence(self):
        """Precomputed online map for the whole page (list views), if any."""
        return self.context.get("presence_online_map")

    def _group(self, obj):
        return getattr(obj, "group", None)

    def get_type(self, obj):
        return "group" if obj.kind == Conversation.Kind.GROUP else "direct"

    def get_name(self, obj):
        group = self._group(obj)
        if group:
            return group.name
        viewer = self._viewer
        other = obj.member if viewer and viewer.id == obj.admin_id else obj.admin
        return other.full_name if other else ""

    def get_description(self, obj):
        group = self._group(obj)
        return group.description if group else ""

    def get_group_id(self, obj):
        group = self._group(obj)
        return str(group.id) if group else None

    def get_counterpart(self, obj):
        if obj.kind != Conversation.Kind.PRIVATE:
            return None
        viewer = self._viewer
        other = obj.member if viewer and viewer.id == obj.admin_id else obj.admin
        return participant_payload(
            other, viewer=viewer, request=self.context.get("request"), presence=self._presence
        )

    def get_participants(self, obj):
        viewer = self._viewer
        request = self.context.get("request")
        if obj.kind == Conversation.Kind.GROUP:
            group = self._group(obj)
            if group and viewer and viewer.role != "ADMIN" and not group.members_can_view_members:
                return []
        return [
            participant_payload(p.user, viewer=viewer, request=request, presence=self._presence)
            for p in obj.participants.all()
            if p.is_active
        ]

    def get_member_count(self, obj):
        return sum(1 for p in obj.participants.all() if p.is_active)

    def get_last_message(self, obj):
        # List views pre-fetch the page's latest messages in one query and
        # pass them through the context. When that map is present, a missing
        # entry legitimately means "no messages yet" — no per-conversation
        # query is needed for empty conversations either.
        if "latest_messages" in self.context:
            message = self.context["latest_messages"].get(obj.id)
            return MessageSerializer(message, context=self.context).data if message else None
        message = getattr(obj, "latest_message", None)
        if message is None:
            message = obj.messages.order_by("-created_at").first()
        return MessageSerializer(message, context=self.context).data if message else None

    def get_unread(self, obj):
        viewer = self._viewer
        if not viewer:
            return 0
        cached = getattr(obj, "unread_count", None)
        if cached is not None:
            return cached
        from .models import MessageReceipt

        return MessageReceipt.objects.filter(
            message__conversation=obj, recipient=viewer, read_at__isnull=True
        ).count()

    def get_can_send(self, obj):
        if not obj.is_active:
            return False
        viewer = self._viewer
        group = self._group(obj)
        if group and viewer and viewer.role == "MEMBER":
            return bool(group.is_active and group.members_can_send)
        return True

    def get_read_only_reason(self, obj):
        if not obj.is_active:
            return "This conversation is archived."
        if not self.get_can_send(obj):
            return "Only the administrator can post in this group."
        return ""

    def get_is_admin_thread(self, obj):
        return obj.kind == Conversation.Kind.PRIVATE
