"""
NEXORA — URL map (the authoritative API contract).

Everything is under ``/api/`` except the health probes. The frontend's
``assets/js/api.js`` mirrors this list exactly; if the two ever disagree, this
file wins.

    AUTH        /api/auth/csrf|login|logout|refresh|change-pin/
                /api/auth/sessions/  /api/auth/sessions/{uuid}/
    IDENTITY    /api/me/  /api/me/avatar/  /api/me/preferences/
    MEMBERS     /api/members/ ... /{uuid}/{activate,deactivate,reset-pin,
                conversation,activity,avatar}/
    CHAT        /api/conversations/ ... /{uuid}/{messages,read,typing,media}/
                /api/conversations/unread-summary/
    MESSAGES    /api/messages/{uuid}/  /reactions/  /api/messages/status/
                /api/messages/search/  /api/search/
    GROUPS      /api/groups/ ... /{uuid}/{members,leave,archive,unarchive,
                activity}/  /members/{uuid}/
    MEDIA       /api/media/{uuid}/  /api/media/{uuid}/url/  /api/media/
                /api/uploads/ ...
    NOTIFY      /api/notifications/  /read/  /unread-count/  /api/unread/
    PUSH        /api/push/  /subscribe/  /unsubscribe/
    ADMIN       /api/settings/  /policies/  /branding/  /assets/{kind}/
                /api/audit/  /api/security/  /api/security/events/
                /api/dashboard/
    PUBLIC      /api/public/config/  /api/public/branding/{kind}/
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.accounts.views import (
    MemberViewSet,
    avatar,
    change_pin,
    csrf,
    dashboard,
    login,
    logout,
    me,
    member_avatar,
    preferences,
    refresh,
    revoke_session,
    sessions,
)
from apps.audit.views import AuditListView
from apps.conversations.views import (
    ConversationViewSet,
    message_detail,
    message_status,
    reaction,
    search_messages,
    unread_counts,
)
from apps.core.health import live, ready
from apps.groups.views import GroupViewSet
from apps.media.multipart import complete as upload_complete
from apps.media.multipart import detail as upload_detail
from apps.media.multipart import initiate as upload_initiate
from apps.media.multipart import sign_part as upload_part
from apps.media.views import download as media_download
from apps.media.views import media_url
from apps.media.views import upload as media_upload
from apps.notifications.views import NotificationViewSet, PushViewSet
from apps.platform_settings.admin_views import PlatformSettingsView, PolicyView
from apps.platform_settings.assets import public_branding, upload_branding
from apps.platform_settings.views import public_config
from apps.security.views import SecurityEventListView, SecuritySettingsView

router = DefaultRouter()
router.register("members", MemberViewSet, basename="members")
router.register("conversations", ConversationViewSet, basename="conversations")
router.register("groups", GroupViewSet, basename="groups")
router.register("notifications", NotificationViewSet, basename="notifications")
router.register("push", PushViewSet, basename="push")

urlpatterns = [
    # --- health ---------------------------------------------------------
    path("health/live/", live),
    path("health/ready/", ready),
    # --- authentication --------------------------------------------------
    path("api/auth/csrf/", csrf),
    path("api/auth/login/", login),
    path("api/auth/logout/", logout),
    path("api/auth/refresh/", refresh),
    path("api/auth/change-pin/", change_pin),
    path("api/auth/sessions/", sessions),
    path("api/auth/sessions/<uuid:session_id>/", revoke_session),
    # --- identity ---------------------------------------------------------
    path("api/me/", me),
    path("api/me/avatar/", avatar),
    path("api/me/preferences/", preferences),
    path("api/members/<uuid:pk>/avatar/", member_avatar),
    # --- messaging --------------------------------------------------------
    path("api/unread/", unread_counts),
    path("api/messages/search/", search_messages),
    path("api/search/", search_messages),
    path("api/messages/status/", message_status),
    path("api/messages/<uuid:message_id>/", message_detail),
    path("api/messages/<uuid:message_id>/reaction/", reaction),
    path("api/messages/<uuid:message_id>/reactions/", reaction),
    # --- media ------------------------------------------------------------
    path("api/media/", media_upload),
    path("api/media/<uuid:attachment_id>/", media_download),
    path("api/media/<uuid:attachment_id>/url/", media_url),
    path("api/uploads/", upload_initiate),
    path("api/uploads/<uuid:session_id>/", upload_detail),
    path("api/uploads/<uuid:session_id>/part/", upload_part),
    path("api/uploads/<uuid:session_id>/complete/", upload_complete),
    # --- administration ---------------------------------------------------
    path("api/dashboard/", dashboard),
    path("api/settings/", PlatformSettingsView.as_view()),
    path("api/settings/policies/", PolicyView.as_view()),
    path("api/settings/branding/", upload_branding),
    path("api/settings/assets/<str:kind>/", upload_branding),
    path("api/audit/", AuditListView.as_view()),
    path("api/security/", SecuritySettingsView.as_view()),
    path("api/security/events/", SecurityEventListView.as_view()),
    # --- public -----------------------------------------------------------
    path("api/public/config/", public_config),
    path("api/public/branding/<str:kind>/", public_branding),
    # --- routed viewsets --------------------------------------------------
    path("api/", include(router.urls)),
]
