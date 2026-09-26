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
errorlog = "-"
capture_output = True

max_requests = 2000
max_requests_jitter = 200

#: Behind the platform's TLS proxy. X-Forwarded-Proto must be honoured or
#: Django will consider every request insecure and WebSocket upgrades from
#: https pages will be refused.
forwarded_allow_ips = os.getenv("FORWARDED_ALLOW_IPS", "*")
