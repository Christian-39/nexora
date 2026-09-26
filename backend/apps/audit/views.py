from rest_framework.generics import ListAPIView
from rest_framework.exceptions import PermissionDenied,ValidationError
from django.utils.dateparse import parse_date
from .models import AuditLog
from .serializers import AuditSerializer
class AuditListView(ListAPIView):
 serializer_class=AuditSerializer
 def get_queryset(self):
  if self.request.user.role!='ADMIN':raise PermissionDenied()
  qs=AuditLog.objects.select_related('actor').order_by('-created_at');p=self.request.query_params
  if p.get('actor'):qs=qs.filter(actor_id=p['actor'])
  if p.get('action'):qs=qs.filter(action=p['action'])
  if p.get('object_type'):qs=qs.filter(object_type=p['object_type'])
  for name,lookup in (('date_from','created_at__date__gte'),('date_to','created_at__date__lte')):
   if p.get(name):
    value=parse_date(p[name])
    if not value:raise ValidationError({name:'Use YYYY-MM-DD.'})
    qs=qs.filter(**{lookup:value})
  return qs
