from rest_framework import serializers
from .models import DeviceSession
class SessionSerializer(serializers.ModelSerializer):
 class Meta:model=DeviceSession;fields=['id','device_label','created_at','last_active','expires_at','revoked_at'];read_only_fields=fields
