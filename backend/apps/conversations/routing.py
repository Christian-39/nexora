from django.urls import re_path
from .consumers import ConversationConsumer
from apps.accounts.presence import PresenceConsumer
websocket_urlpatterns=[re_path(r'^ws/presence/$',PresenceConsumer.as_asgi()),re_path(r'^ws/conversations/(?P<conversation_id>[0-9a-f-]+)/$',ConversationConsumer.as_asgi())]
