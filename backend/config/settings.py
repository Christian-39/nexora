"""
NEXORA — Django settings.

ALL application/infrastructure configuration is read here, once, through the
centralized ``config(...)`` interface (python-decouple) exposed by
``config.env``. Application code must never call ``os.getenv`` /
``os.environ.get``; it reads ``django.conf.settings`` instead.

Separation of concerns
----------------------
* ENVIRONMENT configuration (this file): secrets, database, Redis, object
  storage, VAPID keys, CORS/CSRF, hosts, binaries, hard limits.
* DATABASE configuration (``apps.platform_settings``): organization branding,
  policies, messaging/media limits, group rules — editable by the administrator.

Every deployment is independent: its own database, storage bucket, secrets,
users and branding. There is no tenant registry and no shared state.
"""

from __future__ import annotations

from datetime import timedelta

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

from .env import BASE_DIR, boolean, config, csv_list

# ---------------------------------------------------------------------------
# Environment mode
# ---------------------------------------------------------------------------

#: "production" | "development" | "test". Only affects safety defaults.
DJANGO_ENV = config("DJANGO_ENV", default="development").strip().lower()
TESTING = DJANGO_ENV == "test"

DEBUG = config("DEBUG", default=(DJANGO_ENV != "production"), cast=boolean)

SECRET_KEY = config(
    "SECRET_KEY",
    default="insecure-development-key-do-not-use-in-production-0123456789",
)

ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="localhost,127.0.0.1,[::1]", cast=csv_list)

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "channels",
    "apps.core",
    "apps.accounts",
    "apps.media",
    "apps.conversations",
    "apps.groups",
    "apps.notifications",
    "apps.platform_settings",
    "apps.audit",
    "apps.security",
]

MIDDLEWARE = [
    # RequestIDMiddleware is FIRST so that every log record produced while the
    # request is handled — including ones emitted by middleware below it —
    # carries the correlation id that is echoed in X-Request-ID.
    "apps.core.middleware.RequestIDMiddleware",
    "apps.core.middleware.RequestLogMiddleware",
    "apps.core.middleware.ExceptionLogMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.core.security_headers.SecurityHeadersMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
ASGI_APPLICATION = "config.asgi.application"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
            ]
        },
    }
]

AUTH_USER_MODEL = "accounts.User"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = config("TIME_ZONE", default="UTC")
LANGUAGE_CODE = config("LANGUAGE_CODE", default="en-us")

AUTH_PASSWORD_VALIDATORS: list[dict] = []  # six-digit PINs are validated explicitly.

# ---------------------------------------------------------------------------
# PIN hashing.
#
# Django's default PBKDF2 at 1,000,000 iterations measured ~256ms on a fast
# CPU and multiples of that on a 0.5 vCPU production instance — a large part
# of the login latency. Argon2id with the OWASP-recommended parameters
# (m=19456 KiB, t=2, p=1) verifies in tens of milliseconds at equivalent or
# better security. PBKDF2 stays in the list so existing hashes keep working;
# Django transparently re-hashes each account to Argon2 on its next
# successful login.
# ---------------------------------------------------------------------------

PASSWORD_HASHERS = [
    "config.hashers.TunedArgon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
]

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
# MySQL 8+ ONLY, in every environment (local, test, production). There is no
# SQLite fallback: if the database is not configured, startup fails loudly
# instead of silently creating db.sqlite3.
#
# Preferred: DATABASE_URL (mysql://user:pass@host:3306/name).
# Alternative: discrete DATABASE_* (or DB_*) values.

DATABASE_URL = config("DATABASE_URL", default="")
MYSQL_ENGINE = "django.db.backends.mysql"
DATABASE_NAME = config("DATABASE_NAME", default=config("DB_NAME", default=""))
DATABASE_USER = config("DATABASE_USER", default=config("DB_USER", default=""))
DATABASE_PASSWORD = config("DATABASE_PASSWORD", default=config("DB_PASSWORD", default=""))
DATABASE_HOST = config("DATABASE_HOST", default=config("DB_HOST", default="127.0.0.1"))
DATABASE_PORT = config("DATABASE_PORT", default=config("DB_PORT", default=3306), cast=int)
#: External managed MySQL is reached over the network; a short max age makes a
#: low-traffic deployment pay a full TCP+auth handshake on almost every request.
#: Five minutes keeps connections warm without outliving typical server limits
#: (MySQL's default wait_timeout is 28800s).
DATABASE_CONN_MAX_AGE = config("DATABASE_CONN_MAX_AGE", default=300, cast=int)

if DATABASE_URL:
    DATABASES = {
        "default": dj_database_url.parse(
            DATABASE_URL, conn_max_age=DATABASE_CONN_MAX_AGE, engine=MYSQL_ENGINE
        )
    }
elif DATABASE_NAME:
    DATABASES = {
        "default": {
            "ENGINE": MYSQL_ENGINE,
            "NAME": DATABASE_NAME,
            "USER": DATABASE_USER,
            "PASSWORD": DATABASE_PASSWORD,
            "HOST": DATABASE_HOST,
            "PORT": str(DATABASE_PORT),
            "CONN_MAX_AGE": DATABASE_CONN_MAX_AGE,
        }
    }
elif DEBUG and DJANGO_ENV != "production":
    DATABASES = {
        "default": {
            "ENGINE": MYSQL_ENGINE,
            "NAME": "nexora",
            "USER": DATABASE_USER or "nexora",
            "PASSWORD": DATABASE_PASSWORD,
            "HOST": DATABASE_HOST,
            "PORT": str(DATABASE_PORT),
            "CONN_MAX_AGE": DATABASE_CONN_MAX_AGE,
        }
    }
else:
    raise ImproperlyConfigured(
        "MySQL is not configured. Set DATABASE_URL (mysql://user:pass@host:3306/name) "
        "or DATABASE_NAME/DATABASE_USER/DATABASE_PASSWORD/DATABASE_HOST/DATABASE_PORT."
    )

if DATABASES["default"]["ENGINE"] != MYSQL_ENGINE:
    raise ImproperlyConfigured(
        "Only MySQL is supported (use a mysql:// DATABASE_URL); SQLite and other engines are not allowed."
    )

# The test runner creates/drops a separate ``test_<NAME>`` database on the same
# MySQL server, so the DB user needs CREATE/DROP privileges on ``test_%``.
DATABASES["default"]["TEST"] = {"CHARSET": "utf8mb4", "COLLATION": "utf8mb4_unicode_ci"}

DATABASES["default"].setdefault("OPTIONS", {})
DATABASES["default"]["OPTIONS"].setdefault("charset", "utf8mb4")
DATABASES["default"]["OPTIONS"].setdefault("init_command", "SET sql_mode='STRICT_TRANS_TABLES'")

# ---------------------------------------------------------------------------
# Database connection pooling (MySQL).
#
# Django serves HTTP through ASGI here (Channels). Under ASGI each request is
# executed inside a fresh asgiref ThreadSensitiveContext whose single-use
# executor thread is destroyed when the request ends. Django's persistent
# connections (CONN_MAX_AGE) are *thread-local*, so they die with that thread:
# measured in production, EVERY request paid a full TCP+TLS+MySQL handshake
# (~1.3-2.1s to a remote database) despite CONN_MAX_AGE=300.
#
# A process-wide SQLAlchemy QueuePool (django-db-connection-pool) survives
# thread churn: connections are checked out per request and returned on
# close(). PRE_PING revalidates a pooled connection before reuse, replacing
# CONN_HEALTH_CHECKS; RECYCLE keeps pooled connections younger than typical
# provider wait_timeout/idle limits.
# ---------------------------------------------------------------------------

DATABASE_POOL = config("DATABASE_POOL", default=True, cast=bool)

if (
    DATABASE_POOL
    and not TESTING
    and DATABASES["default"]["ENGINE"] == "django.db.backends.mysql"
):
    DATABASES["default"]["ENGINE"] = "config.db_pool_backend"
    # Sizing note: shared MySQL hosting commonly enforces max_user_connections
    # (often 10-30) and sometimes max_connections_per_hour. The pool must stay
    # comfortably below the *user* cap across ALL processes (gunicorn web
    # workers + the three background workers). 3 persistent + 5 burst per
    # process is deliberate; raise only after checking the provider's limits.
    # Pooling also collapses the connections-per-hour figure from
    # "two per HTTP request" to "a handful per process per recycle window".
    DATABASES["default"]["POOL_OPTIONS"] = {
        "POOL_SIZE": config("DATABASE_POOL_SIZE", default=3, cast=int),
        "MAX_OVERFLOW": config("DATABASE_POOL_MAX_OVERFLOW", default=5, cast=int),
        "RECYCLE": config("DATABASE_POOL_RECYCLE", default=280, cast=int),
        "PRE_PING": True,
    }
    # The pool owns the connection lifecycle; Django-level persistence and
    # health checks must not fight it.
    DATABASES["default"]["CONN_MAX_AGE"] = 0
else:
    # Without the pool (DATABASE_POOL=false, or under test), keep Django persistent
    # connections and guard reuse with a health check so a connection killed
    # by the provider's idle timeout cannot surface as a mid-request error.
    DATABASES["default"]["CONN_HEALTH_CHECKS"] = config(
        "DATABASE_CONN_HEALTH_CHECKS", default=True, cast=bool
    )

# ---------------------------------------------------------------------------
# Static / local media root
# ---------------------------------------------------------------------------

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_ROOT = BASE_DIR / "media"
MEDIA_URL = "/media/"

# ---------------------------------------------------------------------------
# CORS / CSRF
# ---------------------------------------------------------------------------
# Development convenience: the documented local static-server origins are
# allowed automatically so the frontend works with no edits. Production must
# configure CORS_ALLOWED_ORIGINS explicitly. CORS_ALLOW_ALL_ORIGINS is never set.

LOCAL_FRONTEND_ORIGINS = [
    f"{scheme}://{host}:{port}"
    for scheme in ("http",)
    for host in ("127.0.0.1", "localhost")
    for port in (3000, 4173, 5173, 5500, 5501, 8080, 8081)
]


def normalize_origin(value: str) -> str | None:
    """Canonicalise one configured origin.

    Accepts sloppy deployment input — ``https://host/`` , ``host`` — and always
    produces ``https://host`` (or ``http://…`` only on an explicit http scheme).
    Empty values are dropped so a stray comma can never widen the allowlist.
    """
    text = str(value or "").strip().rstrip("/")
    if not text:
        return None
    if "://" not in text:
        # A bare hostname is trusted to be the production HTTPS frontend.
        text = f"https://{text}"
    scheme, _, rest = text.partition("://")
    if scheme.lower() not in ("http", "https") or not rest:
        return None
    return f"{scheme.lower()}://{rest}"


def _origin_list(*values) -> list[str]:
    out: list[str] = []
    for value in values:
        for item in (value if isinstance(value, (list, tuple)) else str(value).replace("\n", ",").split(",")):
            normalized = normalize_origin(item)
            if normalized and normalized not in out:
                out.append(normalized)
    return out


CORS_ALLOWED_ORIGINS = _origin_list(config("CORS_ALLOWED_ORIGINS", default=""))
CSRF_TRUSTED_ORIGINS = _origin_list(config("CSRF_TRUSTED_ORIGINS", default=""))

# Configure one list and the other follows: the frontend origin must be able
# to both send credentialed CORS requests and pass Django's CSRF origin check.
if CORS_ALLOWED_ORIGINS and not CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS = list(CORS_ALLOWED_ORIGINS)
elif CSRF_TRUSTED_ORIGINS and not CORS_ALLOWED_ORIGINS:
    CORS_ALLOWED_ORIGINS = list(CSRF_TRUSTED_ORIGINS)

if DEBUG:
    CORS_ALLOWED_ORIGINS = sorted(set(CORS_ALLOWED_ORIGINS) | set(LOCAL_FRONTEND_ORIGINS))
    CSRF_TRUSTED_ORIGINS = sorted(set(CSRF_TRUSTED_ORIGINS) | set(LOCAL_FRONTEND_ORIGINS))

#: WebSocket handshakes are NOT covered by the same-origin policy. With
#: SameSite=None cookies any site could otherwise open an authenticated socket
#: for a signed-in visitor, so the ASGI origin allowlist mirrors the frontend
#: origins (see config/asgi.py::OriginAllowlist).
WEBSOCKET_ALLOWED_ORIGINS = _origin_list(
    config("WEBSOCKET_ALLOWED_ORIGINS", default=""),
    CORS_ALLOWED_ORIGINS,
)

#: Render injects the service's public hostname; it must always be servable
#: (health checks hit it directly) even when ALLOWED_HOSTS lists a custom one.
_render_hostname = config("RENDER_EXTERNAL_HOSTNAME", default="").strip()
if _render_hostname and _render_hostname not in ALLOWED_HOSTS:
    ALLOWED_HOSTS = [*ALLOWED_HOSTS, _render_hostname]

CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_HEADERS = [
    "accept",
    "accept-language",
    "authorization",
    "content-type",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-request-id",
    "x-requested-with",
]
CORS_EXPOSE_HEADERS = ["X-Request-ID", "Retry-After", "Content-Range", "Accept-Ranges"]

# ---------------------------------------------------------------------------
# REST framework
# ---------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["apps.accounts.authentication.CookieJWTAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
        "apps.accounts.permissions.CredentialChangedOrAllowed",
    ],
    "DEFAULT_RENDERER_CLASSES": ["apps.core.renderers.EnvelopeJSONRenderer"],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.FormParser",
        "rest_framework.parsers.MultiPartParser",
    ],
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.StandardCursorPagination",
    "PAGE_SIZE": config("API_PAGE_SIZE", default=30, cast=int),
    "EXCEPTION_HANDLER": "apps.core.exceptions.api_exception_handler",
    # The resilient variants fail OPEN (and log) when the Redis-backed counter
    # store is unavailable. The stock DRF classes raised straight out of
    # APIView.initial(), which turned a Redis blip into HTTP 500 on every
    # endpoint — including /api/auth/csrf/ and /api/auth/login/.
    "DEFAULT_THROTTLE_CLASSES": [
        "apps.core.throttles.SafeAnonRateThrottle",
        "apps.core.throttles.SafeUserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": config("THROTTLE_ANON", default="60/min"),
        "user": config("THROTTLE_USER", default="600/min"),
        "login": config("THROTTLE_LOGIN", default="10/min"),
        "messages": config("THROTTLE_MESSAGES", default="120/min"),
        "search": config("THROTTLE_SEARCH", default="30/min"),
        "uploads": config("THROTTLE_UPLOADS", default="60/hour"),
        "push": config("THROTTLE_PUSH", default="30/hour"),
        "credentials": config("THROTTLE_CREDENTIALS", default="10/hour"),
        # Frontend crash reports (apps/core/client_errors.py). Deliberately
        # tight: an anonymous visitor must not be able to flood Render logs.
        "client_errors": config("THROTTLE_CLIENT_ERRORS", default="30/hour"),
    },
    "UNAUTHENTICATED_USER": "django.contrib.auth.models.AnonymousUser",
}

if TESTING:
    # Throttling is exercised explicitly in dedicated tests, not globally.
    REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {
        key: None for key in REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
    }

# ---------------------------------------------------------------------------
# Authentication / tokens / cookies
# ---------------------------------------------------------------------------

ACCESS_TOKEN_MINUTES = config("ACCESS_TOKEN_MINUTES", default=10, cast=int)
REFRESH_TOKEN_DAYS = config("REFRESH_TOKEN_DAYS", default=7, cast=int)

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=ACCESS_TOKEN_MINUTES),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=REFRESH_TOKEN_DAYS),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": SECRET_KEY,
}

ACCESS_COOKIE = config("ACCESS_COOKIE", default="nexora_access")
REFRESH_COOKIE = config("REFRESH_COOKIE", default="nexora_refresh")
REFRESH_COOKIE_PATH = "/api/auth/"
#: "Lax" for local/same-site development; "None" is required when the
#: production frontend is on another site (and then Secure must be on).
COOKIE_SAMESITE = config("COOKIE_SAMESITE", default="Lax" if DEBUG else "None")
COOKIE_SECURE = config("COOKIE_SECURE", default=not DEBUG, cast=boolean)
COOKIE_DOMAIN = config("COOKIE_DOMAIN", default="") or None

if COOKIE_SAMESITE.lower() == "none" and not COOKIE_SECURE and not DEBUG:
    raise ImproperlyConfigured("COOKIE_SAMESITE=None requires COOKIE_SECURE=True.")

LOGIN_FAILURE_LIMIT = config("LOGIN_FAILURE_LIMIT", default=5, cast=int)
LOGIN_LOCKOUT_MINUTES = config("LOGIN_LOCKOUT_MINUTES", default=15, cast=int)

CSRF_COOKIE_NAME = config("CSRF_COOKIE_NAME", default="csrftoken")
# Readable for the same-origin fallback; cross-origin clients use /auth/csrf/ JSON.
CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = COOKIE_SAMESITE
CSRF_COOKIE_SECURE = COOKIE_SECURE
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = COOKIE_SAMESITE
SESSION_COOKIE_SECURE = COOKIE_SECURE

# --------------------------------------------------------------------------- 
# Redis: channel layer, cache, presence, durable workers
# ---------------------------------------------------------------------------

REDIS_URL = config("REDIS_URL", default="")
#: Managed Redis providers that terminate TLS themselves (e.g. Redis Cloud on
#: Render) present a certificate uvicorn's default verification rejects; the
#: deployment must explicitly opt out, never silently.
REDIS_SSL_CERT_REQS = config("REDIS_SSL_CERT_REQS", default="")
REDIS_SOCKET_CONNECT_TIMEOUT = config("REDIS_SOCKET_CONNECT_TIMEOUT", default=5.0, cast=float)
REDIS_SOCKET_TIMEOUT = config("REDIS_SOCKET_TIMEOUT", default=5.0, cast=float)
#: Channels' RedisChannelLayer issues blocking ``BZPOPMIN`` reads with a
#: 5-second server-side block (`brpop_timeout = 5`). The client socket read
#: timeout MUST exceed ``brpop_timeout`` with headroom so an idle wait or brief
#: cloud network jitter never raises ``redis.exceptions.TimeoutError``.
REDIS_CHANNEL_SOCKET_TIMEOUT = config("REDIS_CHANNEL_SOCKET_TIMEOUT", default=15.0, cast=float)
REDIS_RETRY_ON_TIMEOUT = config("REDIS_RETRY_ON_TIMEOUT", default=True, cast=boolean)
REDIS_HEALTH_CHECK_INTERVAL = config("REDIS_HEALTH_CHECK_INTERVAL", default=30, cast=int)
REDIS_CHANNEL_CAPACITY = config("REDIS_CHANNEL_CAPACITY", default=1500, cast=int)
REDIS_CHANNEL_EXPIRY = config("REDIS_CHANNEL_EXPIRY", default=20, cast=int)
REDIS_CHANNEL_GROUP_EXPIRY = config("REDIS_CHANNEL_GROUP_EXPIRY", default=86400, cast=int)


def _redis_host(value: str):
    """Validate and normalise REDIS_URL before boot.

    A malformed URL ("localhost:6379", missing scheme) must fail loudly here
    rather than surface as an inscrutable channel-layer error on first use.
    """
    from urllib.parse import urlparse

    raw = str(value or "").strip()
    if not raw:
        return raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("redis", "rediss", "unix") or not (parsed.hostname or "").strip():
        raise ImproperlyConfigured(
            "REDIS_URL must be a valid redis:// , rediss:// or unix:// URL "
            "(for example redis://host:6379/0)."
        )
    return raw


REDIS_URL = _redis_host(REDIS_URL)


def _redis_channel_hosts(url: str):
    """Channel-layer host entries, with an opt-in TLS-verification override."""
    if not url:
        return [url]
    if url.startswith("rediss://") and REDIS_SSL_CERT_REQS.strip().lower() == "none":
        return [{"address": url, "ssl_cert_reqs": None}]
    return [url]


def _redis_cache_options(url: str) -> dict:
    """Connection-pool options for Django's RedisCache."""
    opts: dict = {
        "socket_connect_timeout": REDIS_SOCKET_CONNECT_TIMEOUT,
        "socket_timeout": REDIS_SOCKET_TIMEOUT,
        "retry_on_timeout": REDIS_RETRY_ON_TIMEOUT,
        "health_check_interval": REDIS_HEALTH_CHECK_INTERVAL,
        "socket_keepalive": True,
    }
    if url.startswith("rediss://") and REDIS_SSL_CERT_REQS.strip().lower() == "none":
        opts["ssl_cert_reqs"] = None
    return opts


def _install_resilient_redis_channel_layer() -> None:
    """Ensure ``channels_redis.core.RedisChannelLayer`` uses resilient sockets.

    1. Injects ``socket_connect_timeout``, ``socket_timeout`` (always greater
       than ``brpop_timeout``), ``retry_on_timeout``, ``health_check_interval``
       and ``socket_keepalive`` into every connection pool created by
       ``RedisChannelLayer.create_pool`` while keeping ``CONFIG['hosts']`` in
       its canonical format.
    2. Wraps ``RedisChannelLayer._brpop_with_clean`` so a transient
       ``redis.exceptions.TimeoutError`` / ``ConnectionError`` during blocking
       ``BZPOPMIN`` logs a throttled structured warning and backs off briefly
       instead of crashing ``await_many_dispatch`` and terminating active
       WebSocket consumers.
    """
    import asyncio
    import logging
    import time

    import redis.asyncio as aioredis
    import redis.exceptions
    from channels_redis.core import RedisChannelLayer
    from django.conf import settings as django_settings

    if getattr(RedisChannelLayer, "_nexora_resilient_patched", False):
        return

    orig_create_pool = RedisChannelLayer.create_pool
    orig_brpop_with_clean = RedisChannelLayer._brpop_with_clean
    rt_log = logging.getLogger("nexora.realtime")
    last_warn_at = 0.0

    def create_pool(self, index):
        host = dict(self.hosts[index])
        brpop_timeout = float(getattr(self, "brpop_timeout", 5) or 5)
        connect_timeout = float(getattr(django_settings, "REDIS_SOCKET_CONNECT_TIMEOUT", 5.0))
        configured_timeout = float(getattr(django_settings, "REDIS_CHANNEL_SOCKET_TIMEOUT", 15.0))
        read_timeout = max(configured_timeout, brpop_timeout + 10.0)
        host.setdefault("socket_connect_timeout", connect_timeout)
        host.setdefault("socket_timeout", read_timeout)
        host.setdefault("retry_on_timeout", bool(getattr(django_settings, "REDIS_RETRY_ON_TIMEOUT", True)))
        host.setdefault("health_check_interval", int(getattr(django_settings, "REDIS_HEALTH_CHECK_INTERVAL", 30)))
        host.setdefault("socket_keepalive", True)
        return aioredis.ConnectionPool.from_url(host.pop("address"), **host)

    async def _brpop_with_clean(self, index, channel, timeout):
        nonlocal last_warn_at
        try:
            return await orig_brpop_with_clean(self, index, channel, timeout)
        except asyncio.CancelledError:
            raise
        except (
            redis.exceptions.TimeoutError,
            redis.exceptions.ConnectionError,
            TimeoutError,
            OSError,
        ) as exc:
            now = time.monotonic()
            if now - last_warn_at >= 30.0:
                last_warn_at = now
                rt_log.warning(
                    "Redis channel layer receive degraded (%s); backing off without dropping consumer",
                    exc.__class__.__name__,
                    extra={
                        "event": "ws.channel_layer_degraded",
                        "operation": "receive_single",
                        "channel": str(channel)[:48],
                        "error_type": exc.__class__.__name__,
                    },
                )
            await asyncio.sleep(0.5)
            return None

    RedisChannelLayer._orig_create_pool = orig_create_pool
    RedisChannelLayer._orig_brpop_with_clean = orig_brpop_with_clean
    RedisChannelLayer.create_pool = create_pool
    RedisChannelLayer._brpop_with_clean = _brpop_with_clean
    RedisChannelLayer._nexora_resilient_patched = True


_install_resilient_redis_channel_layer()


if REDIS_URL and not TESTING:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {
                "hosts": _redis_channel_hosts(REDIS_URL),
                "capacity": REDIS_CHANNEL_CAPACITY,
                "expiry": REDIS_CHANNEL_EXPIRY,
                "group_expiry": REDIS_CHANNEL_GROUP_EXPIRY,
            },
        }
    }
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "KEY_PREFIX": config("CACHE_KEY_PREFIX", default="nexora"),
            "OPTIONS": _redis_cache_options(REDIS_URL),
        }
    }
else:
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "nexora-local",
        }
    }

PRESENCE_TTL_SECONDS = config("PRESENCE_TTL_SECONDS", default=120, cast=int)

# ---------------------------------------------------------------------------
# Object storage (private) — S3-compatible
# ---------------------------------------------------------------------------

STORAGE_BUCKET = config("STORAGE_BUCKET", default="")
STORAGE_ENDPOINT = config("STORAGE_ENDPOINT", default="") or None
STORAGE_REGION = config("STORAGE_REGION", default="") or None
STORAGE_ACCESS_KEY = config("STORAGE_ACCESS_KEY", default="")
STORAGE_SECRET_KEY = config("STORAGE_SECRET_KEY", default="")
#: Lifetime of issued signed URLs. Authorization is always checked *before* a
#: URL is issued; the signature only carries an already-granted permission.
SIGNED_URL_TTL_SECONDS = config("SIGNED_URL_TTL_SECONDS", default=300, cast=int)

if STORAGE_BUCKET:
    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": {
                "bucket_name": STORAGE_BUCKET,
                "endpoint_url": STORAGE_ENDPOINT,
                "region_name": STORAGE_REGION,
                "access_key": STORAGE_ACCESS_KEY,
                "secret_key": STORAGE_SECRET_KEY,
                "default_acl": "private",
                "querystring_auth": True,
                "querystring_expire": SIGNED_URL_TTL_SECONDS,
                "file_overwrite": False,
                "signature_version": "s3v4",
            },
        },
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
else:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }

# ---------------------------------------------------------------------------
# Media pipeline (hard ceilings; per-organization limits live in the database)
# ---------------------------------------------------------------------------

FILE_UPLOAD_MAX_MEMORY_SIZE = config("FILE_UPLOAD_MAX_MEMORY_SIZE", default=2_621_440, cast=int)
DATA_UPLOAD_MAX_MEMORY_SIZE = config("DATA_UPLOAD_MAX_MEMORY_SIZE", default=10_485_760, cast=int)
DATA_UPLOAD_MAX_NUMBER_FIELDS = config("DATA_UPLOAD_MAX_NUMBER_FIELDS", default=200, cast=int)
#: Absolute ceiling enforced before the database limit is even consulted.
MAX_UPLOAD_SIZE = config("MAX_UPLOAD_SIZE", default=2 * 1024**3, cast=int)
MAX_IMAGE_PIXELS = config("MAX_IMAGE_PIXELS", default=50_000_000, cast=int)
MEDIA_THUMBNAIL_SIZE = config("MEDIA_THUMBNAIL_SIZE", default=480, cast=int)
MEDIA_OPTIMIZED_SIZE = config("MEDIA_OPTIMIZED_SIZE", default=1600, cast=int)
MEDIA_WEBP_ENABLED = config("MEDIA_WEBP_ENABLED", default=True, cast=boolean)
MEDIA_PROCESS_INLINE = config("MEDIA_PROCESS_INLINE", default=False, cast=boolean)
MEDIA_STREAM_CHUNK_SIZE = config("MEDIA_STREAM_CHUNK_SIZE", default=262_144, cast=int)

FFMPEG_BINARY = config("FFMPEG_BINARY", default="ffmpeg")
FFPROBE_BINARY = config("FFPROBE_BINARY", default="ffprobe")

MESSAGE_MAX_LENGTH = config("MESSAGE_MAX_LENGTH", default=5000, cast=int)

# ---------------------------------------------------------------------------
# Web push (VAPID). The private key never leaves the backend.
# ---------------------------------------------------------------------------

PUSH_PUBLIC_KEY = config("PUSH_PUBLIC_KEY", default="")
PUSH_PRIVATE_KEY = config("PUSH_PRIVATE_KEY", default="")
PUSH_CONTACT = config("PUSH_CONTACT", default="")
PUSH_MAX_ATTEMPTS = config("PUSH_MAX_ATTEMPTS", default=5, cast=int)
#: Burst window used to aggregate "5 new messages from John" instead of five
#: separate notifications.
PUSH_AGGREGATION_WINDOW_SECONDS = config("PUSH_AGGREGATION_WINDOW_SECONDS", default=60, cast=int)

# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SECURE_HSTS_SECONDS = config("SECURE_HSTS_SECONDS", default=0 if DEBUG else 31_536_000, cast=int)
SECURE_HSTS_INCLUDE_SUBDOMAINS = config("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=not DEBUG, cast=boolean)
SECURE_HSTS_PRELOAD = config("SECURE_HSTS_PRELOAD", default=False, cast=boolean)
SECURE_SSL_REDIRECT = config("SECURE_SSL_REDIRECT", default=False, cast=boolean)

#: The API serves JSON and media only — a deny-all CSP is both correct and
#: compatible. Static HTML is deployed separately; its host-level CSP must be
#: reviewed against the page's inline bootstrap scripts and style attributes.
CONTENT_SECURITY_POLICY = config(
    "CONTENT_SECURITY_POLICY",
    default=(
        "default-src 'none'; img-src 'self' data: blob:; media-src 'self' blob:; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
)
PERMISSIONS_POLICY = config(
    "PERMISSIONS_POLICY",
    default="camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()",
)
CROSS_ORIGIN_RESOURCE_POLICY = config("CROSS_ORIGIN_RESOURCE_POLICY", default="cross-origin")

# ---------------------------------------------------------------------------
# Logging — centralized production observability.
#
# Everything goes to stdout/stderr, unbuffered, because that is the ONLY
# transport Render ingests (Dashboard → nexora-backend → Logs). No file
# handlers: a file inside an ephemeral Render instance is unreadable and is
# lost on every deploy.
#
# Every record carries the request correlation id, so a failed browser request
# (which receives the same value in the X-Request-ID response header, and
# inside the JSON error body as "request_id") can be found by pasting that id
# into the Render log search.
#
# Secrets are never logged: apps/core/observability.scrub() redacts
# PIN/password/token/cookie/authorization/credential-shaped keys, and
# sanitize() neutralises log injection in user-controlled values.
# ---------------------------------------------------------------------------

# Level for our own `nexora.*` loggers. Overridable per environment via the
# LOG_LEVEL env var on Render (e.g. temporarily set DEBUG while diagnosing an
# incident, then put it back to INFO).
LOG_LEVEL = config("LOG_LEVEL", default="INFO").upper()
# Third-party libraries stay at INFO even when DEBUG=True, otherwise the
# connection pool and S3 client drown out the records we actually need.
ROOT_LOG_LEVEL = config("ROOT_LOG_LEVEL", default="INFO").upper()
#: Requests slower than this are reported at WARNING by RequestLogMiddleware.
SLOW_REQUEST_MS = config("SLOW_REQUEST_MS", default=2000, cast=int)
#: Database queries slower than this are reported by the slow-query logger.
SLOW_QUERY_MS = config("SLOW_QUERY_MS", default=500, cast=int)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_context": {"()": "apps.core.observability.RequestContextFilter"},
        # Keeps stdout and stderr disjoint so nothing is logged twice.
        "below_error": {"()": "apps.core.observability.BelowErrorFilter"},
    },
    "formatters": {
        "production": {
            "()": "apps.core.observability.ProductionFormatter",
            "format": (
                "%(asctime)s %(levelname)-8s %(name)s "
                "[req=%(request_id)s user=%(user_id)s role=%(user_role)s op=%(operation)s] %(message)s"
            ),
        },
        "simple": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        # stdout for ordinary records…
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "production",
            "filters": ["request_context", "below_error"],
            "level": "DEBUG",
        },
        # …stderr for ERROR and above, so Render highlights them.
        "console_error": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
            "formatter": "production",
            "filters": ["request_context"],
            "level": "ERROR",
        },
    },
    "root": {"handlers": ["console", "console_error"], "level": ROOT_LOG_LEVEL},
    "loggers": {
        # ---- application ------------------------------------------------
        # One tree: nexora.api, nexora.auth, nexora.request, nexora.exception,
        # nexora.cache, nexora.throttle, nexora.media, nexora.upload,
        # nexora.realtime, nexora.client, nexora.db, nexora.health.
        "nexora": {"handlers": ["console", "console_error"], "level": LOG_LEVEL, "propagate": False},
        # ---- Django -------------------------------------------------------
        # django.request still logs 4xx/5xx; our handlers mark exceptions as
        # already-reported so the traceback is not duplicated.
        "django.request": {"handlers": ["console", "console_error"], "level": "ERROR", "propagate": False},
        "django.server": {"handlers": ["console", "console_error"], "level": "WARNING", "propagate": False},
        "django.security": {"handlers": ["console", "console_error"], "level": "WARNING", "propagate": False},
        # NEVER DEBUG in production: django.db.backends at DEBUG logs every SQL
        # statement *with its parameters*, which would leak message contents
        # and hashed credentials into Render. Errors only.
        "django.db.backends": {"handlers": ["console", "console_error"], "level": "ERROR", "propagate": False},
        # ---- infrastructure ------------------------------------------------
        "channels": {"handlers": ["console", "console_error"], "level": "WARNING", "propagate": False},
        "daphne": {"handlers": ["console", "console_error"], "level": "WARNING", "propagate": False},
        "redis": {"handlers": ["console", "console_error"], "level": "WARNING", "propagate": False},
        "botocore": {"handlers": ["console", "console_error"], "level": "WARNING", "propagate": False},
        "boto3": {"handlers": ["console", "console_error"], "level": "WARNING", "propagate": False},
        "s3transfer": {"handlers": ["console", "console_error"], "level": "WARNING", "propagate": False},
        "urllib3": {"handlers": ["console", "console_error"], "level": "WARNING", "propagate": False},
        "PIL": {"handlers": ["console", "console_error"], "level": "WARNING", "propagate": False},
        # Gunicorn writes its own access/error log to stdout/stderr (see
        # gunicorn.conf.py); keep its records flowing through our formatter.
        "gunicorn.error": {"handlers": ["console", "console_error"], "level": "INFO", "propagate": False},
        "gunicorn.access": {"handlers": ["console", "console_error"], "level": "WARNING", "propagate": False},
        "uvicorn.error": {"handlers": ["console", "console_error"], "level": "INFO", "propagate": False},
        "uvicorn.access": {"handlers": ["console", "console_error"], "level": "WARNING", "propagate": False},
    },
}

# ---------------------------------------------------------------------------
# Production guard rails
# ---------------------------------------------------------------------------

if not DEBUG and not TESTING:
    if SECRET_KEY.startswith("insecure-development-key") or len(SECRET_KEY) < 32:
        raise ImproperlyConfigured("A unique SECRET_KEY of at least 32 characters is required.")
    if not ALLOWED_HOSTS:
        raise ImproperlyConfigured("ALLOWED_HOSTS must be configured in production.")
    if not REDIS_URL:
        raise ImproperlyConfigured("REDIS_URL is mandatory in production (channels, cache, presence).")
    if not STORAGE_BUCKET:
        raise ImproperlyConfigured("Private object storage (STORAGE_BUCKET) is mandatory in production.")
    if not CORS_ALLOWED_ORIGINS and not CSRF_TRUSTED_ORIGINS:
        raise ImproperlyConfigured(
            "Configure CORS_ALLOWED_ORIGINS/CSRF_TRUSTED_ORIGINS for the frontend origin."
        )