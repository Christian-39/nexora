from django.urls import re_path

from .consumers import AppConsumer, ConversationConsumer

UUID = r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}"

websocket_urlpatterns = [
    # Multiplexed application socket used by the frontend.
    re_path(r"^ws/app/$", AppConsumer.as_asgi()),
    # Single-thread socket (same groups, same event names).
    re_path(rf"^ws/conversations/(?P<conversation_id>{UUID})/$", ConversationConsumer.as_asgi()),
]
