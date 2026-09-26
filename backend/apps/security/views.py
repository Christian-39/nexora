from rest_framework.generics import ListAPIView
from rest_framework.exceptions import PermissionDenied
from .models import SecurityEvent
from .serializers import SecurityEventSerializer
class SecurityEventListView(ListAPIView):
 serializer_class=SecurityEventSerializer
 def get_queryset(self):
  if self.request.user.role!='ADMIN':raise PermissionDenied()
  qs=SecurityEvent.objects.select_related('user').order_by('-created_at');event=self.request.query_params.get('event');user=self.request.query_params.get('user')
  if event:qs=qs.filter(event=event)
  if user:qs=qs.filter(user_id=user)
  return qs
