from rest_framework import serializers
from .models import SecurityEvent
class SecurityEventSerializer(serializers.ModelSerializer):
 class Meta:model=SecurityEvent;fields=['id','user','event','ip_address','metadata','created_at'];read_only_fields=fields
