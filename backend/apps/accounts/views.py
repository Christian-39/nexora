"""
NEXORA — authentication, profile and member administration.

Authentication is cookie-based: the access and refresh JWTs live in HttpOnly
cookies, CSRF is enforced on every unsafe request, and every access token
carries the id of a ``DeviceSession`` row so a session can be revoked
server-side immediately.

Only an administrator can create, modify, activate, deactivate or reset a
member. Members can never enumerate other members.
"""

from __future__ import annotations

import datetime

from django.conf import settings
from django.contrib.auth import authenticate
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse, Http404
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import viewsets
from rest_framework.authentication import CSRFCheck
from rest_framework.decorators import action, api_view, parser_classes, permission_classes, throttle_classes
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from apps.audit.services import record
from apps.core.throttles import CredentialThrottle, LoginThrottle
from apps.security.services import event, ip_hash

from .models import DeviceSession, User
from .serializers import (
    MemberCreateSerializer,
    MemberUpdateSerializer,
    PinSerializer,
    PreferencesSerializer,
    ProfileSerializer,
    UserSerializer,
    presence_online_map,
)
from .services import create_member, initial_pin, normalize_phone, register_failure
from .session_serializers import SessionSerializer


def envelope(message, data=None, status=200):
    return Response({"success": True, "message": message, "data": data if data is not None else {}}, status=status)


def failure(message, code, status=400, errors=None):
    return Response(
        {"success": False, "message": message, "code": code, "errors": errors or {}}, status=status
    )


def _set_cookies(response, refresh) -> None:
    """Write the HttpOnly session cookies.

    The refresh cookie is scoped to ``/api/auth/`` so it is never sent with
    ordinary API traffic, shrinking its exposure.
    """
    common = {
        "httponly": True,
        "secure": settings.COOKIE_SECURE,
        "samesite": settings.COOKIE_SAMESITE,
        "domain": settings.COOKIE_DOMAIN,
    }
    response.set_cookie(
        settings.ACCESS_COOKIE,
        str(refresh.access_token),
        max_age=settings.ACCESS_TOKEN_MINUTES * 60,
        path="/",
        **common,
    )
    response.set_cookie(
        settings.REFRESH_COOKIE,
        str(refresh),
        max_age=settings.REFRESH_TOKEN_DAYS * 86400,
        path=settings.REFRESH_COOKIE_PATH,
        **common,
    )


def _clear_cookies(response) -> None:
    response.delete_cookie(settings.ACCESS_COOKIE, path="/", domain=settings.COOKIE_DOMAIN)
    response.delete_cookie(
        settings.REFRESH_COOKIE, path=settings.REFRESH_COOKIE_PATH, domain=settings.COOKIE_DOMAIN
    )


def _require_csrf(request) -> None:
    check = CSRFCheck(lambda req: None)
    check.process_request(request._request)
    if check.process_view(request._request, None, (), {}):
        raise PermissionDenied("CSRF validation failed.")


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@api_view(["GET"])
@permission_classes([AllowAny])
@ensure_csrf_cookie
def csrf(request):
    """Bootstrap the CSRF cookie before the first unsafe request."""
    from django.middleware.csrf import get_token

    return envelope("CSRF cookie set", {"csrf_token": get_token(request._request)})


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([LoginThrottle])
def login(request):
    # Login is CSRF-protected too: DRF exempts views from the CSRF middleware,
    # so the check is made explicitly here (defends against login-CSRF).
    _require_csrf(request)
    from apps.platform_settings.services import messaging_policy

    policy = messaging_policy()
    raw_phone = request.data.get("phone") or request.data.get("identifier") or ""
    pin = str(request.data.get("pin") or request.data.get("password") or "")

    try:
        phone = normalize_phone(raw_phone)
    except Exception:  # noqa: BLE001 - never reveal which half was wrong
        phone = ""

    found = User.objects.filter(phone=phone).first() if phone else None

    # A generic message for every failure mode: no account enumeration.
    generic = failure("Invalid credentials or temporarily unavailable.", "AUTH_FAILED", 401)

    if found and found.locked_until and found.locked_until > timezone.now():
        event("LOGIN_LOCKED", request, found)
        return generic

    user = authenticate(request, phone=phone, password=pin) if phone else None
    if not user or not user.is_active:
        register_failure(found, limit=policy["login_failure_limit"], minutes=policy["lockout_minutes"])
        event("LOGIN_FAILURE", request, found)
        return generic

    with transaction.atomic():
        User.objects.filter(pk=user.pk).update(failed_login_count=0, locked_until=None)
        refresh = RefreshToken.for_user(user)
        session = DeviceSession.objects.create(
            user=user,
            jti=str(refresh["jti"]),
            device_label=str(request.data.get("device_label", ""))[:120],
            user_agent=str(request.headers.get("User-Agent", ""))[:200],
            ip_hash=ip_hash(request),
            expires_at=timezone.datetime.fromtimestamp(refresh["exp"], tz=datetime.timezone.utc),
        )
        refresh["sid"] = str(session.id)

    event("LOGIN_SUCCESS", request, user)
    response = envelope("Login successful", ProfileSerializer(user, context={"request": request}).data)
    _set_cookies(response, refresh)
    return response


@api_view(["POST"])
@permission_classes([AllowAny])
def refresh(request):
    _require_csrf(request)
    token_value = request.COOKIES.get(settings.REFRESH_COOKIE)
    if not token_value:
        return failure("Session expired.", "INVALID_SESSION", 401)
    try:
        token = RefreshToken(token_value)
        session = DeviceSession.objects.get(
            id=token.get("sid"),
            user_id=token["user_id"],
            jti=str(token["jti"]),
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        )
        if not session.user.is_active:
            raise DeviceSession.DoesNotExist
        # Rotate: the presented refresh token is replaced and blacklisted.
        token.blacklist()
        token.set_jti()
        token.set_exp()
        token.set_iat()
        token["sid"] = str(session.id)
        session.jti = str(token["jti"])
        session.expires_at = timezone.datetime.fromtimestamp(token["exp"], tz=datetime.timezone.utc)
        session.save(update_fields=["jti", "expires_at", "last_active"])
    except (TokenError, KeyError, TypeError, ValueError, DeviceSession.DoesNotExist):
        response = failure("Session expired.", "INVALID_SESSION", 401)
        _clear_cookies(response)
        return response

    response = envelope("Token refreshed")
    _set_cookies(response, token)
    return response


@api_view(["POST"])
def logout(request):
    try:
        token = RefreshToken(request.COOKIES.get(settings.REFRESH_COOKIE))
        token.blacklist()
        DeviceSession.objects.filter(id=token.get("sid"), user=request.user).update(
            revoked_at=timezone.now()
        )
    except Exception:  # noqa: BLE001 - logout always succeeds for the client
        DeviceSession.objects.filter(user=request.user, revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )
    event("LOGOUT", request, request.user)
    response = envelope("Logged out")
    _clear_cookies(response)
    return response


@api_view(["POST"])
@throttle_classes([CredentialThrottle])
def change_pin(request):
    serializer = PinSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    if not request.user.check_password(serializer.validated_data["current_pin"]):
        event("CREDENTIAL_CHANGE_FAILED", request, request.user)
        return failure("Current PIN is incorrect.", "INVALID_CREDENTIAL", 400, {"current_pin": ["Incorrect PIN."]})

    with transaction.atomic():
        request.user.set_password(serializer.validated_data["new_pin"])
        request.user.credential_state = User.Credential.CHANGED
        request.user.save(update_fields=["password", "credential_state", "updated_at"])
        # Every other device must re-authenticate with the new PIN.
        request.user.device_sessions.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())

    event("CREDENTIAL_CHANGE", request, request.user)
    response = envelope("PIN changed; sign in again.")
    _clear_cookies(response)
    return response


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@api_view(["GET"])
def sessions(request):
    current_sid = str(getattr(request.auth, "payload", {}).get("sid", "")) if request.auth else ""
    rows = request.user.device_sessions.filter(revoked_at__isnull=True).order_by("-last_active")
    data = SessionSerializer(rows, many=True, context={"current_sid": current_sid}).data
    return envelope("Sessions retrieved", data)


@api_view(["DELETE"])
def revoke_session(request, session_id):
    count = request.user.device_sessions.filter(id=session_id, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )
    if not count:
        return failure("Session not found.", "NOT_FOUND", 404)
    event("SESSION_REVOKED", request, request.user, {"session_id": str(session_id)})
    return envelope("Session revoked")


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


@api_view(["GET", "PATCH"])
def me(request):
    if request.method == "PATCH":
        serializer = ProfileSerializer(request.user, data=request.data, partial=True, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return envelope("Profile updated", serializer.data)
    return envelope("Profile retrieved", ProfileSerializer(request.user, context={"request": request}).data)


@api_view(["GET", "PATCH"])
def preferences(request):
    if request.method == "PATCH":
        serializer = PreferencesSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return envelope("Preferences updated", serializer.data)
    return envelope("Preferences retrieved", PreferencesSerializer(request.user).data)


@api_view(["POST", "DELETE"])
@parser_classes([MultiPartParser, FormParser])
def avatar(request):
    from apps.media.validators import safe_display_name, sniff, storage_key
    from apps.platform_settings.services import messaging_policy

    if request.user.role != "ADMIN" and not messaging_policy()["allow_member_avatar_edit"]:
        raise PermissionDenied("Profile photo changes are disabled by the administrator.")

    if request.method == "DELETE":
        _delete_avatar(request.user)
        return envelope("Profile photo removed", ProfileSerializer(request.user, context={"request": request}).data)

    fileobj = request.FILES.get("avatar") or request.FILES.get("file")
    if fileobj is None:
        raise ValidationError({"avatar": "An image file is required."})
    if fileobj.size > 5 * 1024**2:
        raise ValidationError({"avatar": "Profile photos must be 5 MB or smaller."})

    from apps.media.validators import IMAGE_TYPES, _validate_image  # noqa: PLC2701 - internal helper reuse

    mime = sniff(fileobj)
    if mime not in IMAGE_TYPES:
        raise ValidationError({"avatar": "Upload a JPEG, PNG, WebP or GIF image."})
    _validate_image(fileobj)

    old_key = request.user.avatar_key
    key = default_storage.save(storage_key("avatars", IMAGE_TYPES[mime][0]), fileobj)
    request.user.avatar_key = key
    request.user.save(update_fields=["avatar_key", "updated_at"])
    if old_key:
        try:
            default_storage.delete(old_key)
        except OSError:  # pragma: no cover
            pass
    safe_display_name(getattr(fileobj, "name", ""))
    return envelope("Profile photo updated", ProfileSerializer(request.user, context={"request": request}).data)


def _delete_avatar(user) -> None:
    if user.avatar_key:
        try:
            default_storage.delete(user.avatar_key)
        except OSError:  # pragma: no cover
            pass
        user.avatar_key = ""
        user.save(update_fields=["avatar_key", "updated_at"])


@api_view(["GET"])
def member_avatar(request, pk):
    """Serve a member's avatar to users allowed to see that member."""
    from apps.conversations.services import visible_members_for

    user = User.objects.filter(pk=pk).first()
    if not user or not user.avatar_key:
        raise Http404
    if user.id != request.user.id and not visible_members_for(request.user).filter(pk=pk).exists():
        raise Http404
    try:
        response = FileResponse(default_storage.open(user.avatar_key, "rb"))
    except FileNotFoundError as exc:
        raise Http404 from exc
    response["Cache-Control"] = "private, max-age=300"
    response["X-Content-Type-Options"] = "nosniff"
    return response


# ---------------------------------------------------------------------------
# Member administration
# ---------------------------------------------------------------------------


class MemberViewSet(viewsets.ModelViewSet):
    serializer_class = UserSerializer
    http_method_names = ["get", "post", "patch"]

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "request": self.request}

    def list(self, request, *args, **kwargs):
        """One presence read for the whole page instead of one per row."""
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            rows = page
            wrap = self.get_paginated_response
        else:
            # Pagination disabled for this caller: return the plain envelope.
            rows = list(queryset[:200])
            wrap = lambda data: envelope("Members retrieved", data)  # noqa: E731
        context = {
            **self.get_serializer_context(),
            "presence_online_map": presence_online_map(row.id for row in rows),
        }
        serializer = self.get_serializer(rows, many=True, context=context)
        return wrap(serializer.data)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        # Member administration is administrator-only, without exception.
        # Members never enumerate other members through this surface.
        if request.user.role != User.Role.ADMIN:
            raise PermissionDenied()

    def get_queryset(self):
        if self.request.user.role == User.Role.ADMIN:
            queryset = User.objects.filter(role=User.Role.MEMBER)
            status = str(self.request.query_params.get("status", "")).lower()
            if status == "active":
                queryset = queryset.filter(is_active=True)
            elif status == "inactive":
                queryset = queryset.filter(is_active=False)
            if str(self.request.query_params.get("selectable", "")).lower() in ("1", "true"):
                queryset = queryset.filter(is_active=True)
        else:  # pragma: no cover - unreachable, initial() already refused
            queryset = User.objects.none()

        search = str(self.request.query_params.get("q") or self.request.query_params.get("search") or "").strip()
        if search:
            criteria = Q(full_name__icontains=search)
            if self.request.user.role == User.Role.ADMIN:
                criteria |= Q(phone__icontains=search)
            queryset = queryset.filter(criteria)
        return queryset.distinct().order_by("full_name", "-created_at")

    def create(self, request, *args, **kwargs):
        serializer = MemberCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            member = create_member(actor=request.user, **serializer.validated_data)
        except ValidationError:
            raise
        record(request.user, "MEMBER_CREATED", member, request)
        return envelope(
            "Member created", UserSerializer(member, context={"request": request}).data, status=201
        )

    def partial_update(self, request, *args, **kwargs):
        member = self.get_object()
        serializer = MemberUpdateSerializer(member, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        record(request.user, "MEMBER_UPDATED", member, request, {"fields": sorted(serializer.validated_data)})
        return envelope("Member updated", UserSerializer(member, context={"request": request}).data)

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        member = self.get_object()
        with transaction.atomic():
            member.is_active = False
            member.deactivated_at = timezone.now()
            member.save(update_fields=["is_active", "deactivated_at", "updated_at"])
            member.device_sessions.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())
            record(request.user, "MEMBER_DEACTIVATED", member, request)
        event("MEMBER_DEACTIVATED", request, member)
        return envelope("Member deactivated", UserSerializer(member, context={"request": request}).data)

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        member = self.get_object()
        member.is_active = True
        member.deactivated_at = None
        member.save(update_fields=["is_active", "deactivated_at", "updated_at"])
        record(request.user, "MEMBER_ACTIVATED", member, request)
        return envelope("Member activated", UserSerializer(member, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="reset-pin")
    def reset_pin(self, request, pk=None):
        member = self.get_object()
        with transaction.atomic():
            member.set_password(initial_pin(member.phone))
            member.credential_state = User.Credential.RESET_REQUIRED
            member.save(update_fields=["password", "credential_state", "updated_at"])
            member.device_sessions.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())
            record(request.user, "CREDENTIAL_RESET", member, request)
        event("CREDENTIAL_RESET", request, member)
        # The new PIN is never returned or logged: it is the documented
        # first-six-digits-of-the-phone-number rule.
        return envelope("Credential reset to the documented initial-PIN rule.")

    @action(detail=True, methods=["get"])
    def conversation(self, request, pk=None):
        """Open (or reuse) the private conversation with this member."""
        from apps.conversations.realtime import broadcast_conversation_created
        from apps.conversations.serializers import ConversationSerializer
        from apps.conversations.services import private_conversation

        target = self.get_object()
        conversation, created = private_conversation(request.user, target)
        if created:
            broadcast_conversation_created(
                conversation,
                participant_ids=[conversation.admin_id, conversation.member_id],
                request=request,
            )
        return envelope(
            "Conversation ready",
            ConversationSerializer(conversation, context={"request": request}).data,
        )

    @action(detail=True, methods=["get"])
    def activity(self, request, pk=None):
        """Recent audit/security activity for one member (administrator only)."""
        if request.user.role != User.Role.ADMIN:
            raise PermissionDenied()
        member = self.get_object()
        from apps.audit.models import AuditLog
        from apps.conversations.models import Message
        from apps.security.models import SecurityEvent

        items = []
        for row in AuditLog.objects.filter(object_id=str(member.id)).order_by("-created_at")[:25]:
            items.append({"type": "audit", "action": row.action, "at": row.created_at, "metadata": row.metadata})
        for row in SecurityEvent.objects.filter(user=member).order_by("-created_at")[:25]:
            items.append({"type": "security", "action": row.event, "at": row.created_at, "metadata": {}})
        items.sort(key=lambda x: x["at"], reverse=True)
        stats = {
            "messages_sent": Message.objects.filter(sender=member).count(),
            "last_seen": member.last_seen,
            "is_active": member.is_active,
        }
        return envelope("Member activity retrieved", {"results": items[:40], "stats": stats})


# ---------------------------------------------------------------------------
# Administrator dashboard
# ---------------------------------------------------------------------------


@api_view(["GET"])
def dashboard(request):
    """Real counters computed from the database. No decorative placeholders."""
    if request.user.role != User.Role.ADMIN:
        raise PermissionDenied()

    from apps.audit.models import AuditLog
    from apps.conversations.models import Conversation, Message, MessageReceipt
    from apps.groups.models import Group
    from apps.security.models import SecurityEvent

    since = timezone.now() - timezone.timedelta(days=7)
    # "Today" is the current calendar day in the deployment's configured
    # timezone (settings.TIME_ZONE); with USE_TZ this is the correct boundary
    # for "messages today" rather than a rolling 24h or a UTC-midnight window.
    start_today = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    members = User.objects.filter(role=User.Role.MEMBER).aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(is_active=True)),
        inactive=Count("id", filter=Q(is_active=False)),
        pending_pin=Count("id", filter=~Q(credential_state="CHANGED")),
    )
    # One aggregate query covers all message counters (all-time, 7-day and
    # today) instead of issuing a separate COUNT per metric.
    messages = Message.objects.aggregate(
        total=Count("id"),
        last_7_days=Count("id", filter=Q(created_at__gte=since)),
        media=Count("id", filter=Q(type__in=["IMAGE", "VIDEO"])),
        voice=Count("id", filter=Q(type="VOICE")),
        today=Count("id", filter=Q(created_at__gte=start_today)),
        media_today=Count("id", filter=Q(created_at__gte=start_today, type__in=["IMAGE", "VIDEO"])),
        voice_today=Count("id", filter=Q(created_at__gte=start_today, type="VOICE")),
    )
    conversations = {
        "total": Conversation.objects.filter(is_active=True).count(),
        "private": Conversation.objects.filter(is_active=True, kind="ADMIN_PRIVATE").count(),
        "groups": Group.objects.filter(is_active=True).count(),
    }
    # Distinct conversations (not raw receipts) with unread messages for this admin.
    unread_conversations = (
        MessageReceipt.objects.filter(recipient=request.user, read_at__isnull=True)
        .values("message__conversation_id")
        .distinct()
        .count()
    )
    data = {
        "members": members,
        "conversations": conversations,
        "messages": messages,
        "unread": MessageReceipt.objects.filter(recipient=request.user, read_at__isnull=True).count(),
        # ---- Flat, front-end-facing metric keys (admin.html reads these) ----
        # Kept alongside the nested structure above so existing API consumers
        # and the contract test remain valid while the dashboard renders real
        # numbers instead of the em-dash placeholders caused by the old
        # nested/flat key mismatch.
        "total_members": members["total"],
        "active_members": members["active"],
        "inactive_members": members["inactive"],
        "active_conversations": conversations["total"],
        "unread_conversations": unread_conversations,
        "total_groups": conversations["groups"],
        "messages_today": messages["today"],
        "media_today": messages["media_today"],
        "voice_notes_today": messages["voice_today"],
        "recent_activity": [
            {
                "action": row.action,
                "actor": row.actor.full_name if row.actor else None,
                "object_type": row.object_type,
                "at": row.created_at,
            }
            for row in AuditLog.objects.select_related("actor").order_by("-created_at")[:10]
        ],
        "security_events": [
            {"event": row.event, "at": row.created_at, "user": str(row.user_id) if row.user_id else None}
            for row in SecurityEvent.objects.order_by("-created_at")[:10]
        ],
    }
    return envelope("Dashboard retrieved", data)
