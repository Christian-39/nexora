from django.db.models import Q
from django.db import models
from django.utils import timezone
from rest_framework import viewsets,status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied
from apps.accounts.models import User
from .models import *
from .serializers import *
from .services import private_conversation,send_message,can_access
class ConversationViewSet(viewsets.ReadOnlyModelViewSet):
 serializer_class=ConversationSerializer
 def get_queryset(self):return Conversation.objects.filter(participants__user=self.request.user,participants__is_active=True,is_active=True).distinct().order_by('-updated_at')
 def create(self,request):
  try:target=User.objects.get(id=request.data.get('participant'),is_active=True)
  except User.DoesNotExist:return Response({'success':False,'message':'Invalid participant.','code':'INVALID_PARTICIPANT','errors':{}},status=400)
  c=private_conversation(request.user,target);return Response({'success':True,'message':'Conversation ready','data':ConversationSerializer(c).data},status=201)
 @action(detail=True,methods=['get','post'])
 def messages(self,request,pk=None):
  c=self.get_object()
  if request.method=='GET':
   qs=c.messages.select_related('sender','reply_to','attachment').prefetch_related('receipts').exclude(self_deletions__user=request.user).order_by('-created_at');page=self.paginate_queryset(qs);return self.get_paginated_response(MessageSerializer(page,many=True).data)
  s=MessageCreateSerializer(data=request.data);s.is_valid(raise_exception=True);m,created=send_message(user=request.user,conversation=c,**s.validated_data)
  from asgiref.sync import async_to_sync
  from channels.layers import get_channel_layer
  transaction_callback=lambda:async_to_sync(get_channel_layer().group_send)(f'conversation_{c.id}',{'type':'message.new','data':MessageSerializer(m).data})
  from django.db import transaction
  transaction.on_commit(transaction_callback)
  return Response({'success':True,'message':'Message sent','data':MessageSerializer(m).data},status=201 if created else 200)
 @action(detail=True,methods=['post'])
 def read(self,request,pk=None):
  c=self.get_object();now=timezone.now();MessageReceipt.objects.filter(message__conversation=c,recipient=request.user,read_at__isnull=True).update(delivered_at=now,read_at=now);ConversationParticipant.objects.filter(conversation=c,user=request.user).update(last_read_at=now)
  from asgiref.sync import async_to_sync
  from channels.layers import get_channel_layer
  async_to_sync(get_channel_layer().group_send)(f'conversation_{c.id}',{'type':'receipt.event','data':{'user':str(request.user.id),'read_at':now.isoformat()}})
  return Response({'success':True,'message':'Read state updated','data':{}})
from rest_framework.decorators import api_view,throttle_classes
from apps.core.throttles import SearchThrottle
from rest_framework.exceptions import ValidationError
@api_view(['GET'])
@throttle_classes([SearchThrottle])
def search_messages(request):
 query=str(request.query_params.get('q','')).strip();kind=str(request.query_params.get('type','')).upper();date_from=request.query_params.get('date_from');date_to=request.query_params.get('date_to')
 if query and len(query)<2:raise ValidationError('Search query must contain at least two characters.')
 if not any((query,kind,date_from,date_to)):raise ValidationError('Provide at least one search filter.')
 qs=Message.objects.filter(conversation__participants__user=request.user,conversation__participants__is_active=True,deleted_at__isnull=True).exclude(self_deletions__user=request.user)
 if query:
  text_filter=Q(text__icontains=query)|Q(sender__full_name__icontains=query)|Q(conversation__group__name__icontains=query)
  if request.user.role=='ADMIN':text_filter|=Q(sender__phone__icontains=query)
  qs=qs.filter(text_filter)
 if kind:
  if kind not in Message.Type.values:raise ValidationError('Unsupported message type.')
  qs=qs.filter(type=kind)
 from django.utils.dateparse import parse_date
 if date_from:
  parsed=parse_date(date_from)
  if not parsed:raise ValidationError('Invalid date_from; use YYYY-MM-DD.')
  qs=qs.filter(created_at__date__gte=parsed)
 if date_to:
  parsed=parse_date(date_to)
  if not parsed:raise ValidationError('Invalid date_to; use YYYY-MM-DD.')
  qs=qs.filter(created_at__date__lte=parsed)
 qs=qs.select_related('sender','conversation','attachment').prefetch_related('receipts').distinct().order_by('-created_at')
 paginator=ConversationViewSet.pagination_class();page=paginator.paginate_queryset(qs,request)
 return paginator.get_paginated_response(MessageSerializer(page,many=True,context={'request':request}).data)
@api_view(['PATCH','DELETE'])
def message_detail(request,message_id):
 try:m=Message.objects.select_related('conversation').get(id=message_id)
 except Message.DoesNotExist:return Response({'success':False,'message':'Message not found.','code':'NOT_FOUND','errors':{}},status=404)
 if not can_access(request.user,m.conversation):raise PermissionDenied()
 if request.method=='DELETE':
  scope=request.data.get('scope','self')
  if scope=='self':MessageDeletion.objects.get_or_create(message=m,user=request.user)
  elif scope=='everyone':
   if m.sender_id!=request.user.id:raise PermissionDenied()
   from apps.platform_settings.services import messaging_policy
   policy=messaging_policy()
   if not policy['allow_delete_everyone']:raise PermissionDenied('Delete for everyone is disabled.')
   if timezone.now()-m.created_at>timezone.timedelta(minutes=policy['message_delete_window_minutes']):raise ValidationError('Deletion window has expired.')
   m.text='';m.deleted_at=timezone.now();m.save(update_fields=['text','deleted_at','updated_at'])
   from asgiref.sync import async_to_sync
   from channels.layers import get_channel_layer
   async_to_sync(get_channel_layer().group_send)(f'conversation_{m.conversation_id}',{'type':'message.changed','data':{'id':str(m.id),'deleted_at':m.deleted_at.isoformat()}})
  else:raise ValidationError('Invalid deletion scope.')
  return Response({'success':True,'message':'Message deleted','data':{}})
 if m.sender_id!=request.user.id or m.type!='TEXT' or m.deleted_at:raise PermissionDenied()
 from apps.platform_settings.services import messaging_policy
 if timezone.now()-m.created_at>timezone.timedelta(minutes=messaging_policy()['message_edit_window_minutes']):raise ValidationError('Editing window has expired.')
 text=str(request.data.get('text','')).strip()
 if not text:raise ValidationError('Text is required.')
 m.text=text;m.edited_at=timezone.now();m.save(update_fields=['text','edited_at','updated_at'])
 from asgiref.sync import async_to_sync
 from channels.layers import get_channel_layer
 async_to_sync(get_channel_layer().group_send)(f'conversation_{m.conversation_id}',{'type':'message.changed','data':MessageSerializer(m).data})
 return Response({'success':True,'message':'Message edited','data':MessageSerializer(m).data})
@api_view(['POST','DELETE'])
def reaction(request,message_id):
 allowed={'LIKE','LOVE','LAUGH','WOW','SAD','THANKS'}
 try:m=Message.objects.select_related('conversation').get(id=message_id)
 except Message.DoesNotExist:return Response({'success':False,'message':'Message not found.','code':'NOT_FOUND','errors':{}},status=404)
 if not can_access(request.user,m.conversation):raise PermissionDenied()
 if m.conversation.kind==Conversation.Kind.GROUP and request.user.role=='MEMBER' and not m.conversation.group.members_can_react:raise PermissionDenied('Members cannot react in this group.')
 value=str(request.data.get('reaction','')).upper()
 if value not in allowed:raise ValidationError('Unsupported reaction.')
 if request.method=='POST':obj,_=MessageReaction.objects.get_or_create(message=m,user=request.user,reaction=value);return Response({'success':True,'message':'Reaction saved','data':ReactionSerializer(obj).data},status=201)
 MessageReaction.objects.filter(message=m,user=request.user,reaction=value).delete();return Response({'success':True,'message':'Reaction removed','data':{}})
@api_view(['GET'])
def unread_counts(request):
 rows=(MessageReceipt.objects.filter(recipient=request.user,read_at__isnull=True,message__conversation__is_active=True).values('message__conversation_id').annotate(count=models.Count('id')).order_by())
 data={str(row['message__conversation_id']):row['count'] for row in rows}
 from apps.notifications.models import Notification
 notification_count=Notification.objects.filter(recipient=request.user,read_at__isnull=True).count()
 return Response({'success':True,'message':'Unread counts retrieved','data':{'global':sum(data.values()),'conversations':data,'notifications':notification_count}})
