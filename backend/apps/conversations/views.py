"""
NEXORA — conversation, message, search and unread endpoints.

Every queryset is scoped to the requesting user's participation, so an
unauthorized UUID yields 404 rather than data. Media messages are created in
the same atomic operation as their attachment, so a half-written message can
never reach a client.
"""

from __future__ import annotations

import logging

from django.db import models, transaction
from django.db.models import Count, OuterRef, Prefetch, Q, Subquery
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import viewsets
from rest_framework.decorators import action, api_view, parser_classes, throttle_classes
from rest_framework.exceptions import PermissionDenied, Throttled, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.accounts.models import User
from apps.core.throttles import MessageThrottle, SearchThrottle, UploadThrottle

from . import realtime
from .models import (
    Attachment,
    Conversation,
    ConversationParticipant,
    Message,
    MessageDeletion,
    MessageReaction,
    MessageReceipt,
)
from .serializers import ConversationSerializer, MessageCreateSerializer, MessageSerializer, ReactionSerializer
from .services import can_access, caption_for, private_conversation, recipients_of, send_message, unread_map

#: Full upload lifecycle, so a failure is traceable in the Render log.
upload_log = logging.getLogger("nexora.upload")

MESSAGE_PREFETCH = ("receipts", "reactions")
MESSAGE_SELECT = ("sender", "reply_to", "reply_to__sender", "attachment", "conversation")


def envelope(message, data=None, status=200):
    return Response({"success": True, "message": message, "data": data if data is not None else {}}, status=status)


class ConversationViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ConversationSerializer

    def get_queryset(self):
        user = self.request.user
        latest = Message.objects.filter(conversation=OuterRef("pk")).order_by("-created_at")
        unread = (
            MessageReceipt.objects.filter(message__conversation=OuterRef("pk"), recipient=user, read_at__isnull=True)
            .values("message__conversation")
            .annotate(total=Count("id"))
            .values("total")
        )
        return (
            Conversation.objects.filter(
                participants__user=user, participants__is_active=True, is_active=True
            )
            .select_related("admin", "member", "group")
            .prefetch_related(Prefetch("participants__user"))
            .annotate(
                last_message_at=Subquery(latest.values("created_at")[:1]),
                # The page's last messages are then fetched with ONE query in
                # list() instead of one query per conversation.
                latest_message_id=Subquery(latest.values("id")[:1]),
                # Coalesce: an empty conversation annotates to 0, never NULL,
                # so the serializer's annotation check cannot fall through to
                # a per-conversation COUNT query.
                unread_count=models.functions.Coalesce(Subquery(unread), 0),
            )
            .distinct()
            .order_by(models.F("last_message_at").desc(nulls_last=True), "-updated_at")
        )

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "request": self.request}

    def list(self, request, *args, **kwargs):
        """The conversation list with zero per-row queries.

        The queryset already annotates ``last_message_at`` and ``unread_count``
        (subqueries, one pass). Here the page additionally receives:

        * ``latest_messages``  — one query for the whole page's last messages
          (the serializer used to issue one query per conversation);
        * ``presence_online_map`` — one ``cache.get_many`` for every
          participant/counterpart on the page (used to be one Redis round
          trip per participant).
        """
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        rows = page if page is not None else list(queryset)

        latest_messages = {}
        latest_ids = [row.latest_message_id for row in rows if row.latest_message_id]
        if latest_ids:
            messages = (
                Message.objects.filter(id__in=latest_ids)
                .select_related(*MESSAGE_SELECT)
                .prefetch_related(*MESSAGE_PREFETCH)
            )
            latest_messages = {m.conversation_id: m for m in messages}

        presence_map = None
        user_ids = set()
        for row in rows:
            for participant in row.participants.all():
                user_ids.add(participant.user_id)
            if row.kind == Conversation.Kind.PRIVATE:
                if row.admin_id:
                    user_ids.add(row.admin_id)
                if row.member_id:
                    user_ids.add(row.member_id)
        if user_ids:
            from apps.accounts.serializers import presence_online_map

            presence_map = presence_online_map(user_ids)

        context = {
            **self.get_serializer_context(),
            "latest_messages": latest_messages,
            "presence_online_map": presence_map or {},
        }
        serializer = self.get_serializer(rows, many=True, context=context)
        return self.get_paginated_response(serializer.data)

    def create(self, request, *args, **kwargs):
        """Open (or reuse) the private conversation with one counterpart."""
        target_id = request.data.get("participant") or request.data.get("member") or request.data.get("user")
        target = User.objects.filter(id=target_id, is_active=True).first() if target_id else None
        if not target:
            return Response(
                {"success": False, "message": "Invalid participant.", "code": "INVALID_PARTICIPANT", "errors": {}},
                status=400,
            )
        conversation, created = private_conversation(request.user, target)
        if created:
            realtime.broadcast_conversation_created(
                conversation, participant_ids=[conversation.admin_id, conversation.member_id], request=request
            )
        data = ConversationSerializer(conversation, context={"request": request}).data
        return envelope("Conversation ready", data, status=201 if created else 200)

    # -- messages -----------------------------------------------------------

    @action(detail=True, methods=["get", "post"], parser_classes=[JSONParser, MultiPartParser, FormParser])
    def messages(self, request, pk=None):
        conversation = self.get_object()
        if request.method == "GET":
            queryset = (
                conversation.messages.select_related(*MESSAGE_SELECT)
                .prefetch_related(*MESSAGE_PREFETCH)
                .exclude(self_deletions__user=request.user)
                .order_by("-created_at")
            )
            page = self.paginate_queryset(queryset)
            from apps.accounts.serializers import presence_online_map

            context = {
                "request": request,
                # One presence read for the page's senders instead of per row.
                "presence_online_map": presence_online_map(
                    {message.sender_id for message in page or []}
                ),
            }
            return self.get_paginated_response(
                MessageSerializer(page, many=True, context=context).data
            )

        if request.FILES.get("file"):
            return create_media_message(request, conversation)
        return create_text_message(request, conversation)

    @action(detail=True, methods=["post"])
    def read(self, request, pk=None):
        conversation = self.get_object()
        now = timezone.now()
        queryset = MessageReceipt.objects.filter(
            message__conversation=conversation, recipient=request.user, read_at__isnull=True
        )
        message_ids = [str(x) for x in queryset.values_list("message_id", flat=True)]
        queryset.update(delivered_at=now, read_at=now)
        ConversationParticipant.objects.filter(conversation=conversation, user=request.user).update(
            last_read_at=now
        )
        if message_ids:
            realtime.broadcast_receipts(
                conversation.id, user_id=request.user.id, state="read", message_ids=message_ids, timestamp=now
            )
        realtime.emit_to_users(
            [request.user.id], "unread.update", {"conversation_id": str(conversation.id), "unread": 0}
        )
        return envelope("Read state updated", {"read": len(message_ids)})

    @action(detail=True, methods=["post"])
    def typing(self, request, pk=None):
        """REST fallback for clients without a live socket."""
        conversation = self.get_object()
        realtime.broadcast_typing(
            conversation.id, user_id=request.user.id, typing=bool(request.data.get("typing"))
        )
        return envelope("Typing state broadcast")

    @action(detail=True, methods=["get"])
    def media(self, request, pk=None):
        """Attachments shared in this conversation (authorized by membership)."""
        conversation = self.get_object()
        queryset = (
            conversation.messages.filter(attachment__isnull=False, deleted_at__isnull=True)
            .exclude(self_deletions__user=request.user)
            .select_related(*MESSAGE_SELECT)
            .prefetch_related(*MESSAGE_PREFETCH)
            .order_by("-created_at")
        )
        kind = str(request.query_params.get("type", "")).upper()
        if kind:
            if kind not in Message.Type.values:
                raise ValidationError("Unsupported message type.")
            queryset = queryset.filter(type=kind)
        page = self.paginate_queryset(queryset)
        return self.get_paginated_response(
            MessageSerializer(page, many=True, context={"request": request}).data
        )

    @action(detail=False, methods=["get"], url_path="unread-summary")
    def unread_summary(self, request):
        return envelope("Unread summary retrieved", _unread_payload(request.user))


# ---------------------------------------------------------------------------
# Message creation
# ---------------------------------------------------------------------------


def enforce_throttle(throttle, request) -> None:
    """Apply a hand-invoked throttle and actually reject when it trips.

    ``SimpleRateThrottle.allow_request`` only *reports* the verdict; it is
    ``APIView.check_throttles`` that turns a ``False`` into HTTP 429. These
    two endpoints are dispatched from a shared router action rather than
    their own APIView, so the conversion has to happen here. Previously the
    boolean was discarded, which left message and upload rate limiting
    completely unenforced.
    """
    if not throttle.allow_request(request, None):
        raise Throttled(wait=throttle.wait())


@transaction.atomic
def create_text_message(request, conversation):
    serializer = MessageCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    enforce_throttle(MessageThrottle(), request)

    message, created = send_message(user=request.user, conversation=conversation, **serializer.validated_data)
    message = _reload(message)
    if created:
        realtime.broadcast_new_message(message, request=request, recipient_ids=recipients_of(message))
    data = MessageSerializer(message, context={"request": request}).data
    return envelope("Message sent", data, status=201 if created else 200)


def create_media_message(request, conversation):
    """Multipart send: validate → store → message + attachment → broadcast."""
    if not can_access(request.user, conversation):
        raise PermissionDenied()
    enforce_throttle(UploadThrottle(), request)

    fileobj = request.FILES.get("file")
    if fileobj is None:
        raise ValidationError({"file": "A file is required."})
    kind = str(request.data.get("kind", "")).upper()
    kind = {"IMAGE": "IMAGE", "VIDEO": "VIDEO", "VOICE": "VOICE", "AUDIO": "VOICE"}.get(kind, "")
    if not kind:
        raise ValidationError({"kind": "kind must be image, video or voice."})

    return create_message_from_stored_upload(
        request,
        conversation=conversation,
        fileobj=fileobj,
        kind=kind,
        client_id=request.data.get("client_id", ""),
        declared_name=getattr(fileobj, "name", ""),
        caption=request.data.get("caption", ""),
        reply_to_id=request.data.get("reply_to"),
        poster=request.FILES.get("poster"),
        declared_duration=request.data.get("duration"),
    )


def create_message_from_stored_upload(
    request,
    *,
    conversation,
    fileobj,
    kind,
    client_id,
    declared_name="",
    caption="",
    reply_to_id=None,
    poster=None,
    declared_duration=None,
):
    from apps.media.services import create_attachment, stage_upload
    from apps.media.validators import validate_upload

    client_id = str(client_id or "").strip()
    if not client_id:
        raise ValidationError({"client_id": "A client_id is required for idempotent uploads."})

    # Idempotency first: a retried upload must not re-store the file.
    existing = Message.objects.filter(sender=request.user, client_id=client_id).first()
    if existing:
        if existing.conversation_id != conversation.id:
            raise ValidationError({"client_id": "This idempotency key belongs to another conversation."})
        return envelope("Message already sent", MessageSerializer(_reload(existing), context={"request": request}).data)

    try:
        duration_ms = int(float(declared_duration) * 1000) if declared_duration else None
    except (TypeError, ValueError):
        duration_ms = None

    validated = validate_upload(fileobj, kind=kind, declared_name=declared_name, duration_ms=duration_ms)

    reply_to = None
    if reply_to_id:
        reply_to = Message.objects.filter(id=reply_to_id, conversation=conversation).first()
        if reply_to is None:
            raise ValidationError({"reply_to": "The reply target must belong to this conversation."})

    # Stage the bytes to object storage BEFORE opening the transaction. A
    # large PUT can take tens of seconds; doing it inside the transaction
    # pinned a pooled MySQL connection and its locks for the whole transfer.
    upload_log.info(
        "upload staging kind=%s size=%s mime=%s conversation=%s",
        kind,
        validated.size,
        validated.mime_type,
        conversation.id,
    )
    staged = stage_upload(fileobj, validated, poster=poster)

    try:
        with transaction.atomic():
            message, created = send_message(
                user=request.user,
                conversation=conversation,
                client_id=client_id,
                text="",
                type=kind,
                reply_to=reply_to,
            )
            if created:
                create_attachment(message=message, validated=validated, staged=staged)
                caption_for(message, caption)
        if not created:
            # Lost an idempotency race: the staged object is unreferenced.
            staged.discard()
    except Exception as exc:
        # Never leave an object behind that no row points at, and never let
        # the client believe a failed send succeeded.
        staged.discard()
        upload_log.error(
            "upload failed after staging kind=%s size=%s conversation=%s (%s: %s)",
            kind,
            validated.size,
            conversation.id,
            exc.__class__.__name__,
            exc,
        )
        raise

    message = _reload(message)
    upload_log.info("upload committed message=%s kind=%s size=%s", message.id, kind, validated.size)
    if created:
        realtime.broadcast_new_message(message, request=request, recipient_ids=recipients_of(message))
    return envelope(
        "Message sent",
        MessageSerializer(message, context={"request": request}).data,
        status=201 if created else 200,
    )


def _reload(message):
    return (
        Message.objects.select_related(*MESSAGE_SELECT)
        .prefetch_related(*MESSAGE_PREFETCH)
        .get(pk=message.pk)
    )


# ---------------------------------------------------------------------------
# Single message operations
# ---------------------------------------------------------------------------


def _authorized_message(request, message_id) -> Message:
    message = (
        Message.objects.select_related(*MESSAGE_SELECT)
        .prefetch_related(*MESSAGE_PREFETCH)
        .filter(id=message_id)
        .first()
    )
    if message is None or not can_access(request.user, message.conversation):
        # Indistinguishable from "does not exist" — no UUID probing.
        from django.http import Http404

        raise Http404
    return message


@api_view(["GET", "PATCH", "DELETE"])
def message_detail(request, message_id):
    from apps.platform_settings.services import messaging_policy

    message = _authorized_message(request, message_id)

    if request.method == "GET":
        return envelope("Message retrieved", MessageSerializer(message, context={"request": request}).data)

    if request.method == "DELETE":
        scope = str(request.data.get("scope") or request.query_params.get("scope") or "self").lower()
        if scope == "self":
            MessageDeletion.objects.get_or_create(message=message, user=request.user)
            return envelope("Message removed for you")
        if scope != "everyone":
            raise ValidationError({"scope": "Invalid deletion scope."})
        if message.sender_id != request.user.id:
            raise PermissionDenied("Only the sender can delete a message for everyone.")
        policy = messaging_policy()
        if not policy["allow_delete_everyone"]:
            raise PermissionDenied("Delete for everyone is disabled for this organization.")
        if timezone.now() - message.created_at > timezone.timedelta(
            minutes=policy["message_delete_window_minutes"]
        ):
            raise ValidationError("The deletion window has expired.")
        with transaction.atomic():
            message.text = ""
            message.deleted_at = timezone.now()
            message.save(update_fields=["text", "deleted_at", "updated_at"])
        realtime.broadcast_message_deleted(message)
        return envelope("Message deleted")

    # PATCH — edit
    policy = messaging_policy()
    if message.sender_id != request.user.id or message.type != "TEXT" or message.deleted_at:
        raise PermissionDenied("This message cannot be edited.")
    if not policy["allow_message_editing"]:
        raise PermissionDenied("Message editing is disabled for this organization.")
    if timezone.now() - message.created_at > timezone.timedelta(
        minutes=policy["message_edit_window_minutes"]
    ):
        raise ValidationError("The editing window has expired.")
    text = str(request.data.get("text", "")).strip()
    if not text:
        raise ValidationError({"text": "Message text is required."})
    if len(text) > policy["max_message_length"]:
        raise ValidationError({"text": "Message exceeds the configured length limit."})
    message.text = text
    message.edited_at = timezone.now()
    message.save(update_fields=["text", "edited_at", "updated_at"])
    message = _reload(message)
    realtime.broadcast_message_updated(message, request=request)
    return envelope("Message edited", MessageSerializer(message, context={"request": request}).data)


@api_view(["POST", "DELETE"])
def reaction(request, message_id):
    from apps.platform_settings.services import messaging_policy

    allowed = {"LIKE", "LOVE", "LAUGH", "WOW", "SAD", "THANKS"}
    message = _authorized_message(request, message_id)

    if not messaging_policy()["allow_reactions"]:
        raise PermissionDenied("Reactions are disabled for this organization.")
    if (
        message.conversation.kind == Conversation.Kind.GROUP
        and request.user.role == "MEMBER"
        and not getattr(message.conversation, "group", None).members_can_react
    ):
        raise PermissionDenied("Members cannot react in this group.")

    value = str(request.data.get("reaction") or request.query_params.get("reaction") or "").upper()
    if value not in allowed:
        raise ValidationError({"reaction": "Unsupported reaction."})

    if request.method == "POST":
        obj, _ = MessageReaction.objects.get_or_create(message=message, user=request.user, reaction=value)
        realtime.broadcast_reaction(message, user_id=request.user.id, reaction=value)
        return envelope("Reaction saved", ReactionSerializer(obj).data, status=201)

    MessageReaction.objects.filter(message=message, user=request.user, reaction=value).delete()
    realtime.broadcast_reaction(message, user_id=request.user.id, reaction=value, removed=True)
    return envelope("Reaction removed")


@api_view(["POST"])
def message_status(request):
    """Authoritative delivery state for a set of message ids (reconciliation)."""
    ids = [str(x) for x in (request.data.get("ids") or [])][:200]
    if not ids:
        raise ValidationError({"ids": "Provide up to 200 message ids."})
    messages = (
        Message.objects.filter(
            id__in=ids,
            conversation__participants__user=request.user,
            conversation__participants__is_active=True,
        )
        .select_related(*MESSAGE_SELECT)
        .prefetch_related(*MESSAGE_PREFETCH)
        .distinct()
    )
    serializer = MessageSerializer(messages, many=True, context={"request": request})
    return envelope("Message status retrieved", {"results": serializer.data})


# ---------------------------------------------------------------------------
# Search + unread
# ---------------------------------------------------------------------------


@api_view(["GET"])
@throttle_classes([SearchThrottle])
def search_messages(request):
    query = str(request.query_params.get("q", "")).strip()
    kind = str(request.query_params.get("type", "")).upper()
    date_from = request.query_params.get("date_from")
    date_to = request.query_params.get("date_to")

    if query and len(query) < 2:
        raise ValidationError({"q": "Search queries must contain at least two characters."})
    if not any((query, kind, date_from, date_to)):
        raise ValidationError("Provide at least one search filter.")

    # Scoped to the caller's own conversations: search can never be used to
    # discover users or threads the caller is not part of.
    queryset = Message.objects.filter(
        conversation__participants__user=request.user,
        conversation__participants__is_active=True,
        deleted_at__isnull=True,
    ).exclude(self_deletions__user=request.user)

    if query:
        text_filter = (
            Q(text__icontains=query)
            | Q(sender__full_name__icontains=query)
            | Q(conversation__group__name__icontains=query)
        )
        if request.user.role == "ADMIN":
            text_filter |= Q(sender__phone__icontains=query)
        queryset = queryset.filter(text_filter)

    if kind:
        if kind not in Message.Type.values:
            raise ValidationError({"type": "Unsupported message type."})
        queryset = queryset.filter(type=kind)

    for value, lookup, field in ((date_from, "created_at__date__gte", "date_from"), (date_to, "created_at__date__lte", "date_to")):
        if value:
            parsed = parse_date(value)
            if not parsed:
                raise ValidationError({field: "Use YYYY-MM-DD."})
            queryset = queryset.filter(**{lookup: parsed})

    queryset = (
        queryset.select_related(*MESSAGE_SELECT).prefetch_related(*MESSAGE_PREFETCH).distinct().order_by("-created_at")
    )
    paginator = ConversationViewSet.pagination_class()
    page = paginator.paginate_queryset(queryset, request)
    return paginator.get_paginated_response(
        MessageSerializer(page, many=True, context={"request": request}).data
    )


def _unread_payload(user) -> dict:
    from apps.notifications.models import Notification

    conversations = unread_map(user)
    return {
        "global": sum(conversations.values()),
        "total": sum(conversations.values()),
        "conversations": conversations,
        "notifications": Notification.objects.filter(recipient=user, read_at__isnull=True).count(),
    }


@api_view(["GET"])
def unread_counts(request):
    return envelope("Unread counts retrieved", _unread_payload(request.user))
