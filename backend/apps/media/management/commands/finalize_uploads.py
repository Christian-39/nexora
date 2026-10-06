"""Retry cleanup for resumable-upload staging objects.

The current UploadSession contract is OPEN → COMPLETED or ABORTED. Message
creation happens in `/api/uploads/{id}/complete/`; this worker only expires
abandoned sessions and retries staging-object deletion. It must not publish a
message from stale fields or a state that no longer exists in the model.
"""

from __future__ import annotations

import logging
import posixpath
import time

from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.cache import should_report
from apps.core.observability import sanitize
from apps.media.models import UploadSession

logger = logging.getLogger("nexora.upload")


def _staging_objects(session_id, current_key: str) -> list[str]:
    """Find the active object and any abandoned replacement parts for a session.

    New session objects share a UUID-specific prefix, so a failed delete after
    a part swap stays discoverable until this finalizer can retry it. The
    current_key also covers sessions created by older builds, whose objects
    were stored outside the per-session prefix.
    """
    prefix = f"media/staging/{session_id}"
    keys = {current_key} if current_key else set()
    pending = [prefix]
    visited = set()
    while pending:
        directory = pending.pop()
        if directory in visited:
            continue
        visited.add(directory)
        try:
            directories, files = default_storage.listdir(directory)
        except FileNotFoundError:
            continue
        for name in files:
            candidate = posixpath.normpath(posixpath.join(directory, name))
            if candidate.startswith(f"{prefix}/"):
                keys.add(candidate)
        for name in directories:
            child = posixpath.normpath(posixpath.join(directory, name))
            if child.startswith(f"{prefix}/"):
                pending.append(child)
    return sorted(keys)


class Command(BaseCommand):
    help = "Expire abandoned resumable uploads and retry staging-object cleanup."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process one cleanup batch and exit.")
        parser.add_argument("--interval", type=int, default=30, help="Idle polling interval in seconds.")
        parser.add_argument("--batch-size", type=int, default=100, help="Maximum sessions per cleanup batch.")

    def handle(self, *args, **options):
        interval = max(1, options["interval"])
        batch_size = max(1, min(1000, options["batch_size"]))
        while True:
            processed = self.sweep_batch(batch_size)
            if options["once"]:
                break
            if processed == 0:
                time.sleep(interval)

    def sweep_batch(self, batch_size: int = 100) -> int:
        """Clean a bounded number of expired or completed staging objects."""
        now = timezone.now()
        pending = (
            UploadSession.objects.filter(
                Q(state=UploadSession.State.OPEN, expires_at__lte=now)
                | Q(state__in=[UploadSession.State.COMPLETED, UploadSession.State.ABORTED], staging_key__gt="")
            )
            .order_by("expires_at")
            .values_list("id", flat=True)[:batch_size]
        )
        session_ids = list(pending)
        cleaned = 0

        for session_id in session_ids:
            with transaction.atomic():
                session = (
                    UploadSession.objects.select_for_update(skip_locked=True)
                    .filter(pk=session_id)
                    .first()
                )
                if session is None:
                    continue
                if session.state == UploadSession.State.OPEN:
                    if session.expires_at > now:
                        continue
                    session.state = UploadSession.State.ABORTED
                    session.save(update_fields=["state", "updated_at"])
                key = session.staging_key
                user_id = str(session.user_id)
                session_state = session.state

            if not key:
                continue
            try:
                objects = _staging_objects(session_id, key)
                for staging_key in objects:
                    default_storage.delete(staging_key)
                cleared = UploadSession.objects.filter(pk=session_id, staging_key=key).update(
                    staging_key="", updated_at=timezone.now()
                )
            except Exception as exc:  # noqa: BLE001 - retry on the next sweep
                if should_report(f"upload-finalizer:{exc.__class__.__name__}"):
                    logger.warning(
                        "upload staging cleanup failed session=%s user=%s state=%s storage_op=list_delete_or_mark exception=%s message=%s",
                        session_id,
                        user_id,
                        session_state,
                        exc.__class__.__name__,
                        sanitize(exc, limit=300),
                        extra={"user_id": user_id},
                    )
                continue

            if not cleared:
                continue
            logger.info(
                "upload staging cleaned session=%s user=%s state=%s objects=%s storage_op=delete",
                session_id,
                user_id,
                session_state,
                len(objects),
                extra={"user_id": user_id},
            )
            cleaned += 1

        return cleaned
