import uuid
from django.conf import settings
from django.db import models
class AuditLog(models.Model):
 id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False);actor=models.ForeignKey(settings.AUTH_USER_MODEL,null=True,on_delete=models.SET_NULL);action=models.CharField(max_length=80,db_index=True);object_type=models.CharField(max_length=80);object_id=models.CharField(max_length=64);ip_address=models.GenericIPAddressField(null=True);metadata=models.JSONField(default=dict);created_at=models.DateTimeField(auto_now_add=True,db_index=True)
