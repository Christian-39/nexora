import uuid
import pytest
from django.utils import timezone
from rest_framework.test import APIClient
from apps.accounts.models import User,DeviceSession
from apps.conversations.services import private_conversation,send_message
from tests.test_api import authed
@pytest.mark.django_db
def test_login_creates_revocable_session():
 u=User.objects.create_user('+2348012345678','123456',full_name='A',role='MEMBER')
 c=APIClient();c.get('/api/auth/csrf/');token=c.cookies['csrftoken'].value
 r=c.post('/api/auth/login/',{'phone':u.phone,'pin':'123456','device_label':'Phone'},format='json',HTTP_X_CSRFTOKEN=token)
 assert r.status_code==200 and DeviceSession.objects.filter(user=u).count()==1
@pytest.mark.django_db
def test_edit_delete_and_search_authorization():
 admin=User.objects.create_superuser('+2348030000000','987654',full_name='Admin');a=User.objects.create_user('+2348012345678','123456',full_name='A',role='MEMBER');b=User.objects.create_user('+2348098765432','123456',full_name='B',role='MEMBER')
 c=private_conversation(admin,a);m,_=send_message(user=a,conversation=c,client_id=uuid.uuid4(),text='searchable hello')
 assert authed(b).get('/api/messages/search/?q=searchable').json()['data']['results']==[]
 assert authed(a).patch(f'/api/messages/{m.id}/',{'text':'edited hello'},format='json').status_code==200
 assert authed(a).delete(f'/api/messages/{m.id}/',{'scope':'everyone'},format='json').status_code==200
 m.refresh_from_db();assert m.deleted_at is not None and m.text==''
