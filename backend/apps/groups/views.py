"""
NEXORA — group administration.

Only an administrator creates groups, chooses their members and changes
membership. A member can see only the groups they were explicitly added to,
and (when permitted) leave one. Every rule is enforced against the database,
never against a hidden UI control.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.accounts.models import User
from apps.accounts.serializers import UserSerializer
from apps.audit.services import record
from apps.conversations import realtime
from apps.conversations.models import Conversation, ConversationParticipant

from .models import Group, GroupMembership
from .serializers import GroupSerializer


def envelope(message, data=None, status=200):
    return Response({"success": True, "message": message, "data": data if data is not None else {}}, status=status)


class GroupViewSet(viewsets.ModelViewSet):
    serializer_class = GroupSerializer
    http_method_names = ["get", "post", "patch", "delete"]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "request": self.request}

    def get_queryset(self):
        queryset = (
            Group.objects.select_related("creator", "conversation")
            .prefetch_related(Prefetch("memberships", queryset=GroupMembership.objects.select_related("user")))
            .annotate(active_member_count=Count("memberships", filter=Q(memberships__is_active=True)))
        )
        include_archived = str(self.request.query_params.get("archived", "")).lower() in ("1", "true")
        if not include_archived:
            queryset = queryset.filter(is_active=True)
        if self.request.user.role != "ADMIN":
            queryset = queryset.filter(memberships__user=self.request.user, memberships__is_active=True)
        return queryset.distinct().order_by("-updated_at")

    def _require_admin(self):
        if self.request.user.role != "ADMIN":
            raise PermissionDenied()

    # -- lifecycle ----------------------------------------------------------

    def create(self, request, *args, **kwargs):
        self._require_admin()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        member_ids = serializer.validated_data.pop("member_ids", [])
        members = self._resolve_members(member_ids)

        with transaction.atomic():
            conversation = Conversation.objects.create(kind=Conversation.Kind.GROUP, admin=request.user)
            group = serializer.save(creator=request.user, conversation=conversation)
            GroupMembership.objects.bulk_create([GroupMembership(group=group, user=u) for u in members])
            ConversationParticipant.objects.bulk_create(
                [ConversationParticipant(conversation=conversation, user=request.user)]
                + [ConversationParticipant(conversation=conversation, user=u) for u in members]
            )
            record(request.user, "GROUP_CREATED", group, request, {"member_count": len(members)})

        realtime.broadcast_conversation_created(
            conversation, participant_ids=[request.user.id, *[u.id for u in members]], request=request
        )
        realtime.broadcast_group_membership(group, added=[u.id for u in members])
        return envelope("Group created", GroupSerializer(group, context={"request": request}).data, status=201)

    def partial_update(self, request, *args, **kwargs):
        self._require_admin()
        group = self.get_object()
        serializer = self.get_serializer(group, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        record(request.user, "GROUP_UPDATED", group, request, {"fields": sorted(serializer.validated_data)})
        realtime.emit_to_conversation(
            group.conversation_id,
            "conversation.updated",
            {"conversation_id": str(group.conversation_id), "name": group.name},
        )
        return envelope("Group updated", GroupSerializer(group, context={"request": request}).data)

    def destroy(self, request, *args, **kwargs):
        """Groups are archived, never hard-deleted.

        Message history is evidence: destroying it would break the audit trail
        and orphan attachments. DELETE therefore archives, which is what the
        UI means by "delete group".
        """
        return self._set_archived(request, archived=True)

    # -- membership ---------------------------------------------------------

    def _resolve_members(self, ids):
        ids = [str(x) for x in (ids or [])]
        if not ids:
            return []
        members = list(User.objects.filter(id__in=ids, role="MEMBER", is_active=True))
        if len(members) != len(set(ids)):
            raise ValidationError({"member_ids": "Every entry must be an active member."})
        return members

    @action(detail=True, methods=["get", "post", "delete"])
    def members(self, request, pk=None):
        group = self.get_object()

        if request.method == "GET":
            if request.user.role != "ADMIN" and not group.members_can_view_members:
                raise PermissionDenied("The member list is hidden in this group.")
            users = User.objects.filter(
                group_memberships__group=group, group_memberships__is_active=True
            ).order_by("full_name")
            return envelope(
                "Members retrieved", UserSerializer(users, many=True, context={"request": request}).data
            )

        self._require_admin()
        ids = request.data.get("member_ids") or request.data.get("members") or []
        members = self._resolve_members(ids)
        if not members:
            raise ValidationError({"member_ids": "Select at least one member."})

        with transaction.atomic():
            if request.method == "POST":
                GroupMembership.objects.bulk_create(
                    [GroupMembership(group=group, user=u) for u in members], ignore_conflicts=True
                )
                GroupMembership.objects.filter(group=group, user__in=members).update(is_active=True)
                ConversationParticipant.objects.bulk_create(
                    [ConversationParticipant(conversation=group.conversation, user=u) for u in members],
                    ignore_conflicts=True,
                )
                ConversationParticipant.objects.filter(
                    conversation=group.conversation, user__in=members
                ).update(is_active=True)
                action_name = "GROUP_MEMBERS_ADDED"
            else:
                GroupMembership.objects.filter(group=group, user__in=members).update(is_active=False)
                ConversationParticipant.objects.filter(
                    conversation=group.conversation, user__in=members
                ).update(is_active=False)
                action_name = "GROUP_MEMBERS_REMOVED"
            record(request.user, action_name, group, request, {"member_ids": [str(u.id) for u in members]})

        ids = [u.id for u in members]
        if request.method == "POST":
            realtime.broadcast_conversation_created(group.conversation, participant_ids=ids, request=request)
            realtime.broadcast_group_membership(group, added=ids)
        else:
            realtime.broadcast_group_membership(group, removed=ids)
        return envelope("Membership updated", {"member_ids": [str(x) for x in ids]})

    @action(detail=True, methods=["delete"], url_path=r"members/(?P<member_id>[0-9a-f-]+)")
    def remove_member(self, request, pk=None, member_id=None):
        self._require_admin()
        group = self.get_object()
        member = User.objects.filter(id=member_id).first()
        if not member:
            raise ValidationError({"member_id": "Unknown member."})
        with transaction.atomic():
            GroupMembership.objects.filter(group=group, user=member).update(is_active=False)
            ConversationParticipant.objects.filter(conversation=group.conversation, user=member).update(
                is_active=False
            )
            record(request.user, "GROUP_MEMBERS_REMOVED", group, request, {"member_ids": [str(member.id)]})
        realtime.broadcast_group_membership(group, removed=[member.id])
        return envelope("Member removed from group")

    @action(detail=True, methods=["post"])
    def leave(self, request, pk=None):
        group = self.get_object()
        if request.user.role != "MEMBER":
            raise PermissionDenied("Administrators cannot leave their own group.")
        if not group.members_can_leave:
            raise PermissionDenied("Members cannot leave this group.")
        with transaction.atomic():
            GroupMembership.objects.filter(group=group, user=request.user).update(is_active=False)
            ConversationParticipant.objects.filter(
                conversation=group.conversation, user=request.user
            ).update(is_active=False)
        realtime.broadcast_group_membership(group, removed=[request.user.id])
        return envelope("You left the group")

    # -- archiving ----------------------------------------------------------

    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        return self._set_archived(request, archived=True)

    @action(detail=True, methods=["post"])
    def unarchive(self, request, pk=None):
        return self._set_archived(request, archived=False)

    def _set_archived(self, request, *, archived):
        self._require_admin()
        group = self.get_object()
        with transaction.atomic():
            group.is_active = not archived
            group.save(update_fields=["is_active", "updated_at"])
            group.conversation.is_active = not archived
            group.conversation.save(update_fields=["is_active", "updated_at"])
            record(request.user, "GROUP_ARCHIVED" if archived else "GROUP_RESTORED", group, request)
        member_ids = list(
            group.memberships.filter(is_active=True).values_list("user_id", flat=True)
        )
        realtime.emit_to_users(
            member_ids,
            "group.removed" if archived else "group.membership",
            {"group_id": str(group.id), "conversation_id": str(group.conversation_id), "name": group.name},
        )
        return envelope("Group archived" if archived else "Group restored")

    @action(detail=True, methods=["get", "post", "delete"])
    def image(self, request, pk=None):
        """Group photo: administrators upload/remove it, members may read it."""
        from django.core.files.storage import default_storage
        from django.http import FileResponse, Http404

        from apps.media.validators import IMAGE_TYPES, _validate_image, sniff, storage_key

        group = self.get_object()

        if request.method == "GET":
            if not group.image_key:
                raise Http404
            try:
                response = FileResponse(default_storage.open(group.image_key, "rb"))
            except FileNotFoundError as exc:
                raise Http404 from exc
            response["Cache-Control"] = "private, max-age=300"
            response["X-Content-Type-Options"] = "nosniff"
            return response

        self._require_admin()

        if request.method == "DELETE":
            if group.image_key:
                try:
                    default_storage.delete(group.image_key)
                except OSError:  # pragma: no cover - storage best effort
                    pass
                group.image_key = ""
                group.save(update_fields=["image_key", "updated_at"])
            return envelope("Group photo removed")

        fileobj = request.FILES.get("image") or request.FILES.get("file")
        if fileobj is None:
            raise ValidationError({"image": "An image file is required."})
        if fileobj.size > 5 * 1024**2:
            raise ValidationError({"image": "Group photos must be 5 MB or smaller."})
        mime = sniff(fileobj)
        if mime not in IMAGE_TYPES:
            raise ValidationError({"image": "Upload a JPEG, PNG, WebP or GIF image."})
        _validate_image(fileobj)

        previous = group.image_key
        group.image_key = default_storage.save(
            storage_key(f"groups/{group.id}", IMAGE_TYPES[mime][0]), fileobj
        )
        group.save(update_fields=["image_key", "updated_at"])
        if previous:
            try:
                default_storage.delete(previous)
            except OSError:  # pragma: no cover
                pass
        record(request.user, "GROUP_IMAGE_UPDATED", group, request)
        realtime.emit_to_conversation(
            group.conversation_id,
            "conversation.updated",
            {"conversation_id": str(group.conversation_id), "image_url": f"/api/groups/{group.id}/image/"},
        )
        return envelope("Group photo updated", GroupSerializer(group, context={"request": request}).data)

    @action(detail=True, methods=["get"])
    def activity(self, request, pk=None):
        """Audit trail for one group (administrator only)."""
        self._require_admin()
        group = self.get_object()
        from apps.audit.models import AuditLog

        rows = AuditLog.objects.select_related("actor").filter(
            object_type="Group", object_id=str(group.id)
        ).order_by("-created_at")[:50]
        return envelope(
            "Group activity retrieved",
            {
                "results": [
                    {
                        "action": row.action,
                        "actor": row.actor.full_name if row.actor else None,
                        "at": row.created_at,
                        "metadata": row.metadata,
                    }
                    for row in rows
                ]
            },
        )
