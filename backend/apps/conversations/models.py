import uuid
from django.conf import settings
from django.db import models
from django.db.models import Q
from apps.core.models import TimeStampedModel
class Conversation(TimeStampedModel):
 class Kind(models.TextChoices):PRIVATE='ADMIN_PRIVATE';GROUP='GROUP'
 id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False);kind=models.CharField(max_length=20,choices=Kind.choices);admin=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='admin_conversations',null=True);member=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='member_conversations',null=True);is_active=models.BooleanField(default=True)
 class Meta:
  constraints=[models.UniqueConstraint(fields=['admin','member'],condition=Q(kind='ADMIN_PRIVATE'),name='unique_admin_member_private'),models.CheckConstraint(condition=(Q(kind='ADMIN_PRIVATE',admin__isnull=False,member__isnull=False)|Q(kind='GROUP',admin__isnull=False,member__isnull=True)),name='valid_conversation_shape')]
class ConversationParticipant(TimeStampedModel):
 conversation=models.ForeignKey(Conversation,on_delete=models.CASCADE,related_name='participants');user=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.CASCADE,related_name='conversation_memberships');last_read_at=models.DateTimeField(null=True);is_active=models.BooleanField(default=True)
 class Meta:constraints=[models.UniqueConstraint(fields=['conversation','user'],name='unique_conversation_participant')]
class Message(TimeStampedModel):
 class Type(models.TextChoices):TEXT='TEXT';IMAGE='IMAGE';VIDEO='VIDEO';VOICE='VOICE';SYSTEM='SYSTEM'
 id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False);conversation=models.ForeignKey(Conversation,on_delete=models.CASCADE,related_name='messages');sender=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='sent_messages');client_id=models.CharField(max_length=64,db_index=True);type=models.CharField(max_length=10,choices=Type.choices,default=Type.TEXT);text=models.TextField(blank=True);reply_to=models.ForeignKey('self',null=True,blank=True,on_delete=models.SET_NULL);edited_at=models.DateTimeField(null=True);deleted_at=models.DateTimeField(null=True)
 class Meta:
  constraints=[models.UniqueConstraint(fields=['sender','client_id'],name='unique_sender_client_message')];indexes=[models.Index(fields=['conversation','-created_at']),models.Index(fields=['sender','created_at']),models.Index(fields=['type']),models.Index(fields=['conversation','type','-created_at'])]
class MessageReceipt(TimeStampedModel):
 message=models.ForeignKey(Message,on_delete=models.CASCADE,related_name='receipts');recipient=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.CASCADE);delivered_at=models.DateTimeField(null=True);read_at=models.DateTimeField(null=True)
 class Meta:constraints=[models.UniqueConstraint(fields=['message','recipient'],name='unique_message_receipt')];indexes=[models.Index(fields=['recipient','read_at'],name='receipt_recipient_unread_idx')]
class Attachment(TimeStampedModel):
 id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False);message=models.OneToOneField(Message,on_delete=models.CASCADE,related_name='attachment');storage_key=models.CharField(max_length=512,unique=True);original_name=models.CharField(max_length=255);mime_type=models.CharField(max_length=100);size=models.PositiveBigIntegerField();duration_ms=models.PositiveIntegerField(null=True);width=models.PositiveIntegerField(null=True);height=models.PositiveIntegerField(null=True);processing_state=models.CharField(max_length=12,choices=[('PENDING','PENDING'),('PROCESSING','PROCESSING'),('RETRY','RETRY'),('READY','READY'),('FAILED','FAILED')],default='PENDING',db_index=True);thumbnail_key=models.CharField(max_length=512,blank=True);optimized_key=models.CharField(max_length=512,blank=True);processing_error=models.CharField(max_length=80,blank=True);processing_attempts=models.PositiveSmallIntegerField(default=0);next_attempt_at=models.DateTimeField(null=True,blank=True,db_index=True)
class MessageDeletion(TimeStampedModel):
 message=models.ForeignKey(Message,on_delete=models.CASCADE,related_name='self_deletions');user=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.CASCADE)
 class Meta:constraints=[models.UniqueConstraint(fields=['message','user'],name='unique_message_self_deletion')]
class MessageReaction(TimeStampedModel):
 message=models.ForeignKey(Message,on_delete=models.CASCADE,related_name='reactions');user=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.CASCADE);reaction=models.CharField(max_length=24)
 class Meta:constraints=[models.UniqueConstraint(fields=['message','user','reaction'],name='unique_message_user_reaction')]
