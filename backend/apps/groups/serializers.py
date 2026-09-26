from rest_framework import serializers
from .models import Group
class GroupSerializer(serializers.ModelSerializer):
 member_ids=serializers.ListField(child=serializers.UUIDField(),write_only=True,required=False)
 class Meta:model=Group;fields=['id','name','description','creator','conversation','is_active','members_can_send','members_can_view_members','members_can_send_media','members_can_send_voice','members_can_reply','members_can_react','members_can_leave','member_ids','created_at'];read_only_fields=['id','creator','conversation','created_at']
 def update(self,instance,validated_data):
  validated_data.pop('member_ids',None);return super().update(instance,validated_data)
