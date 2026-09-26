from channels.db import database_sync_to_async
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken
@database_sync_to_async
def auth_for(token):
 from .models import User,DeviceSession
 try:
  parsed=AccessToken(token);user=User.objects.get(id=parsed['user_id'],is_active=True)
  if not DeviceSession.objects.filter(id=parsed.get('sid'),user=user,revoked_at__isnull=True,expires_at__gt=timezone.now()).exists():return AnonymousUser(),None
  return user,int(parsed['exp'])
 except Exception:return AnonymousUser(),None
class JWTAuthMiddleware:
 def __init__(self,app):self.app=app
 async def __call__(self,scope,receive,send):
  headers=dict(scope.get('headers',[]));cookies={}
  for item in headers.get(b'cookie',b'').decode().split(';'):
   if '=' in item:k,v=item.strip().split('=',1);cookies[k]=v
  scope['user'],scope['token_exp']=await auth_for(cookies.get(settings.ACCESS_COOKIE,''));return await self.app(scope,receive,send)
