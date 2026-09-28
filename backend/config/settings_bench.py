"""Local benchmark overlay — NOT for production.

Adds per-request SQL instrumentation and connection-open logging so the
production latency model can be verified empirically under the real ASGI
stack (gunicorn + UvicornWorker), a real MySQL wire protocol, and a
latency-injecting TCP proxy.
"""
from .settings import *  # noqa: F401,F403
import os

# Point the DB at the latency proxy when BENCH_DB_PORT is set.
_port = os.environ.get("BENCH_DB_PORT")
if _port:
    DATABASES["default"]["HOST"] = "127.0.0.1"  # noqa: F405
    DATABASES["default"]["PORT"] = _port  # noqa: F405

_cma = os.environ.get("BENCH_CONN_MAX_AGE")
if _cma is not None:
    DATABASES["default"]["CONN_MAX_AGE"] = None if _cma == "none" else int(_cma)  # noqa: F405

if os.environ.get("BENCH_HEALTH_CHECKS") == "1":
    DATABASES["default"]["CONN_HEALTH_CHECKS"] = True  # noqa: F405

MIDDLEWARE = ["apps.core.bench_middleware.BenchMiddleware"] + MIDDLEWARE  # noqa: F405
