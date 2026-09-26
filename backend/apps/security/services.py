import hashlib
from .models import SecurityEvent
def client_ip(request):
 forwarded=request.META.get('HTTP_X_FORWARDED_FOR','').split(',')[0].strip()
 return forwarded or request.META.get('REMOTE_ADDR')
def ip_hash(request):
 value=client_ip(request) or ''
 return hashlib.sha256(value.encode()).hexdigest() if value else ''
def event(name,request=None,user=None,metadata=None):
 safe={k:v for k,v in (metadata or {}).items() if k.lower() not in {'pin','password','token','secret','authorization'}}
 return SecurityEvent.objects.create(user=user,event=name,ip_address=client_ip(request) if request else None,metadata=safe)
