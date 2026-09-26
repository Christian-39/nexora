import uuid
import pytest
from apps.accounts.models import User
from apps.conversations.services import private_conversation,send_message
from apps.groups.models import Group,GroupMembership
from apps.conversations.models import Conversation,ConversationParticipant
from tests.test_api import authed
@pytest.mark.django_db
def test_member_cannot_patch_group_or_cross_conversation_reply():
 admin=User.objects.create_superuser('+2348030000000','987654',full_name='Admin');a=User.objects.create_user('+2348012345678','123456',full_name='A',role='MEMBER');b=User.objects.create_user('+2348098765432','123456',full_name='B',role='MEMBER')
 gc=Conversation.objects.create(kind='GROUP',admin=admin);g=Group.objects.create(name='G',creator=admin,conversation=gc);GroupMembership.objects.create(group=g,user=a);ConversationParticipant.objects.bulk_create([ConversationParticipant(conversation=gc,user=admin),ConversationParticipant(conversation=gc,user=a)])
 assert authed(a).patch(f'/api/groups/{g.id}/',{'name':'Hacked'},format='json').status_code==403
 private=private_conversation(admin,b);other,_=send_message(user=admin,conversation=private,client_id=uuid.uuid4(),text='private')
 with pytest.raises(Exception):send_message(user=a,conversation=gc,client_id=uuid.uuid4(),text='bad reply',reply_to=other)
@pytest.mark.django_db
def test_member_cannot_react_when_disabled():
 admin=User.objects.create_superuser('+2348030000000','987654',full_name='Admin');a=User.objects.create_user('+2348012345678','123456',full_name='A',role='MEMBER')
 c=Conversation.objects.create(kind='GROUP',admin=admin);g=Group.objects.create(name='G',creator=admin,conversation=c,members_can_react=False);GroupMembership.objects.create(group=g,user=a);ConversationParticipant.objects.bulk_create([ConversationParticipant(conversation=c,user=admin),ConversationParticipant(conversation=c,user=a)]);m,_=send_message(user=admin,conversation=c,client_id=uuid.uuid4(),text='hello')
 assert authed(a).post(f'/api/messages/{m.id}/reaction/',{'reaction':'LIKE'},format='json').status_code==403
