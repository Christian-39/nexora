"""
NEXORA — durable media derivative worker.

    python manage.py media_worker            # long-running
    python manage.py media_worker --once     # drain the queue and exit

Claims attachments with SELECT ... FOR UPDATE SKIP LOCKED so several workers
can run side by side. Failures are retried with backoff and never destroy the
original upload.
"""

import time

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.conversations.models import Attachment
from apps.media.processing import ffmpeg_available, ffprobe_available, process_attachment


class Command(BaseCommand):
    help = "Generate image/video/voice derivatives for pending attachments."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process the backlog and exit.")
        parser.add_argument("--sleep", type=float, default=2.0)
        parser.add_argument("--batch", type=int, default=10)

    def handle(self, *args, **options):
        if not ffmpeg_available():
            self.stderr.write("ffmpeg not found: video posters will be marked FAILED (FFMPEG_MISSING).")
        if not ffprobe_available():
            self.stderr.write("ffprobe not found: media durations fall back to client-declared values.")

        while True:
            claimed = self._claim(options["batch"])
            for attachment in claimed:
                process_attachment(attachment)
            if options["once"]:
                if not claimed:
                    break
                continue
            if not claimed:
                time.sleep(options["sleep"])

    def _claim(self, batch):
        now = timezone.now()
        with transaction.atomic():
            queryset = (
                Attachment.objects.select_for_update(skip_locked=True)
                .select_related("message")
                .filter(Q(processing_state="PENDING") | Q(processing_state="RETRY", next_attempt_at__lte=now))
                .order_by("created_at")[:batch]
            )
            return list(queryset)
