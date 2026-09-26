import json
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from .models import Notification,PushSubscription,PushDelivery
def create_notification(*,recipient_id,type,title,message,related_id=None):
 with transaction.atomic():
  n=Notification.objects.create(recipient_id=recipient_id,type=type,title=title,message=message,related_id=related_id)
  subscriptions=PushSubscription.objects.filter(user_id=recipient_id,user__push_enabled=True,is_active=True)
  PushDelivery.objects.bulk_create([PushDelivery(notification=n,subscription=s) for s in subscriptions],ignore_conflicts=True)
  return n
def deliver(delivery):
 from pywebpush import webpush,WebPushException
 sub=delivery.subscription;n=delivery.notification
 payload=json.dumps({'notification_id':str(n.id),'type':n.type,'title':n.title,'body':n.message,'related_id':str(n.related_id) if n.related_id else None})
 try:
  webpush(subscription_info={'endpoint':sub.endpoint,'keys':{'p256dh':sub.p256dh,'auth':sub.auth}},data=payload,vapid_private_key=settings.PUSH_PRIVATE_KEY,vapid_claims={'sub':settings.PUSH_CONTACT},ttl=300)
  delivery.state=PushDelivery.State.SENT;delivery.last_error_code='';sub.last_used_at=timezone.now();sub.save(update_fields=['last_used_at','updated_at'])
 except WebPushException as exc:
  status=getattr(exc.response,'status_code',None);delivery.attempts+=1;delivery.last_error_code=str(status or 'WEBPUSH_ERROR')[:40]
  if status in (404,410):sub.is_active=False;sub.save(update_fields=['is_active','updated_at']);delivery.state=PushDelivery.State.FAILED
  elif delivery.attempts>=5:delivery.state=PushDelivery.State.FAILED
  else:delivery.state=PushDelivery.State.RETRY;delivery.next_attempt_at=timezone.now()+timezone.timedelta(seconds=min(3600,30*(2**delivery.attempts)))
 delivery.save(update_fields=['state','attempts','next_attempt_at','last_error_code','updated_at'])
