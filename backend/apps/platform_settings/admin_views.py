from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied
from django.db import transaction
from .models import PlatformConfiguration
from .serializers import PlatformSerializer
from apps.audit.services import record
class PlatformSettingsView(APIView):
 def _admin(self,request):
  if request.user.role!='ADMIN':raise PermissionDenied()
 def get(self,request):
  self._admin(request);obj=PlatformConfiguration.objects.first();return Response({'success':True,'message':'Settings retrieved','data':PlatformSerializer(obj).data if obj else {}})
 @transaction.atomic
 def patch(self,request):
  self._admin(request);obj,_=PlatformConfiguration.objects.get_or_create(singleton=1,defaults={'organization_name':request.data.get('organization_name','Organization')});s=PlatformSerializer(obj,data=request.data,partial=True);s.is_valid(raise_exception=True);s.save();
  from django.core.cache import cache
  cache.delete('platform:messaging_policy');record(request.user,'PLATFORM_SETTINGS_UPDATED',obj,request,{'fields':sorted(s.validated_data.keys())});return Response({'success':True,'message':'Settings updated','data':s.data})
