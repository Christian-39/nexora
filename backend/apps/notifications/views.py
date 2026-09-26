from django.utils import timezone
from django.db import transaction
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import Notification,PushSubscription
from .serializers import NotificationSerializer,PushSerializer
from apps.core.throttles import PushThrottle
class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
 serializer_class=NotificationSerializer
 def get_queryset(self):return Notification.objects.filter(recipient=self.request.user).order_by('-created_at')
 @action(detail=False,methods=['post'])
 @transaction.atomic
 def read(self,request):
  ids=request.data.get('ids',[]);Notification.objects.filter(recipient=request.user,id__in=ids,read_at__isnull=True).update(read_at=timezone.now());return Response({'success':True,'message':'Notifications marked read','data':{}})
class PushViewSet(viewsets.ModelViewSet):
 serializer_class=PushSerializer;http_method_names=['get','post','delete'];throttle_classes=[PushThrottle]
 def get_queryset(self):return PushSubscription.objects.filter(user=self.request.user,is_active=True)
 def perform_create(self,s):s.save(user=self.request.user)
