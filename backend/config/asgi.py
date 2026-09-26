import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
from django.core.asgi import get_asgi_application
django_asgi=get_asgi_application()
from channels.routing import ProtocolTypeRouter,URLRouter
from apps.accounts.ws_auth import JWTAuthMiddleware
from apps.conversations.routing import websocket_urlpatterns
application=ProtocolTypeRouter({'http':django_asgi,'websocket':JWTAuthMiddleware(URLRouter(websocket_urlpatterns))})
