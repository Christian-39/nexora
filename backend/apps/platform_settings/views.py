from django.conf import settings
from rest_framework.decorators import api_view,permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from .models import PlatformConfiguration
@api_view(['GET'])
@permission_classes([AllowAny])
def public_config(request):
 c=PlatformConfiguration.objects.first()
 data={} if not c else {k:getattr(c,k) for k in ['organization_name','app_name','short_app_name','phone','address','website','primary_color','secondary_color','privacy_policy','terms','community_rules','about']}
 data['push_public_key']=settings.PUSH_PUBLIC_KEY
 from .models import BrandingAsset
 assets=set(BrandingAsset.objects.values_list('kind',flat=True));data['logo']=request.build_absolute_uri('/api/public/branding/logo/') if 'LOGO' in assets else None;data['favicon']=request.build_absolute_uri('/api/public/branding/favicon/') if 'FAVICON' in assets else None
 return Response({'success':True,'message':'Configuration retrieved','data':data})
