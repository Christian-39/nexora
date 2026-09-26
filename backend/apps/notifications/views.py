"""NEXORA — notification centre and web-push subscription endpoints."""

from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.core.throttles import PushThrottle

from .models import Notification, PushSubscription
from .serializers import NotificationSerializer, PushSerializer


def envelope(message, data=None, status=200):
    return Response({"success": True, "message": message, "data": data if data is not None else {}}, status=status)


class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = NotificationSerializer

    def get_queryset(self):
        queryset = Notification.objects.filter(recipient=self.request.user).order_by("-created_at")
        if str(self.request.query_params.get("unread", "")).lower() in ("1", "true"):
            queryset = queryset.filter(read_at__isnull=True)
        return queryset

    @action(detail=False, methods=["post"])
    @transaction.atomic
    def read(self, request):
        queryset = Notification.objects.filter(recipient=request.user, read_at__isnull=True)
        if not request.data.get("all"):
            ids = [str(x) for x in (request.data.get("ids") or [])][:500]
            if not ids:
                raise ValidationError({"ids": "Provide notification ids or all=true."})
            queryset = queryset.filter(id__in=ids)
        updated = queryset.update(read_at=timezone.now())

        from apps.conversations.realtime import emit_to_users

        emit_to_users([request.user.id], "notification.read", {"count": updated})
        return envelope("Notifications marked read", {"updated": updated})

    @action(detail=False, methods=["get"], url_path="unread-count")
    def unread_count(self, request):
        from apps.conversations.views import _unread_payload

        return envelope("Unread count retrieved", _unread_payload(request.user))


class PushViewSet(viewsets.ModelViewSet):
    """Web push subscriptions for the signed-in user only."""

    serializer_class = PushSerializer
    http_method_names = ["get", "post", "delete"]
    throttle_classes = [PushThrottle]

    def get_queryset(self):
        return PushSubscription.objects.filter(user=self.request.user, is_active=True)

    def list(self, request, *args, **kwargs):
        """Push capability + the public VAPID key (never the private key)."""
        from apps.platform_settings.services import messaging_policy

        return envelope(
            "Push configuration retrieved",
            {
                "enabled": bool(settings.PUSH_PUBLIC_KEY) and messaging_policy()["push_enabled"],
                "vapid_public_key": settings.PUSH_PUBLIC_KEY or None,
                "public_key": settings.PUSH_PUBLIC_KEY or None,
                "subscriptions": PushSerializer(self.get_queryset(), many=True).data,
            },
        )

    @action(detail=False, methods=["post"])
    def subscribe(self, request):
        raw = request.data.get("subscription") or request.data
        endpoint = str(raw.get("endpoint") or request.data.get("endpoint") or "").strip()
        keys = raw.get("keys") or {}
        p256dh = str(keys.get("p256dh") or request.data.get("p256dh") or "").strip()
        auth = str(keys.get("auth") or request.data.get("auth") or "").strip()

        if not endpoint.startswith("https://") or len(endpoint) > 1000:
            raise ValidationError({"endpoint": "A valid HTTPS push endpoint is required."})
        if not p256dh or not auth:
            raise ValidationError({"keys": "Both p256dh and auth keys are required."})

        subscription, created = PushSubscription.objects.update_or_create(
            endpoint=endpoint,
            defaults={
                "user": request.user,
                "p256dh": p256dh[:255],
                "auth": auth[:255],
                "user_agent": str(request.data.get("user_agent", ""))[:200],
                "device_label": str(request.data.get("device_label", ""))[:120],
                "is_active": True,
            },
        )
        return envelope(
            "Push subscription registered", PushSerializer(subscription).data, status=201 if created else 200
        )

    @action(detail=False, methods=["post"])
    def unsubscribe(self, request):
        endpoint = str(request.data.get("endpoint", "")).strip()
        if not endpoint:
            raise ValidationError({"endpoint": "An endpoint is required."})
        updated = PushSubscription.objects.filter(user=request.user, endpoint=endpoint).update(
            is_active=False
        )
        return envelope("Push subscription removed", {"removed": updated})

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])
