"""
NEXORA — security event log and security policy.

Security policy values live in the organization's database configuration and
are enforced by the backend (sign-in lockout, PIN age, session idle window).
Infrastructure secrets are never readable or writable here.
"""

from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import record
from apps.platform_settings.admin_views import ensure_configuration
from apps.platform_settings.services import invalidate

from .models import SecurityEvent
from .serializers import SecurityEventSerializer


class SecurityEventListView(ListAPIView):
    serializer_class = SecurityEventSerializer

    def get_queryset(self):
        if self.request.user.role != "ADMIN":
            raise PermissionDenied()
        queryset = SecurityEvent.objects.select_related("user").order_by("-created_at")
        event = self.request.query_params.get("event")
        user = self.request.query_params.get("user")
        if event:
            queryset = queryset.filter(event=event)
        if user:
            queryset = queryset.filter(user_id=user)
        return queryset


FIELDS = {
    "max_login_attempts": ("login_failure_limit", 3, 20),
    "lockout_minutes": ("lockout_minutes", 1, 1440),
    "session_idle_minutes": ("session_idle_minutes", 5, 10080),
    "login_rate_window_minutes": ("login_rate_window_minutes", 1, 120),
    "pin_max_age_days": ("pin_max_age_days", 0, 3650),
}


class SecuritySettingsView(APIView):
    """GET/PATCH the administrator-controlled security policy."""

    def _require_admin(self, request):
        if request.user.role != "ADMIN":
            raise PermissionDenied()

    def _payload(self, row):
        return {key: getattr(row, field) for key, (field, _, _) in FIELDS.items()}

    def get(self, request):
        self._require_admin(request)
        return Response(
            {"success": True, "message": "Security policy retrieved", "data": self._payload(ensure_configuration())}
        )

    def patch(self, request):
        self._require_admin(request)
        row = ensure_configuration()
        changed = []
        for key, (field, low, high) in FIELDS.items():
            if key not in request.data:
                continue
            try:
                value = int(request.data[key])
            except (TypeError, ValueError):
                raise ValidationError({key: "Provide a whole number."}) from None
            if not low <= value <= high:
                raise ValidationError({key: f"Must be between {low} and {high}."})
            setattr(row, field, value)
            changed.append(field)
        if not changed:
            raise ValidationError("No recognised security settings were supplied.")
        row.save(update_fields=[*changed, "updated_at"])
        invalidate()
        record(request.user, "SECURITY_POLICY_UPDATED", row, request, {"fields": sorted(changed)})
        return Response(
            {"success": True, "message": "Security policy updated", "data": self._payload(row)}
        )
