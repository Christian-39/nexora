import io,uuid
import pytest
from PIL import Image
from rest_framework.test import APIClient
from apps.accounts.models import User
from apps.conversations.services import private_conversation
@pytest.fixture
def users(db):
 admin=User.objects.create_superuser('+2348030000000','987654',full_name='Admin')
 a=User.objects.create_user('+2348012345678','123456',full_name='A',role='MEMBER')
 b=User.objects.create_user('+2348098765432','123456',full_name='B',role='MEMBER')
 return admin,a,b
def authed(user):
 user.credential_state='CHANGED';user.save(update_fields=['credential_state'])
 from rest_framework_simplejwt.tokens import RefreshToken
 from apps.accounts.models import DeviceSession
 from django.utils import timezone
 r=RefreshToken.for_user(user);session=DeviceSession.objects.create(user=user,jti=str(r['jti']),expires_at=timezone.now()+timezone.timedelta(days=1));r['sid']=str(session.id)
 c=APIClient();c.cookies['nexora_access']=str(r.access_token);c.cookies['csrftoken']='test';c.credentials(HTTP_X_CSRFTOKEN='test');return c
@pytest.mark.django_db
def test_member_cannot_list_members(users):
 _,a,_=users
 assert authed(a).get('/api/members/').status_code==403
@pytest.mark.django_db
def test_member_cannot_create_other_member_chat(users):
 _,a,b=users
 assert authed(a).post('/api/conversations/',{'participant':str(b.id)},format='json').status_code==403
@pytest.mark.django_db
def test_private_media_idor(users,settings,tmp_path):
 admin,a,b=users;settings.MEDIA_ROOT=tmp_path;c=private_conversation(admin,a)
 img=io.BytesIO();Image.new('RGB',(4,4),'red').save(img,'JPEG');img.seek(0);img.name='safe.jpg'
 r=authed(a).post('/api/media/',{'conversation':str(c.id),'client_id':str(uuid.uuid4()),'type':'IMAGE','file':img},format='multipart')
 assert r.status_code==201
 from apps.conversations.models import Attachment
 attachment=Attachment.objects.get()
 assert authed(b).get(f'/api/media/{attachment.id}/').status_code==403
