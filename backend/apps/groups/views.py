from django.db import transaction
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied,ValidationError
from rest_framework.response import Response
from apps.accounts.models import User
from apps.accounts.serializers import UserSerializer
from apps.conversations.models import Conversation,ConversationParticipant
from apps.audit.services import record
from .models import Group,GroupMembership
from .serializers import GroupSerializer
class GroupViewSet(viewsets.ModelViewSet):
 serializer_class=GroupSerializer;http_method_names=['get','post','patch']
 def get_queryset(self):
  q=Group.objects.select_related('creator','conversation').prefetch_related('memberships__user').filter(is_active=True)
  return q if self.request.user.role=='ADMIN' else q.filter(memberships__user=self.request.user,memberships__is_active=True)
 def _admin(self):
  if self.request.user.role!='ADMIN':raise PermissionDenied()
 def update(self,request,*args,**kwargs):
  self._admin();response=super().update(request,*args,**kwargs);record(request.user,'GROUP_UPDATED',self.get_object(),request,{'fields':sorted(request.data.keys())});return response
 @transaction.atomic
 def create(self,request):
  self._admin();s=self.get_serializer(data=request.data);s.is_valid(raise_exception=True);ids=s.validated_data.pop('member_ids',[]);members=list(User.objects.filter(id__in=ids,role='MEMBER',is_active=True))
  if len(members)!=len(set(ids)):raise ValidationError('Every member must be a valid active member.')
  c=Conversation.objects.create(kind=Conversation.Kind.GROUP,admin=request.user);g=s.save(creator=request.user,conversation=c)
  GroupMembership.objects.bulk_create([GroupMembership(group=g,user=u) for u in members]);ConversationParticipant.objects.bulk_create([ConversationParticipant(conversation=c,user=request.user)]+[ConversationParticipant(conversation=c,user=u) for u in members]);record(request.user,'GROUP_CREATED',g,request,{'member_count':len(members)});return Response({'success':True,'message':'Group created','data':GroupSerializer(g).data},status=201)
 @action(detail=True,methods=['get','post','delete'])
 @transaction.atomic
 def members(self,request,pk=None):
  g=self.get_object()
  if request.method=='GET':
   if request.user.role!='ADMIN' and not g.members_can_view_members:raise PermissionDenied()
   users=User.objects.filter(group_memberships__group=g,group_memberships__is_active=True,is_active=True).order_by('full_name');return Response({'success':True,'message':'Members retrieved','data':UserSerializer(users,many=True).data})
  self._admin();ids=request.data.get('member_ids',[]);members=list(User.objects.filter(id__in=ids,role='MEMBER',is_active=True))
  if len(members)!=len(set(ids)):raise ValidationError('Every member must be a valid active member.')
  if request.method=='POST':
   GroupMembership.objects.bulk_create([GroupMembership(group=g,user=u) for u in members],ignore_conflicts=True);GroupMembership.objects.filter(group=g,user__in=members).update(is_active=True);ConversationParticipant.objects.bulk_create([ConversationParticipant(conversation=g.conversation,user=u) for u in members],ignore_conflicts=True);ConversationParticipant.objects.filter(conversation=g.conversation,user__in=members).update(is_active=True);action_name='GROUP_MEMBERS_ADDED'
  else:
   GroupMembership.objects.filter(group=g,user__in=members).update(is_active=False);ConversationParticipant.objects.filter(conversation=g.conversation,user__in=members).update(is_active=False);action_name='GROUP_MEMBERS_REMOVED'
  record(request.user,action_name,g,request,{'member_ids':[str(x.id) for x in members]});return Response({'success':True,'message':'Membership updated','data':{}})
 @action(detail=True,methods=['post'])
 def leave(self,request,pk=None):
  g=self.get_object()
  if request.user.role!='MEMBER' or not g.members_can_leave:raise PermissionDenied()
  with transaction.atomic():GroupMembership.objects.filter(group=g,user=request.user).update(is_active=False);ConversationParticipant.objects.filter(conversation=g.conversation,user=request.user).update(is_active=False)
  return Response({'success':True,'message':'You left the group','data':{}})
 @action(detail=True,methods=['post'])
 def archive(self,request,pk=None):
  self._admin();g=self.get_object();g.is_active=False;g.save(update_fields=['is_active','updated_at']);g.conversation.is_active=False;g.conversation.save(update_fields=['is_active','updated_at']);record(request.user,'GROUP_ARCHIVED',g,request);return Response({'success':True,'message':'Group archived','data':{}})
