"""
NEXORA — Gunicorn configuration for the ASGI application.

    gunicorn config.asgi:application -k uvicorn.workers.UvicornWorker -c gunicorn.conf.py

The worker class MUST be a uvicorn worker: a synchronous Gunicorn worker cannot
speak the WebSocket protocol, and NEXORA's realtime layer (Django Channels)
would silently disappear.
"""

import os

#: Platforms such as Render assign the port through $PORT and health-check the
#: service by connecting to it. Hard-coding 8000 makes the deploy time out.
bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"

worker_class = os.getenv("GUNICORN_WORKER_CLASS", "uvicorn.workers.UvicornWorker")
workers = int(os.getenv("WEB_CONCURRENCY", "2"))
worker_tmp_dir = "/dev/shm" if os.path.isdir("/dev/shm") else None

timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
graceful_timeout = 30
keepalive = 5

accesslog = "-"
# Do not log %(r)s: Gunicorn's default request line includes the query string,
# which may contain private search terms. %(U)s is the path without parameters.
access_log_format = '%(t)s %(h)s "%(m)s %(U)s" %(s)s %(L)s'
errorlog = "-"
capture_output = True

max_requests = 2000
max_requests_jitter = 200

# The ASGI SecurityRelayBoundary validates the private relay token and then
# trusts the relay's fixed X-Forwarded-Proto value. Disable Uvicorn's generic
# proxy-header parser so arbitrary network peers cannot rewrite client/scheme
# metadata before that authentication boundary runs.
proxy_headers = False
