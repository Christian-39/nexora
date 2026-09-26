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
# Preferred: DATABASE_URL. Alternative: discrete DATABASE_* values (MySQL 8+).
# SQLite is permitted for local development and the test-suite only; it is
# rejected outright when DEBUG is off.

DATABASE_URL = config("DATABASE_URL", default="")
DATABASE_ENGINE = config("DATABASE_ENGINE", default="django.db.backends.mysql")
DATABASE_NAME = config("DATABASE_NAME", default=config("DB_NAME", default=""))
DATABASE_USER = config("DATABASE_USER", default=config("DB_USER", default=""))
DATABASE_PASSWORD = config("DATABASE_PASSWORD", default=config("DB_PASSWORD", default=""))
DATABASE_HOST = config("DATABASE_HOST", default=config("DB_HOST", default="127.0.0.1"))
DATABASE_PORT = config("DATABASE_PORT", default=config("DB_PORT", default=3306), cast=int)
DATABASE_CONN_MAX_AGE = config("DATABASE_CONN_MAX_AGE", default=60, cast=int)

if TESTING:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
            "TEST": {"NAME": ":memory:"},
        }
    }
elif DATABASE_URL:
    DATABASES = {"default": dj_database_url.parse(DATABASE_URL, conn_max_age=DATABASE_CONN_MAX_AGE)}
elif DATABASE_NAME:
    DATABASES = {
        "default": {
            "ENGINE": DATABASE_ENGINE,
            "NAME": DATABASE_NAME,
            "USER": DATABASE_USER,
            "PASSWORD": DATABASE_PASSWORD,
            "HOST": DATABASE_HOST,
            "PORT": str(DATABASE_PORT),
            "CONN_MAX_AGE": DATABASE_CONN_MAX_AGE,
            "OPTIONS": {"charset": "utf8mb4"} if "mysql" in DATABASE_ENGINE else {},
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": str(BASE_DIR / "db.sqlite3"),
        }
    }

if "mysql" in DATABASES["default"].get("ENGINE", ""):
    DATABASES["default"].setdefault("OPTIONS", {})
    DATABASES["default"]["OPTIONS"].setdefault("charset", "utf8mb4")
    DATABASES["default"]["OPTIONS"].setdefault(
        "init_command", "SET sql_mode='STRICT_TRANS_TABLES'"
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

CORS_ALLOWED_ORIGINS = config("CORS_ALLOWED_ORIGINS", default="", cast=csv_list)
CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", default="", cast=csv_list)

if DEBUG:
    CORS_ALLOWED_ORIGINS = sorted(set(CORS_ALLOWED_ORIGINS) | set(LOCAL_FRONTEND_ORIGINS))
    CSRF_TRUSTED_ORIGINS = sorted(set(CSRF_TRUSTED_ORIGINS) | set(LOCAL_FRONTEND_ORIGINS))

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
COOKIE_SAMESITE = config("COOKIE_SAMESITE", default="None")
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

REDIS_URL = config("REDIS_URL", default="")

if REDIS_URL and not TESTING:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [REDIS_URL], "capacity": 1500, "expiry": 20},
        }
    }
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "KEY_PREFIX": config("CACHE_KEY_PREFIX", default="nexora"),
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
        raise ImproperlyConfigured("REDIS_URL is mandatory in production (channels, cache, presence).")
    if DATABASES["default"]["ENGINE"].endswith("sqlite3"):
        raise ImproperlyConfigured("SQLite is not supported in production; configure MySQL 8+.")
    if not STORAGE_BUCKET:
        raise ImproperlyConfigured("Private object storage (STORAGE_BUCKET) is mandatory in production.")
    if not CORS_ALLOWED_ORIGINS and not CSRF_TRUSTED_ORIGINS:
        raise ImproperlyConfigured(
            "Configure CORS_ALLOWED_ORIGINS/CSRF_TRUSTED_ORIGINS for the frontend origin."
        )
