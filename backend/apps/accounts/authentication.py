from django.conf import settings
from django.utils import timezone
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.authentication import CSRFCheck
from rest_framework.exceptions import PermissionDenied,AuthenticationFailed
from .models import DeviceSession
class CookieJWTAuthentication(JWTAuthentication):
 def authenticate(self,request):
  raw=request.COOKIES.get(settings.ACCESS_COOKIE)
  if not raw:return None
  token=self.get_validated_token(raw);user=self.get_user(token);sid=token.get('sid')
  if not sid or not DeviceSession.objects.filter(id=sid,user=user,revoked_at__isnull=True,expires_at__gt=timezone.now()).exists():raise AuthenticationFailed('Session revoked or expired.')
  check=CSRFCheck(lambda req:None);check.process_request(request._request);reason=check.process_view(request._request,None,(),{})
  if reason:raise PermissionDenied('CSRF validation failed.')
  return user,token
