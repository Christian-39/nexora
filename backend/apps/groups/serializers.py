from rest_framework import serializers

from .models import Group


class GroupSerializer(serializers.ModelSerializer):
    member_ids = serializers.ListField(child=serializers.UUIDField(), write_only=True, required=False)
    member_count = serializers.SerializerMethodField()
    members = serializers.SerializerMethodField()
    conversation_id = serializers.CharField(source="conversation.id", read_only=True)
    is_archived = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()
    can_manage = serializers.SerializerMethodField()

    class Meta:
        model = Group
        fields = [
            "id",
            "name",
            "description",
            "creator",
            "conversation",
            "conversation_id",
            "is_active",
            "is_archived",
            "image_url",
            "can_manage",
            "members_can_send",
            "members_can_view_members",
            "members_can_send_media",
            "members_can_send_voice",
            "members_can_reply",
            "members_can_react",
            "members_can_leave",
            "member_ids",
            "member_count",
            "members",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "creator", "conversation", "created_at", "updated_at"]

    def _viewer(self):
        request = self.context.get("request")
        return getattr(request, "user", None) if request else None

    def get_image_url(self, obj):
        if not obj.image_key:
            return None
        path = f"/api/groups/{obj.id}/image/"
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request else path

    def get_is_archived(self, obj):
        return not obj.is_active

    def get_can_manage(self, obj):
        viewer = self._viewer()
        return bool(viewer and viewer.role == "ADMIN")

    def get_member_count(self, obj):
        cached = getattr(obj, "active_member_count", None)
        if cached is not None:
            return cached
        return obj.memberships.filter(is_active=True).count()

    def get_members(self, obj):
        viewer = self._viewer()
        if viewer and viewer.role != "ADMIN" and not obj.members_can_view_members:
            return []
        from apps.conversations.serializers import participant_payload

        return [
            participant_payload(m.user, viewer=viewer, request=self.context.get("request"))
            for m in obj.memberships.all()
            if m.is_active
        ]

    def validate_name(self, value):
        value = value.strip()
        if len(value) < 2:
            raise serializers.ValidationError("Give the group a name of at least two characters.")
        return value

    def update(self, instance, validated_data):
        validated_data.pop("member_ids", None)
        return super().update(instance, validated_data)
