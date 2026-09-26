from rest_framework import serializers
from .models import Notification,PushSubscription
class NotificationSerializer(serializers.ModelSerializer):
 class Meta:model=Notification;fields=['id','type','title','message','related_id','read_at','created_at'];read_only_fields=fields
class PushSerializer(serializers.ModelSerializer):
 class Meta:model=PushSubscription;fields=['id','endpoint','p256dh','auth','device_label'];read_only_fields=['id']
