"""
NEXORA — Django settings.

All environment/infrastructure configuration is read directly through
python-decouple, using the same DATABASE_* configuration pattern as J-ONE HOTEL
& LODGE.

DATABASE CONFIGURATION
----------------------
NEXORA uses MySQL 8+ exclusively.

Required database environment variables:

    DATABASE_ENGINE=django.db.backends.mysql
    DATABASE_NAME=...
    DATABASE_USER=...
    DATABASE_PASSWORD=...
    DATABASE_HOST=...
    DATABASE_PORT=3306

SQLite is intentionally NOT supported.

There is:
- no SQLite fallback
- no SQLite test database
- no db.sqlite3
- no DATABASE_URL requirement
- no automatic database substitution

If the required MySQL configuration is missing or invalid, Django fails
during startup instead of silently connecting to another database.

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

from decouple import Csv, config
from django.core.exceptions import ImproperlyConfigured


# ===========================================================================
# BASE / ENVIRONMENT
# ===========================================================================

BASE_DIR = __file__
BASE_DIR = BASE_DIR.rsplit("/", 2)[0]

# Convert BASE_DIR back to a Path object.
from pathlib import Path

BASE_DIR = Path(BASE_DIR)


# ---------------------------------------------------------------------------
# Environment mode
# ---------------------------------------------------------------------------

DJANGO_ENV = config(
    "DJANGO_ENV",
    default="development",
).strip().lower()

TESTING = DJANGO_ENV == "test"

DEBUG = config(
    "DEBUG",
    default=(DJANGO_ENV != "production"),
    cast=bool,
)

SECRET_KEY = config(
    "SECRET_KEY",
    default="insecure-development-key-do-not-use-in-production-0123456789",
)

ALLOWED_HOSTS = config(
    "ALLOWED_HOSTS",
    default="localhost,127.0.0.1,[::1]",
    cast=Csv(),
)


# ===========================================================================
# APPLICATIONS
# ===========================================================================

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",

    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "channels",

    # NEXORA applications
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


# ===========================================================================
# MIDDLEWARE
# ===========================================================================

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


# ===========================================================================
# CORE DJANGO CONFIGURATION
# ===========================================================================

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
            ],
        },
    },
]


AUTH_USER_MODEL = "accounts.User"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

USE_TZ = True

TIME_ZONE = config(
    "TIME_ZONE",
    default="UTC",
)

LANGUAGE_CODE = config(
    "LANGUAGE_CODE",
    default="en-us",
)


# NEXORA uses six-digit PIN credentials.
AUTH_PASSWORD_VALIDATORS: list[dict] = []


# ===========================================================================
# DATABASE — MYSQL ONLY
# ===========================================================================
#
# This intentionally follows the J-ONE configuration pattern:
#
#     DATABASE_ENGINE
#     DATABASE_NAME
#     DATABASE_USER
#     DATABASE_PASSWORD
#     DATABASE_HOST
#     DATABASE_PORT
#
# SQLite is completely unsupported.
#
# There is intentionally NO:
#
#     DATABASE_URL
#     sqlite3
#     db.sqlite3
#     :memory:
#
# If DB configuration is missing, Django must fail immediately.
# ===========================================================================

DATABASE_ENGINE = config(
    "DATABASE_ENGINE",
    default="django.db.backends.mysql",
).strip()

DATABASE_NAME = config(
    "DATABASE_NAME",
    default="",
).strip()

DATABASE_USER = config(
    "DATABASE_USER",
    default="",
).strip()

DATABASE_PASSWORD = config(
    "DATABASE_PASSWORD",
    default="",
)

DATABASE_HOST = config(
    "DATABASE_HOST",
    default="",
).strip()

DATABASE_PORT = config(
    "DATABASE_PORT",
    default="3306",
).strip()


# ---------------------------------------------------------------------------
# Database validation
# ---------------------------------------------------------------------------

if not DATABASE_ENGINE:
    raise ImproperlyConfigured(
        "DATABASE_ENGINE is required. NEXORA requires MySQL 8+."
    )

if DATABASE_ENGINE != "django.db.backends.mysql":
    raise ImproperlyConfigured(
        "NEXORA requires MySQL 8+. "
        f"Unsupported database engine: {DATABASE_ENGINE!r}"
    )

if not DATABASE_NAME:
    raise ImproperlyConfigured(
        "DATABASE_NAME is required. NEXORA does not support SQLite or "
        "automatic database fallbacks."
    )

if not DATABASE_USER:
    raise ImproperlyConfigured(
        "DATABASE_USER is required for the NEXORA MySQL database."
    )

if not DATABASE_HOST:
    raise ImproperlyConfigured(
        "DATABASE_HOST is required for the NEXORA MySQL database."
    )

if not DATABASE_PORT:
    raise ImproperlyConfigured(
        "DATABASE_PORT is required for the NEXORA MySQL database."
    )

try:
    DATABASE_PORT_INT = int(DATABASE_PORT)
except (TypeError, ValueError) as exc:
    raise ImproperlyConfigured(
        f"DATABASE_PORT must be a valid integer. Received: {DATABASE_PORT!r}"
    ) from exc

if not 1 <= DATABASE_PORT_INT <= 65535:
    raise ImproperlyConfigured(
        f"DATABASE_PORT must be between 1 and 65535. Received: {DATABASE_PORT_INT}"
    )


DATABASE_CONN_MAX_AGE = config(
    "DATABASE_CONN_MAX_AGE",
    default=60,
    cast=int,
)


# ---------------------------------------------------------------------------
# Mandatory MySQL database configuration
# ---------------------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": DATABASE_NAME,
        "USER": DATABASE_USER,
        "PASSWORD": DATABASE_PASSWORD,
        "HOST": DATABASE_HOST,
        "PORT": str(DATABASE_PORT_INT),

        "CONN_MAX_AGE": DATABASE_CONN_MAX_AGE,

        "OPTIONS": {
            "charset": "utf8mb4",
            "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
        },
    }
}


# ===========================================================================
# STATIC / LOCAL MEDIA ROOT
# ===========================================================================

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_ROOT = BASE_DIR / "media"
MEDIA_URL = "/media/"


# ===========================================================================
# CORS / CSRF
# ===========================================================================

LOCAL_FRONTEND_ORIGINS = [
    f"{scheme}://{host}:{port}"
    for scheme in ("http",)
    for host in ("127.0.0.1", "localhost")
    for port in (
        3000,
        4173,
        5173,
        5500,
        5501,
        8080,
        8081,
    )
]


CORS_ALLOWED_ORIGINS = config(
    "CORS_ALLOWED_ORIGINS",
    default="",
    cast=Csv(),
)

CSRF_TRUSTED_ORIGINS = config(
    "CSRF_TRUSTED_ORIGINS",
    default="",
    cast=Csv(),
)


if DEBUG:
    CORS_ALLOWED_ORIGINS = sorted(
        set(CORS_ALLOWED_ORIGINS)
        | set(LOCAL_FRONTEND_ORIGINS)
    )

    CSRF_TRUSTED_ORIGINS = sorted(
        set(CSRF_TRUSTED_ORIGINS)
        | set(LOCAL_FRONTEND_ORIGINS)
    )


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

CORS_EXPOSE_HEADERS = [
    "X-Request-ID",
    "Retry-After",
    "Content-Range",
    "Accept-Ranges",
]


# ===========================================================================
# REST FRAMEWORK
# ===========================================================================

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.accounts.authentication.CookieJWTAuthentication",
    ],

    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
        "apps.accounts.permissions.CredentialChangedOrAllowed",
    ],

    "DEFAULT_RENDERER_CLASSES": [
        "apps.core.renderers.EnvelopeJSONRenderer",
    ],

    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.FormParser",
        "rest_framework.parsers.MultiPartParser",
    ],

    "DEFAULT_PAGINATION_CLASS": (
        "apps.core.pagination.StandardCursorPagination"
    ),

    "PAGE_SIZE": config(
        "API_PAGE_SIZE",
        default=30,
        cast=int,
    ),

    "EXCEPTION_HANDLER": (
        "apps.core.exceptions.api_exception_handler"
    ),

    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],

    "DEFAULT_THROTTLE_RATES": {
        "anon": config(
            "THROTTLE_ANON",
            default="60/min",
        ),

        "user": config(
            "THROTTLE_USER",
            default="600/min",
        ),

        "login": config(
            "THROTTLE_LOGIN",
            default="10/min",
        ),

        "messages": config(
            "THROTTLE_MESSAGES",
            default="120/min",
        ),

        "search": config(
            "THROTTLE_SEARCH",
            default="30/min",
        ),

        "uploads": config(
            "THROTTLE_UPLOADS",
            default="60/hour",
        ),

        "push": config(
            "THROTTLE_PUSH",
            default="30/hour",
        ),

        "credentials": config(
            "THROTTLE_CREDENTIALS",
            default="10/hour",
        ),
    },

    "UNAUTHENTICATED_USER": (
        "django.contrib.auth.models.AnonymousUser"
    ),
}


if TESTING:
    REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {
        key: None
        for key in REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
    }


# ===========================================================================
# AUTHENTICATION / TOKENS / COOKIES
# ===========================================================================

ACCESS_TOKEN_MINUTES = config(
    "ACCESS_TOKEN_MINUTES",
    default=10,
    cast=int,
)

REFRESH_TOKEN_DAYS = config(
    "REFRESH_TOKEN_DAYS",
    default=7,
    cast=int,
)


SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(
        minutes=ACCESS_TOKEN_MINUTES
    ),

    "REFRESH_TOKEN_LIFETIME": timedelta(
        days=REFRESH_TOKEN_DAYS
    ),

    "ROTATE_REFRESH_TOKENS": True,

    "BLACKLIST_AFTER_ROTATION": True,

    "UPDATE_LAST_LOGIN": True,

    "ALGORITHM": "HS256",

    "SIGNING_KEY": SECRET_KEY,
}


ACCESS_COOKIE = config(
    "ACCESS_COOKIE",
    default="nexora_access",
)

REFRESH_COOKIE = config(
    "REFRESH_COOKIE",
    default="nexora_refresh",
)

REFRESH_COOKIE_PATH = "/api/auth/"


COOKIE_SAMESITE = config(
    "COOKIE_SAMESITE",
    default="Lax",
)

COOKIE_SECURE = config(
    "COOKIE_SECURE",
    default=not DEBUG,
    cast=bool,
)

COOKIE_DOMAIN = config(
    "COOKIE_DOMAIN",
    default="",
) or None


if (
    COOKIE_SAMESITE.lower() == "none"
    and not COOKIE_SECURE
    and not DEBUG
):
    raise ImproperlyConfigured(
        "COOKIE_SAMESITE=None requires COOKIE_SECURE=True."
    )


LOGIN_FAILURE_LIMIT = config(
    "LOGIN_FAILURE_LIMIT",
    default=5,
    cast=int,
)

LOGIN_LOCKOUT_MINUTES = config(
    "LOGIN_LOCKOUT_MINUTES",
    default=15,
    cast=int,
)


CSRF_COOKIE_NAME = config(
    "CSRF_COOKIE_NAME",
    default="csrftoken",
)

CSRF_COOKIE_HTTPONLY = False

CSRF_COOKIE_SAMESITE = COOKIE_SAMESITE

CSRF_COOKIE_SECURE = COOKIE_SECURE

SESSION_COOKIE_HTTPONLY = True

SESSION_COOKIE_SAMESITE = COOKIE_SAMESITE

SESSION_COOKIE_SECURE = COOKIE_SECURE


# ===========================================================================
# REDIS
# ===========================================================================

REDIS_URL = config(
    "REDIS_URL",
    default="",
).strip()


if REDIS_URL and not TESTING:

    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",

            "CONFIG": {
                "hosts": [REDIS_URL],
                "capacity": 1500,
                "expiry": 20,
            },
        }
    }


    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",

            "LOCATION": REDIS_URL,

            "KEY_PREFIX": config(
                "CACHE_KEY_PREFIX",
                default="nexora",
            ),
        }
    }

else:

    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels.layers.InMemoryChannelLayer",
        }
    }

    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",

            "LOCATION": "nexora-local",
        }
    }


PRESENCE_TTL_SECONDS = config(
    "PRESENCE_TTL_SECONDS",
    default=120,
    cast=int,
)


# ===========================================================================
# OBJECT STORAGE — PRIVATE S3-COMPATIBLE STORAGE
# ===========================================================================

STORAGE_BUCKET = config(
    "STORAGE_BUCKET",
    default="",
)

STORAGE_ENDPOINT = config(
    "STORAGE_ENDPOINT",
    default="",
) or None

STORAGE_REGION = config(
    "STORAGE_REGION",
    default="",
) or None

STORAGE_ACCESS_KEY = config(
    "STORAGE_ACCESS_KEY",
    default="",
)

STORAGE_SECRET_KEY = config(
    "STORAGE_SECRET_KEY",
    default="",
)


SIGNED_URL_TTL_SECONDS = config(
    "SIGNED_URL_TTL_SECONDS",
    default=300,
    cast=int,
)


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

        "staticfiles": {
            "BACKEND": (
                "django.contrib.staticfiles.storage.StaticFilesStorage"
            ),
        },
    }

else:

    STORAGES = {
        "default": {
            "BACKEND": (
                "django.core.files.storage.FileSystemStorage"
            ),
        },

        "staticfiles": {
            "BACKEND": (
                "django.contrib.staticfiles.storage.StaticFilesStorage"
            ),
        },
    }


# ===========================================================================
# MEDIA PIPELINE
# ===========================================================================

FILE_UPLOAD_MAX_MEMORY_SIZE = config(
    "FILE_UPLOAD_MAX_MEMORY_SIZE",
    default=2_621_440,
    cast=int,
)

DATA_UPLOAD_MAX_MEMORY_SIZE = config(
    "DATA_UPLOAD_MAX_MEMORY_SIZE",
    default=10_485_760,
    cast=int,
)

DATA_UPLOAD_MAX_NUMBER_FIELDS = config(
    "DATA_UPLOAD_MAX_NUMBER_FIELDS",
    default=200,
    cast=int,
)


MAX_UPLOAD_SIZE = config(
    "MAX_UPLOAD_SIZE",
    default=2 * 1024**3,
    cast=int,
)

MAX_IMAGE_PIXELS = config(
    "MAX_IMAGE_PIXELS",
    default=50_000_000,
    cast=int,
)

MEDIA_THUMBNAIL_SIZE = config(
    "MEDIA_THUMBNAIL_SIZE",
    default=480,
    cast=int,
)

MEDIA_OPTIMIZED_SIZE = config(
    "MEDIA_OPTIMIZED_SIZE",
    default=1600,
    cast=int,
)

MEDIA_WEBP_ENABLED = config(
    "MEDIA_WEBP_ENABLED",
    default=True,
    cast=bool,
)

MEDIA_PROCESS_INLINE = config(
    "MEDIA_PROCESS_INLINE",
    default=False,
    cast=bool,
)

MEDIA_STREAM_CHUNK_SIZE = config(
    "MEDIA_STREAM_CHUNK_SIZE",
    default=262_144,
    cast=int,
)


FFMPEG_BINARY = config(
    "FFMPEG_BINARY",
    default="ffmpeg",
)

FFPROBE_BINARY = config(
    "FFPROBE_BINARY",
    default="ffprobe",
)


MESSAGE_MAX_LENGTH = config(
    "MESSAGE_MAX_LENGTH",
    default=5000,
    cast=int,
)


# ===========================================================================
# WEB PUSH / VAPID
# ===========================================================================

PUSH_PUBLIC_KEY = config(
    "PUSH_PUBLIC_KEY",
    default="",
)

PUSH_PRIVATE_KEY = config(
    "PUSH_PRIVATE_KEY",
    default="",
)

PUSH_CONTACT = config(
    "PUSH_CONTACT",
    default="",
)

PUSH_MAX_ATTEMPTS = config(
    "PUSH_MAX_ATTEMPTS",
    default=5,
    cast=int,
)


PUSH_AGGREGATION_WINDOW_SECONDS = config(
    "PUSH_AGGREGATION_WINDOW_SECONDS",
    default=60,
    cast=int,
)


# ===========================================================================
# SECURITY HEADERS
# ===========================================================================

SECURE_PROXY_SSL_HEADER = (
    "HTTP_X_FORWARDED_PROTO",
    "https",
)

SECURE_CONTENT_TYPE_NOSNIFF = True

SECURE_REFERRER_POLICY = "same-origin"

X_FRAME_OPTIONS = "DENY"


SECURE_HSTS_SECONDS = config(
    "SECURE_HSTS_SECONDS",
    default=0 if DEBUG else 31_536_000,
    cast=int,
)

SECURE_HSTS_INCLUDE_SUBDOMAINS = config(
    "SECURE_HSTS_INCLUDE_SUBDOMAINS",
    default=not DEBUG,
    cast=bool,
)

SECURE_HSTS_PRELOAD = config(
    "SECURE_HSTS_PRELOAD",
    default=False,
    cast=bool,
)

SECURE_SSL_REDIRECT = config(
    "SECURE_SSL_REDIRECT",
    default=False,
    cast=bool,
)


CONTENT_SECURITY_POLICY = config(
    "CONTENT_SECURITY_POLICY",
    default=(
        "default-src 'none'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'self'"
    ),
)


PERMISSIONS_POLICY = config(
    "PERMISSIONS_POLICY",
    default=(
        "camera=(), "
        "microphone=(), "
        "geolocation=(), "
        "payment=(), "
        "usb=(), "
        "interest-cohort=()"
    ),
)


CROSS_ORIGIN_RESOURCE_POLICY = config(
    "CROSS_ORIGIN_RESOURCE_POLICY",
    default="cross-origin",
)


# ===========================================================================
# LOGGING
# ===========================================================================

LOG_LEVEL = config(
    "LOG_LEVEL",
    default="INFO",
)


LOGGING = {
    "version": 1,

    "disable_existing_loggers": False,

    "formatters": {
        "standard": {
            "format": (
                "%(asctime)s "
                "%(levelname)s "
                "%(name)s "
                "%(message)s"
            ),
        },
    },

    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
    },

    "root": {
        "handlers": ["console"],
        "level": LOG_LEVEL,
    },

    "loggers": {
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },

        "nexora": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
    },
}


# ===========================================================================
# PRODUCTION GUARD RAILS
# ===========================================================================

if not DEBUG and not TESTING:

    if (
        SECRET_KEY.startswith(
            "insecure-development-key"
        )
        or len(SECRET_KEY) < 32
    ):
        raise ImproperlyConfigured(
            "A unique SECRET_KEY of at least 32 characters "
            "is required in production."
        )


    if not ALLOWED_HOSTS:
        raise ImproperlyConfigured(
            "ALLOWED_HOSTS must be configured in production."
        )


    if not REDIS_URL:
        raise ImproperlyConfigured(
            "REDIS_URL is mandatory in production "
            "(channels, cache, presence)."
        )


    if DATABASES["default"]["ENGINE"] != "django.db.backends.mysql":
        raise ImproperlyConfigured(
            "NEXORA requires MySQL 8+ in production."
        )


    if not STORAGE_BUCKET:
        raise ImproperlyConfigured(
            "Private object storage (STORAGE_BUCKET) "
            "is mandatory in production."
        )


    if not CORS_ALLOWED_ORIGINS and not CSRF_TRUSTED_ORIGINS:
        raise ImproperlyConfigured(
            "Configure CORS_ALLOWED_ORIGINS and/or "
            "CSRF_TRUSTED_ORIGINS for the frontend origin."
        )