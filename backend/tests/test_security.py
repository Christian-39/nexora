import pytest
from apps.accounts.models import User
from apps.accounts.services import initial_pin,create_member
from apps.conversations.services import private_conversation,send_message
@pytest.mark.django_db
def test_initial_pin_and_duplicate_phone():
 admin=User.objects.create_superuser('+2348030000000','987654',full_name='Admin')
 m=create_member(actor=admin,phone='+2348012345678',full_name='A')
 assert initial_pin(m.phone)=='234801' and m.check_password('234801')
 with pytest.raises(Exception):create_member(actor=admin,phone='+2348012345678',full_name='B')
@pytest.mark.django_db
def test_member_cannot_private_message_member():
 a=User.objects.create_user('+2348012345678','123456',full_name='A',role='MEMBER')
 b=User.objects.create_user('+2348098765432','123456',full_name='B',role='MEMBER')
 with pytest.raises(Exception):private_conversation(a,b)
@pytest.mark.django_db
def test_message_idempotency():
 import uuid
 admin=User.objects.create_superuser('+2348030000000','987654',full_name='Admin')
 member=User.objects.create_user('+2348012345678','123456',full_name='A',role='MEMBER')
 c=private_conversation(admin,member);key=uuid.uuid4()
 one,new=send_message(user=member,conversation=c,client_id=key,text='hello')
 two,new2=send_message(user=member,conversation=c,client_id=key,text='hello')
 assert one.id==two.id and new and not new2
