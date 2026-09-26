from .models import AuditLog
def record(actor,action,obj,request=None,metadata=None):
 safe={k:v for k,v in (metadata or {}).items() if k.lower() not in {'pin','password','token','secret'}}
 ip=request.META.get('REMOTE_ADDR') if request else None
 return AuditLog.objects.create(actor=actor,action=action,object_type=obj.__class__.__name__,object_id=str(obj.pk),ip_address=ip,metadata=safe)
