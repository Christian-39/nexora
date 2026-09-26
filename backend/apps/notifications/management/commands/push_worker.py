import time
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from apps.notifications.models import PushDelivery
from apps.notifications.services import deliver
class Command(BaseCommand):
 help='Process durable web-push deliveries.'
 def add_arguments(self,p):p.add_argument('--once',action='store_true');p.add_argument('--sleep',type=float,default=2)
 def handle(self,*args,**options):
  while True:
   with transaction.atomic():
    jobs=list(PushDelivery.objects.select_for_update(skip_locked=True).select_related('notification','subscription').filter(Q(state='PENDING')|Q(state='RETRY',next_attempt_at__lte=timezone.now())).order_by('created_at')[:50])
    for job in jobs:deliver(job)
   if options['once']:break
   if not jobs:time.sleep(options['sleep'])
