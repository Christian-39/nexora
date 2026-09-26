import uuid
from django.conf import settings
from django.db import models
from apps.core.models import TimeStampedModel
class Group(TimeStampedModel):
 id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False);name=models.CharField(max_length=120);description=models.TextField(blank=True);creator=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT);conversation=models.OneToOneField('conversations.Conversation',on_delete=models.CASCADE,related_name='group');is_active=models.BooleanField(default=True);members_can_send=models.BooleanField(default=True);members_can_view_members=models.BooleanField(default=True);members_can_send_media=models.BooleanField(default=True);members_can_send_voice=models.BooleanField(default=True);members_can_reply=models.BooleanField(default=True);members_can_react=models.BooleanField(default=True);members_can_leave=models.BooleanField(default=False)
class GroupMembership(TimeStampedModel):
 group=models.ForeignKey(Group,on_delete=models.CASCADE,related_name='memberships');user=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.CASCADE,related_name='group_memberships');is_active=models.BooleanField(default=True)
 class Meta:constraints=[models.UniqueConstraint(fields=['group','user'],name='unique_group_membership')]
