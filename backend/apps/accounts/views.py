from django.conf import settings
from django.db import transaction
from django.contrib.auth import authenticate
from django.utils import timezone
from rest_framework import status,viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.decorators import api_view,permission_classes,throttle_classes,action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.authentication import CSRFCheck
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from .models import User,DeviceSession
from .serializers import *
from .services import create_member,normalize_phone,register_failure
from .session_serializers import SessionSerializer
from apps.security.services import event,ip_hash
from apps.audit.services import record
from apps.core.throttles import CredentialThrottle
class LoginThrottle(AnonRateThrottle):scope='login'
def cookies(response,refresh):
 response.set_cookie(settings.ACCESS_COOKIE,str(refresh.access_token),httponly=True,secure=settings.COOKIE_SECURE,samesite=settings.COOKIE_SAMESITE,max_age=600,path='/')
 response.set_cookie(settings.REFRESH_COOKIE,str(refresh),httponly=True,secure=settings.COOKIE_SECURE,samesite=settings.COOKIE_SAMESITE,max_age=604800,path='/api/auth/')
@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([LoginThrottle])
def login(request):
 phone=request.data.get('phone',''); pin=request.data.get('pin',''); found=None
 try: found=User.objects.filter(phone=normalize_phone(phone)).first()
 except Exception: pass
 if found and found.locked_until and found.locked_until>timezone.now():return Response({'success':False,'message':'Invalid credentials or temporarily unavailable.','code':'AUTH_FAILED','errors':{}},status=401)
 
 try: auth_phone=normalize_phone(phone)
 except Exception: auth_phone=''
 user=authenticate(request,phone=auth_phone,password=pin)
 if not user or not user.is_active:register_failure(found);event('LOGIN_FAILURE',request,found);return Response({'success':False,'message':'Invalid credentials or temporarily unavailable.','code':'AUTH_FAILED','errors':{}},status=401)
 user.failed_login_count=0;user.locked_until=None;user.save(update_fields=['failed_login_count','locked_until']);r=RefreshToken.for_user(user)
 session=DeviceSession.objects.create(user=user,jti=str(r['jti']),device_label=str(request.data.get('device_label',''))[:120],ip_hash=ip_hash(request),expires_at=timezone.datetime.fromtimestamp(r['exp'],tz=timezone.get_current_timezone()))
 r['sid']=str(session.id);event('LOGIN_SUCCESS',request,user)
 out=Response({'success':True,'message':'Login successful','data':UserSerializer(user).data});cookies(out,r);return out
@api_view(['POST'])
@permission_classes([AllowAny])
def refresh(request):
 check=CSRFCheck(lambda req:None);check.process_request(request._request);reason=check.process_view(request._request,None,(),{})
 if reason:raise PermissionDenied('CSRF validation failed.')
 try:
  r=RefreshToken(request.COOKIES.get(settings.REFRESH_COOKIE));sid=r.get('sid');session=DeviceSession.objects.get(id=sid,user_id=r['user_id'],jti=str(r['jti']),revoked_at__isnull=True,expires_at__gt=timezone.now());r.set_jti();r.set_exp();r.set_iat();r['sid']=str(session.id);session.jti=str(r['jti']);session.expires_at=timezone.datetime.fromtimestamp(r['exp'],tz=timezone.get_current_timezone());session.save(update_fields=['jti','expires_at','last_active']);out=Response({'success':True,'message':'Token refreshed','data':{}});cookies(out,r);return out
 except (TokenError,TypeError,ValueError,DeviceSession.DoesNotExist):return Response({'success':False,'message':'Session expired.','code':'INVALID_SESSION','errors':{}},status=401)
@api_view(['POST'])
def logout(request):
 try:
  token=RefreshToken(request.COOKIES.get(settings.REFRESH_COOKIE));token.blacklist();DeviceSession.objects.filter(id=token.get('sid'),user=request.user).update(revoked_at=timezone.now());event('LOGOUT',request,request.user)
 except Exception:pass
 out=Response({'success':True,'message':'Logged out','data':{}});out.delete_cookie(settings.ACCESS_COOKIE);out.delete_cookie(settings.REFRESH_COOKIE,path='/api/auth/');return out
@api_view(['POST'])
@throttle_classes([CredentialThrottle])
def change_pin(request):
 s=PinSerializer(data=request.data);s.is_valid(raise_exception=True)
 if not request.user.check_password(s.validated_data['current_pin']):return Response({'success':False,'message':'Current PIN is incorrect.','code':'INVALID_CREDENTIAL','errors':{}},status=400)
 request.user.set_password(s.validated_data['new_pin']);request.user.credential_state=User.Credential.CHANGED;request.user.save();request.user.device_sessions.filter(revoked_at__isnull=True).update(revoked_at=timezone.now());event('CREDENTIAL_CHANGE',request,request.user);return Response({'success':True,'message':'PIN changed; sign in again.','data':{}})
class MemberViewSet(viewsets.ModelViewSet):
 serializer_class=UserSerializer;http_method_names=['get','post','patch']
 def initial(self,request,*args,**kwargs):
  super().initial(request,*args,**kwargs)
  if request.user.role!=User.Role.ADMIN:raise PermissionDenied()
 def get_queryset(self):
  return User.objects.filter(role=User.Role.MEMBER).order_by('-created_at') if self.request.user.role==User.Role.ADMIN else User.objects.none()
 def create(self,request):
  s=MemberCreateSerializer(data=request.data);s.is_valid(raise_exception=True);u=create_member(actor=request.user,**s.validated_data);record(request.user,'MEMBER_CREATED',u,request);return Response({'success':True,'message':'Member created','data':UserSerializer(u).data},status=201)
 @action(detail=True,methods=['post'])
 def deactivate(self,request,pk=None):
  u=self.get_object()
  with transaction.atomic():
   u.is_active=False;u.deactivated_at=timezone.now();u.save(update_fields=['is_active','deactivated_at','updated_at']);u.device_sessions.filter(revoked_at__isnull=True).update(revoked_at=timezone.now());record(request.user,'MEMBER_DEACTIVATED',u,request)
  return Response({'success':True,'message':'Member deactivated','data':UserSerializer(u).data})
 @action(detail=True,methods=['post'])
 def activate(self,request,pk=None):
  u=self.get_object();u.is_active=True;u.deactivated_at=None;u.save(update_fields=['is_active','deactivated_at','updated_at']);record(request.user,'MEMBER_ACTIVATED',u,request);return Response({'success':True,'message':'Member activated','data':UserSerializer(u).data})
 @action(detail=True,methods=['post'],url_path='reset-pin')
 def reset_pin(self,request,pk=None):
  from .services import initial_pin
  u=self.get_object()
  with transaction.atomic():
   u.set_password(initial_pin(u.phone));u.credential_state=User.Credential.RESET_REQUIRED;u.save(update_fields=['password','credential_state','updated_at']);u.device_sessions.filter(revoked_at__isnull=True).update(revoked_at=timezone.now());record(request.user,'CREDENTIAL_RESET',u,request)
  return Response({'success':True,'message':'Credential reset to the documented initial-phone rule.','data':{}})

from django.views.decorators.csrf import ensure_csrf_cookie
@api_view(['GET'])
@permission_classes([AllowAny])
@ensure_csrf_cookie
def csrf(request):
 return Response({'success':True,'message':'CSRF cookie set','data':{}})

@api_view(['GET','PATCH'])
def me(request):
 if request.method=='PATCH':
  from apps.platform_settings.services import messaging_policy
  if request.user.role=='MEMBER' and 'full_name' in request.data and not messaging_policy()['allow_member_name_edit']:raise PermissionDenied('Display-name editing is disabled.')
  s=ProfileSerializer(request.user,data=request.data,partial=True);s.is_valid(raise_exception=True);s.save();return Response({'success':True,'message':'Profile updated','data':s.data})
 return Response({'success':True,'message':'Profile retrieved','data':ProfileSerializer(request.user).data})
@api_view(['GET'])
def sessions(request):
 rows=request.user.device_sessions.order_by('-last_active')
 return Response({'success':True,'message':'Sessions retrieved','data':SessionSerializer(rows,many=True).data})
@api_view(['DELETE'])
def revoke_session(request,session_id):
 count=request.user.device_sessions.filter(id=session_id,revoked_at__isnull=True).update(revoked_at=timezone.now())
 if not count:return Response({'success':False,'message':'Session not found.','code':'NOT_FOUND','errors':{}},status=404)
 event('SESSION_REVOKED',request,request.user,{'session_id':str(session_id)})
 return Response({'success':True,'message':'Session revoked','data':{}})
