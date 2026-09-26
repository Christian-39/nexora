"""
NEXORA — durable web-push worker.

    python manage.py push_worker           # long-running
    python manage.py push_worker --once    # drain and exit

Claims deliveries with SELECT ... FOR UPDATE SKIP LOCKED so multiple workers
can run concurrently. Failed deliveries back off exponentially; endpoints that
report 404/410 are pruned.
"""

import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.notifications.models import PushDelivery
from apps.notifications.services import deliver


class Command(BaseCommand):
    help = "Process durable web-push deliveries."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--sleep", type=float, default=2.0)
        parser.add_argument("--batch", type=int, default=50)

    def handle(self, *args, **options):
        if not settings.PUSH_PRIVATE_KEY:
            self.stderr.write("PUSH_PRIVATE_KEY is not configured; no deliveries will be attempted.")
            return
        while True:
            jobs = self._claim(options["batch"])
            for job in jobs:
                deliver(job)
            if options["once"]:
                if not jobs:
                    break
                continue
            if not jobs:
                time.sleep(options["sleep"])

    def _claim(self, batch):
        now = timezone.now()
        with transaction.atomic():
            return list(
                PushDelivery.objects.select_for_update(skip_locked=True)
                .select_related("notification", "subscription")
                .filter(Q(state="PENDING") | Q(state="RETRY", next_attempt_at__lte=now))
                .order_by("created_at")[:batch]
            )
