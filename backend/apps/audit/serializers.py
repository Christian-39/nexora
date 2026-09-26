from rest_framework import serializers
from .models import AuditLog
class AuditSerializer(serializers.ModelSerializer):
 class Meta:model=AuditLog;fields=['id','actor','action','object_type','object_id','ip_address','metadata','created_at'];read_only_fields=fields
