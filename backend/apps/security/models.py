import uuid
from django.conf import settings
from django.db import models
class SecurityEvent(models.Model):
 id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False);user=models.ForeignKey(settings.AUTH_USER_MODEL,null=True,on_delete=models.SET_NULL);event=models.CharField(max_length=60,db_index=True);ip_address=models.GenericIPAddressField(null=True);metadata=models.JSONField(default=dict);created_at=models.DateTimeField(auto_now_add=True,db_index=True)
