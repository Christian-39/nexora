from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied,ValidationError
from apps.accounts.models import User
from .models import *
def can_access(user,c):return c.is_active and c.participants.filter(user=user,is_active=True).exists()
@transaction.atomic
def private_conversation(actor,target):
 if actor.role=='MEMBER' and target.role!='ADMIN':raise PermissionDenied()
 if actor.role=='ADMIN' and target.role!='MEMBER':raise ValidationError('Private chats require one admin and one member.')
 admin=actor if actor.role=='ADMIN' else target;member=target if actor.role=='ADMIN' else actor
 c,_=Conversation.objects.get_or_create(kind=Conversation.Kind.PRIVATE,admin=admin,member=member)
 ConversationParticipant.objects.bulk_create([ConversationParticipant(conversation=c,user=admin),ConversationParticipant(conversation=c,user=member)],ignore_conflicts=True);return c
@transaction.atomic
def send_message(*,user,conversation,client_id,text,type='TEXT',reply_to=None):
 if not can_access(user,conversation):raise PermissionDenied()
 if conversation.kind==Conversation.Kind.GROUP and user.role=='MEMBER':
  group=conversation.group
  if not group.members_can_send:raise PermissionDenied('Members cannot send in this group.')
  if type in ('IMAGE','VIDEO') and not group.members_can_send_media:raise PermissionDenied('Members cannot send media in this group.')
  if type=='VOICE' and not group.members_can_send_voice:raise PermissionDenied('Members cannot send voice notes in this group.')
  if reply_to and not group.members_can_reply:raise PermissionDenied('Members cannot reply in this group.')
 if reply_to and reply_to.conversation_id!=conversation.id:raise ValidationError('Reply target must belong to this conversation.')
 if type=='TEXT' and not text.strip():raise ValidationError('Text is required.')
 from apps.platform_settings.services import messaging_policy
 if type=='TEXT' and len(text)>messaging_policy()['max_message_length']:raise ValidationError('Message exceeds the configured length limit.')
 m,created=Message.objects.get_or_create(sender=user,client_id=client_id,defaults={'conversation':conversation,'text':text,'type':type,'reply_to':reply_to})
 if not created and m.conversation_id!=conversation.id:raise ValidationError('Invalid idempotency key.')
 if created:
  recipients=conversation.participants.filter(is_active=True).exclude(user=user).values_list('user_id',flat=True)
  recipient_ids=list(recipients)
  MessageReceipt.objects.bulk_create([MessageReceipt(message=m,recipient_id=x) for x in recipient_ids])
  from apps.notifications.services import create_notification
  for recipient_id in recipient_ids:create_notification(recipient_id=recipient_id,type='NEW_MESSAGE',title='New message',message='You have a new message.',related_id=m.id)
 return m,created
