import os,uuid
from PIL import Image,UnidentifiedImageError
from django.core.files.storage import default_storage
from django.db import transaction
from django.http import FileResponse,Http404
from rest_framework.decorators import api_view,parser_classes,permission_classes
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.exceptions import PermissionDenied,ValidationError
from rest_framework.response import Response
from apps.media.validators import sniff,IMAGE_TYPES,storage_key
from .models import BrandingAsset
from apps.audit.services import record
@api_view(['POST'])
@parser_classes([MultiPartParser])
def upload_branding(request,kind=None):
 """Upload the organization logo or favicon (administrator only).

 The image is validated by content signature, extension and decodability
 before it is stored under a generated key; the client filename never
 reaches the filesystem.
 """
 if request.user.role!='ADMIN':raise PermissionDenied()
 kind=str(kind or request.data.get('kind','')).upper();f=request.FILES.get('file') or request.FILES.get('image')
 if kind not in ('LOGO','FAVICON') or not f:raise ValidationError('A logo or favicon file is required.')
 if f.size<=0 or f.size>5*1024**2:raise ValidationError('Branding image exceeds 5 MB.')
 mime=sniff(f);ext=os.path.splitext(f.name)[1].lower()
 if mime not in IMAGE_TYPES or ext not in IMAGE_TYPES[mime]:raise ValidationError('Invalid branding image content or extension.')
 try:
  image=Image.open(f);image.verify();f.seek(0);image=Image.open(f);w,h=image.size
  if w<16 or h<16 or w*h>16_000_000:raise ValidationError('Branding image dimensions are not permitted.')
  f.seek(0)
 except (UnidentifiedImageError,OSError):raise ValidationError('Invalid branding image.')
 key=storage_key(f'branding/{kind.lower()}',ext);saved=default_storage.save(key,f);old=None
 try:
  with transaction.atomic():
   old=BrandingAsset.objects.filter(kind=kind).first();obj,_=BrandingAsset.objects.update_or_create(kind=kind,defaults={'storage_key':saved,'mime_type':mime,'size':f.size,'width':w,'height':h})
   if old and old.storage_key!=saved:transaction.on_commit(lambda:default_storage.delete(old.storage_key))
   record(request.user,'BRANDING_ASSET_UPDATED',obj,request,{'kind':kind})
   from .services import invalidate
   transaction.on_commit(invalidate)
 except Exception:default_storage.delete(saved);raise
 return Response({'success':True,'message':'Branding image updated','data':{'kind':kind,'url':f'/api/public/branding/{kind.lower()}/'}},status=201)
@api_view(['GET'])
@permission_classes([AllowAny])
def public_branding(request,kind):
 try:asset=BrandingAsset.objects.get(kind=kind.upper())
 except BrandingAsset.DoesNotExist:raise Http404
 try:response=FileResponse(default_storage.open(asset.storage_key,'rb'),content_type=asset.mime_type)
 except FileNotFoundError:raise Http404
 response['Cache-Control']='public, max-age=300';response['X-Content-Type-Options']='nosniff';return response
