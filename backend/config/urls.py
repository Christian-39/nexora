from django.urls import path,include
from rest_framework.routers import DefaultRouter
from apps.accounts.views import login,logout,refresh,change_pin,csrf,me,sessions,revoke_session,MemberViewSet
from apps.conversations.views import ConversationViewSet,search_messages,message_detail,reaction,unread_counts
from apps.groups.views import GroupViewSet
from apps.platform_settings.views import public_config
from apps.platform_settings.admin_views import PlatformSettingsView
from apps.platform_settings.assets import upload_branding,public_branding
from apps.notifications.views import NotificationViewSet,PushViewSet
from apps.audit.views import AuditListView
from apps.media.views import upload as media_upload,download as media_download
from apps.media.multipart import initiate as upload_initiate,sign_part as upload_part,complete as upload_complete,detail as upload_detail
from apps.security.views import SecurityEventListView
from apps.core.health import live,ready
r=DefaultRouter();r.register('members',MemberViewSet,basename='members');r.register('conversations',ConversationViewSet,basename='conversations');r.register('groups',GroupViewSet,basename='groups');r.register('notifications',NotificationViewSet,basename='notifications');r.register('push',PushViewSet,basename='push')
urlpatterns=[path('health/live/',live),path('health/ready/',ready),path('api/auth/csrf/',csrf),path('api/auth/login/',login),path('api/auth/logout/',logout),path('api/auth/refresh/',refresh),path('api/auth/change-pin/',change_pin),path('api/me/',me),path('api/auth/sessions/',sessions),path('api/auth/sessions/<uuid:session_id>/',revoke_session),path('api/unread/',unread_counts),path('api/messages/search/',search_messages),path('api/messages/<uuid:message_id>/',message_detail),path('api/messages/<uuid:message_id>/reaction/',reaction),path('api/public/config/',public_config),path('api/public/branding/<str:kind>/',public_branding),path('api/settings/branding/',upload_branding),path('api/settings/',PlatformSettingsView.as_view()),path('api/audit/',AuditListView.as_view()),path('api/security/',SecurityEventListView.as_view()),path('api/media/',media_upload),path('api/uploads/',upload_initiate),path('api/uploads/<uuid:session_id>/',upload_detail),path('api/uploads/<uuid:session_id>/part/',upload_part),path('api/uploads/<uuid:session_id>/complete/',upload_complete),path('api/media/<uuid:attachment_id>/',media_download),path('api/',include(r.urls))]
