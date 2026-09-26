from django.db import connection
from django.core.cache import cache
from django.http import JsonResponse
from rest_framework.decorators import api_view,permission_classes
from rest_framework.permissions import AllowAny
@api_view(['GET'])
@permission_classes([AllowAny])
def live(request):return JsonResponse({'success':True,'message':'alive','data':{}})
@api_view(['GET'])
@permission_classes([AllowAny])
def ready(request):
 checks={}
 try:
  with connection.cursor() as cursor:cursor.execute('SELECT 1');cursor.fetchone()
  checks['database']='ok'
 except Exception:checks['database']='failed'
 try:cache.set('health:ready','1',10);checks['cache']='ok' if cache.get('health:ready')=='1' else 'failed'
 except Exception:checks['cache']='failed'
 healthy=all(x=='ok' for x in checks.values());return JsonResponse({'success':healthy,'message':'ready' if healthy else 'not ready','data':checks},status=200 if healthy else 503)
