"""
Production deployment configuration.

These tests load ``config/settings.py`` in isolation with a controlled
environment, which is the only way to assert on the production guard rails
(the suite itself necessarily runs with DJANGO_ENV=test). They encode the exact
Render + Vercel deployment contract:

    Vercel frontend  ──HTTPS/WSS──►  Render ASGI backend
                                      ├── external MySQL
                                      ├── external Redis  (REDIS_URL)
                                      └── external private object storage
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from unittest import mock

import pytest
from django.core.exceptions import ImproperlyConfigured

SETTINGS_PATH = Path(__file__).resolve().parents[1] / "config" / "settings.py"

#: A complete, valid production environment for this deployment.
PRODUCTION_ENV = {
    "DJANGO_ENV": "production",
    "DEBUG": "False",
    "SECRET_KEY": "a-unique-production-secret-key-of-more-than-32-characters",
    "ALLOWED_HOSTS": "nexora-backend-ptsc.onrender.com",
    "CORS_ALLOWED_ORIGINS": "https://nexora-eight-lilac.vercel.app",
    "CSRF_TRUSTED_ORIGINS": "https://nexora-eight-lilac.vercel.app",
    "COOKIE_SAMESITE": "None",
    "COOKIE_SECURE": "True",
    "REDIS_URL": "redis://red-abc123:6379",
    "DATABASE_URL": "mysql://nexora:secret@db.internal:3306/nexora",
    "STORAGE_BUCKET": "nexora-private",
    "STORAGE_ACCESS_KEY": "key",
    "STORAGE_SECRET_KEY": "secret",
    "RENDER_EXTERNAL_HOSTNAME": "",
}


def load_settings(**overrides):
    """Execute config/settings.py with a specific environment."""
    env = {**PRODUCTION_ENV, **overrides}
    env = {key: value for key, value in env.items() if value is not None}

    spec = importlib.util.spec_from_file_location("config._settings_probe", SETTINGS_PATH)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "config"  # so `from .env import …` resolves

    removed = {key: None for key in overrides if overrides[key] is None}
    with mock.patch.dict(os.environ, env, clear=False):
        for key in removed:
            os.environ.pop(key, None)
        spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------


def test_production_uses_the_external_redis_channel_layer_and_cache():
    settings = load_settings()
    assert settings.CHANNEL_LAYERS["default"]["BACKEND"] == "channels_redis.core.RedisChannelLayer"
    assert settings.CHANNEL_LAYERS["default"]["CONFIG"]["hosts"] == [PRODUCTION_ENV["REDIS_URL"]]
    assert settings.CACHES["default"]["BACKEND"] == "django.core.cache.backends.redis.RedisCache"
    assert settings.CACHES["default"]["LOCATION"] == PRODUCTION_ENV["REDIS_URL"]


def test_production_without_redis_fails_loudly_and_never_falls_back_to_memory():
    with pytest.raises(ImproperlyConfigured) as exc:
        load_settings(REDIS_URL=None)
    message = str(exc.value)
    assert "REDIS_URL" in message
    assert "production" in message
    # The infrastructure problem must be identified without leaking anything.
    assert "secret" not in message.lower()


def test_a_malformed_redis_url_is_rejected_before_boot():
    with pytest.raises(ImproperlyConfigured):
        load_settings(REDIS_URL="localhost:6379")


def test_managed_tls_redis_can_relax_certificate_verification_only_when_asked():
    settings = load_settings(REDIS_URL="rediss://user:pw@managed:6379", REDIS_SSL_CERT_REQS="none")
    host = settings.CHANNEL_LAYERS["default"]["CONFIG"]["hosts"][0]
    assert host["ssl_cert_reqs"] is None

    strict = load_settings(REDIS_URL="rediss://user:pw@managed:6379")
    assert strict.CHANNEL_LAYERS["default"]["CONFIG"]["hosts"] == ["rediss://user:pw@managed:6379"]


def test_development_may_still_use_the_in_memory_channel_layer():
    settings = load_settings(
        DJANGO_ENV="development", DEBUG="True", REDIS_URL=None, STORAGE_BUCKET=None, DATABASE_URL=None
    )
    assert settings.CHANNEL_LAYERS["default"]["BACKEND"] == "channels.layers.InMemoryChannelLayer"


# ---------------------------------------------------------------------------
# Cross-origin: CORS, CSRF, cookies
# ---------------------------------------------------------------------------


def test_cors_is_restricted_to_the_configured_frontend_origin():
    settings = load_settings()
    assert settings.CORS_ALLOWED_ORIGINS == ["https://nexora-eight-lilac.vercel.app"]
    assert settings.CORS_ALLOW_ALL_ORIGINS is False
    assert settings.CORS_ALLOW_CREDENTIALS is True


def test_csrf_trusted_origins_include_the_frontend_origin():
    settings = load_settings()
    assert "https://nexora-eight-lilac.vercel.app" in settings.CSRF_TRUSTED_ORIGINS


def test_configuring_only_one_origin_list_seeds_the_other():
    settings = load_settings(CSRF_TRUSTED_ORIGINS=None)
    assert settings.CSRF_TRUSTED_ORIGINS == ["https://nexora-eight-lilac.vercel.app"]

    settings = load_settings(CORS_ALLOWED_ORIGINS=None)
    assert settings.CORS_ALLOWED_ORIGINS == ["https://nexora-eight-lilac.vercel.app"]


def test_sloppy_origin_values_are_normalised():
    settings = load_settings(
        CORS_ALLOWED_ORIGINS="https://nexora-eight-lilac.vercel.app/ , nexora-eight-lilac.vercel.app"
    )
    assert settings.CORS_ALLOWED_ORIGINS == ["https://nexora-eight-lilac.vercel.app"]


def test_cross_site_cookies_are_none_and_secure_for_this_deployment():
    settings = load_settings()
    assert settings.COOKIE_SAMESITE == "None"
    assert settings.COOKIE_SECURE is True
    assert settings.SESSION_COOKIE_SAMESITE == "None"
    assert settings.SESSION_COOKIE_SECURE is True
    assert settings.CSRF_COOKIE_SAMESITE == "None"
    assert settings.CSRF_COOKIE_SECURE is True
    # CSRF stays enabled. The cookie supports same-origin fallback while the
    # cross-origin frontend consumes the endpoint's JSON token.
    assert settings.CSRF_COOKIE_HTTPONLY is False
    assert "django.middleware.csrf.CsrfViewMiddleware" in settings.MIDDLEWARE
    assert settings.SESSION_COOKIE_HTTPONLY is True


def test_local_cookie_defaults_remain_usable_over_http():
    settings = load_settings(
        DJANGO_ENV="development",
        DEBUG="True",
        COOKIE_SAMESITE=None,
        COOKIE_SECURE=None,
        REDIS_URL=None,
        STORAGE_BUCKET=None,
        DATABASE_URL=None,
    )
    assert settings.COOKIE_SAMESITE == "Lax"
    assert settings.COOKIE_SECURE is False
    assert settings.CSRF_COOKIE_SAMESITE == "Lax"
    assert settings.CSRF_COOKIE_SECURE is False


def test_samesite_none_without_secure_is_refused():
    with pytest.raises(ImproperlyConfigured):
        load_settings(COOKIE_SECURE="False")


# ---------------------------------------------------------------------------
# Hosts, database, storage, ASGI
# ---------------------------------------------------------------------------


def test_the_render_hostname_is_always_an_allowed_host():
    settings = load_settings(
        ALLOWED_HOSTS="example.org", RENDER_EXTERNAL_HOSTNAME="nexora-backend-ptsc.onrender.com"
    )
    assert "nexora-backend-ptsc.onrender.com" in settings.ALLOWED_HOSTS


def test_production_requires_mysql_and_private_storage():
    with pytest.raises(ImproperlyConfigured):
        load_settings(DATABASE_URL=None)
    with pytest.raises(ImproperlyConfigured):
        load_settings(STORAGE_BUCKET=None)

    settings = load_settings()
    assert "mysql" in settings.DATABASES["default"]["ENGINE"]
    assert settings.STORAGES["default"]["OPTIONS"]["default_acl"] == "private"
    assert settings.STORAGES["default"]["OPTIONS"]["querystring_auth"] is True


def test_the_asgi_application_is_the_deployed_entrypoint():
    settings = load_settings()
    assert settings.ASGI_APPLICATION == "config.asgi.application"


def test_websocket_origins_are_derived_from_the_configured_frontend():
    settings = load_settings()
    assert settings.WEBSOCKET_ALLOWED_ORIGINS == ["https://nexora-eight-lilac.vercel.app"]


def test_security_headers_and_proxy_ssl_stay_enabled():
    settings = load_settings()
    assert settings.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")
    assert settings.SECURE_CONTENT_TYPE_NOSNIFF is True
    assert settings.SECURE_HSTS_SECONDS > 0
    assert settings.X_FRAME_OPTIONS == "DENY"


# ---------------------------------------------------------------------------
# Deployment artefacts
# ---------------------------------------------------------------------------

REPO = Path(__file__).resolve().parents[2]


def test_the_repository_contains_no_docker_deployment_artefacts():
    offenders = [
        str(path.relative_to(REPO))
        for path in REPO.rglob("*")
        if path.is_file()
        and ".git/" not in str(path)
        and path.name.lower() in {"dockerfile", "docker-compose.yml", "docker-compose.yaml", ".dockerignore"}
    ]
    assert not offenders, f"Docker artefacts must not exist: {offenders}"


def test_the_render_blueprint_deploys_asgi_without_docker():
    blueprint = (REPO / "render.yaml").read_text()
    assert "config.asgi:application" in blueprint
    assert "uvicorn.workers.UvicornWorker" in blueprint
    start_commands = [line for line in blueprint.splitlines() if "startCommand" in line]
    assert start_commands and all("config.wsgi" not in line for line in start_commands)
    assert "runtime: python" in blueprint
    directives = [
        line for line in blueprint.splitlines() if line.strip() and not line.strip().startswith("#")
    ]
    assert all("docker" not in line.lower() for line in directives)
    # Background processing runs as its own worker services, not in the web dyno.
    for command in ("push_worker", "media_worker", "finalize_uploads"):
        assert command in blueprint


def test_gunicorn_binds_the_platform_port_and_uses_an_asgi_worker():
    source = (REPO / "backend" / "gunicorn.conf.py").read_text()
    assert "PORT" in source
    assert "uvicorn.workers.UvicornWorker" in source
