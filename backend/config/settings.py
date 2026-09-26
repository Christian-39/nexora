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

import logging
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

#: Render injects the service's public hostname. Adding it automatically means a
#: deployment can never 400 itself on its own URL because ALLOWED_HOSTS was
#: typed with a scheme, a trailing slash or a stale hostname.
RENDER_EXTERNAL_HOSTNAME = config("RENDER_EXTERNAL_HOSTNAME", default="").strip()
if RENDER_EXTERNAL_HOSTNAME and RENDER_EXTERNAL_HOSTNAME not in ALLOWED_HOSTS:
    ALLOWED_HOSTS = [*ALLOWED_HOSTS, RENDER_EXTERNAL_HOSTNAME]

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
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.core.middleware.RequestIDMiddleware",
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
# Database
# ---------------------------------------------------------------------------
_DEFAULT_DB = {
    "ENGINE": config("DATABASE_ENGINE", default="django.db.backends.sqlite3"),
    "NAME": config("DATABASE_NAME", default=BASE_DIR / "db.sqlite3"),
    "USER": config("DATABASE_USER", default=""),
    "PASSWORD": config("DATABASE_PASSWORD", default=""),
    "HOST": config("DATABASE_HOST", default=""),
    "PORT": config("DATABASE_PORT", default=""),
    "CONN_MAX_AGE": 60,
    "CONN_HEALTH_CHECKS": True,
}

# STRICT_TRANS_TABLES is MySQL-only; SQLite rejects the SET statement.
if "mysql" in _DEFAULT_DB["ENGINE"]:
    _DEFAULT_DB["OPTIONS"] = {"init_command": "SET sql_mode='STRICT_TRANS_TABLES'"}

DATABASES = {"default": _DEFAULT_DB}

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

def _clean_origins(values) -> list[str]:
    """Normalise configured origins to scheme://host[:port] with no trailing slash.

    Operators routinely paste ``https://app.example.org/`` or a bare hostname
    into a dashboard field; Django then silently fails CSRF/CORS checks. The
    values are repaired here instead of failing in production.
    """
    cleaned: list[str] = []
    for value in csv_list(values):
        origin = value.strip().rstrip("/")
        if not origin:
            continue
        if "://" not in origin:
            origin = f"https://{origin}"
        if origin not in cleaned:
            cleaned.append(origin)
    return cleaned


CORS_ALLOWED_ORIGINS = _clean_origins(config("CORS_ALLOWED_ORIGINS", default=""))
CSRF_TRUSTED_ORIGINS = _clean_origins(config("CSRF_TRUSTED_ORIGINS", default=""))

# A cross-origin frontend needs BOTH lists. Configuring only one is the single
# most common cause of "login works in curl but not in the browser", so each
# list seeds the other when it was left empty.
if CORS_ALLOWED_ORIGINS and not CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS = list(CORS_ALLOWED_ORIGINS)
if CSRF_TRUSTED_ORIGINS and not CORS_ALLOWED_ORIGINS:
    CORS_ALLOWED_ORIGINS = [o for o in CSRF_TRUSTED_ORIGINS if "*" not in o]

if DEBUG:
    CORS_ALLOWED_ORIGINS = sorted(set(CORS_ALLOWED_ORIGINS) | set(LOCAL_FRONTEND_ORIGINS))
    CSRF_TRUSTED_ORIGINS = sorted(set(CSRF_TRUSTED_ORIGINS) | set(LOCAL_FRONTEND_ORIGINS))

#: Origins allowed to open a WebSocket. Browsers send ``Origin`` on the
#: handshake but the same-origin policy does NOT apply to WebSockets, so with
#: cross-site (SameSite=None) cookies any website could otherwise open an
#: authenticated socket. Enforced in ``config.asgi`` by OriginAllowlist.
WEBSOCKET_ALLOWED_ORIGINS = sorted(set(CORS_ALLOWED_ORIGINS) | set(CSRF_TRUSTED_ORIGINS))

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
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
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
#: "Lax" for same-site deployments; "None" is required when the frontend is
#: served from a different site than the API (and then Secure must be on).
COOKIE_SAMESITE = config("COOKIE_SAMESITE", default="Lax")
COOKIE_SECURE = config("COOKIE_SECURE", default=not DEBUG, cast=boolean)
COOKIE_DOMAIN = config("COOKIE_DOMAIN", default="") or None

if COOKIE_SAMESITE.lower() == "none" and not COOKIE_SECURE and not DEBUG:
    raise ImproperlyConfigured("COOKIE_SAMESITE=None requires COOKIE_SECURE=True.")

LOGIN_FAILURE_LIMIT = config("LOGIN_FAILURE_LIMIT", default=5, cast=int)
LOGIN_LOCKOUT_MINUTES = config("LOGIN_LOCKOUT_MINUTES", default=15, cast=int)

CSRF_COOKIE_NAME = config("CSRF_COOKIE_NAME", default="csrftoken")
CSRF_COOKIE_HTTPONLY = False  # the frontend must read it to echo X-CSRFToken
CSRF_COOKIE_SAMESITE = COOKIE_SAMESITE
CSRF_COOKIE_SECURE = COOKIE_SECURE
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = COOKIE_SAMESITE
SESSION_COOKIE_SECURE = COOKIE_SECURE

# ---------------------------------------------------------------------------
# Redis: channel layer, cache, presence, durable workers
# ---------------------------------------------------------------------------

#: An EXTERNAL managed Redis instance (Render Key Value, Upstash, Redis Cloud,
#: ElastiCache …). There is no bundled/containerised Redis: the URL always
#: points at a service that is operated independently of this web process.
REDIS_URL = config("REDIS_URL", default="").strip()

if REDIS_URL and not REDIS_URL.startswith(("redis://", "rediss://", "unix://")):
    raise ImproperlyConfigured(
        "REDIS_URL must be a redis:// , rediss:// or unix:// URL "
        "(managed providers give you this string; do not include quotes)."
    )

#: Managed providers terminate TLS with certificates that the container trust
#: store cannot always chain. ``rediss://`` + REDIS_SSL_CERT_REQS=none keeps the
#: transport encrypted while tolerating that, without weakening anything else.
REDIS_SSL_CERT_REQS = config("REDIS_SSL_CERT_REQS", default="required").strip().lower()

if REDIS_URL and not TESTING:
    _redis_is_tls = REDIS_URL.startswith("rediss://")
    _channel_host: dict | str = REDIS_URL
    _cache_options: dict = {}
    if _redis_is_tls and REDIS_SSL_CERT_REQS in {"none", "optional"}:
        _channel_host = {"address": REDIS_URL, "ssl_cert_reqs": None}
        _cache_options = {"connection_pool_kwargs": {"ssl_cert_reqs": None}}

    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [_channel_host], "capacity": 1500, "expiry": 20},
        }
    }
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "KEY_PREFIX": config("CACHE_KEY_PREFIX", default="nexora"),
            **({"OPTIONS": _cache_options} if _cache_options else {}),
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
#: compatible. The static frontend ships its own CSP via its host/meta tag.
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
# Logging — never emits credentials, PINs or tokens.
# ---------------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"standard": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "standard"}},
    "root": {"handlers": ["console"], "level": config("LOG_LEVEL", default="INFO")},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "nexora": {"handlers": ["console"], "level": config("LOG_LEVEL", default="INFO"), "propagate": False},
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
        raise ImproperlyConfigured(
            "REDIS_URL is mandatory in production (channels, cache, presence). "
            "Provision an EXTERNAL managed Redis instance and set REDIS_URL to its "
            "connection string (redis:// or rediss://) in the service environment. "
            "The in-memory channel layer is development-only: it cannot carry "
            "realtime events between ASGI processes."
        )
    if DATABASES["default"]["ENGINE"].endswith("sqlite3"):
        raise ImproperlyConfigured("SQLite is not supported in production; configure MySQL 8+.")
    if not STORAGE_BUCKET:
        raise ImproperlyConfigured("Private object storage (STORAGE_BUCKET) is mandatory in production.")
    if not CORS_ALLOWED_ORIGINS and not CSRF_TRUSTED_ORIGINS:
        raise ImproperlyConfigured(
            "Configure CORS_ALLOWED_ORIGINS/CSRF_TRUSTED_ORIGINS for the frontend origin."
        )

    # Cross-site frontend (e.g. frontend on Vercel, API on Render): the browser
    # only sends the HttpOnly session cookies when they are SameSite=None and
    # Secure. This is a configuration mistake the operator must see, but it is
    # not safe to guess the registrable domain here, so it is reported loudly
    # rather than raised.
    _api_hosts = {h.lstrip(".").lower() for h in ALLOWED_HOSTS}
    _frontend_hosts = {
        o.split("://", 1)[-1].split("/")[0].split(":")[0].lower() for o in CORS_ALLOWED_ORIGINS
    }
    if _frontend_hosts - _api_hosts and COOKIE_SAMESITE.lower() != "none":
        logging.getLogger("nexora").warning(
            "The frontend origin is not one of ALLOWED_HOSTS, so authentication is "
            "cross-site: set COOKIE_SAMESITE=None and COOKIE_SECURE=True or the "
            "browser will refuse to send the session cookies."
        )
