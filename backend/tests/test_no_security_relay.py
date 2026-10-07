"""Regression tests for the Security Relay removal.

The production deployment now communicates directly:

    Vercel frontend  ──HTTPS/WSS──►  Render public ASGI backend

These tests pin that contract: there must be no SecurityRelayBoundary ASGI
wrapper, no relay token setting, no relay header, no relay-specific docstring
on the live code paths, and the settings must boot in production without any
relay-only configuration.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"


# ---------------------------------------------------------------------------
# Settings — no relay variables must be required for production startup
# ---------------------------------------------------------------------------

PRODUCTION_ENV = {
    "DJANGO_ENV": "production",
    "DEBUG": "False",
    "SECRET_KEY": "production-secret-key-of-at-least-32-characters-1234",
    "ALLOWED_HOSTS": "nexora-f397.onrender.com",
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


def _load_settings(**overrides):
    """Load config/settings.py against a controlled environment."""
    from unittest import mock

    env = {**PRODUCTION_ENV, **overrides}
    env = {key: value for key, value in env.items() if value is not None}

    spec = importlib.util.spec_from_file_location(
        "_settings_probe", BACKEND / "config" / "settings.py"
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "config"
    removed = {key: None for key in overrides if overrides[key] is None}
    with mock.patch.dict(os.environ, env, clear=False):
        for key in removed:
            os.environ.pop(key, None)
        spec.loader.exec_module(module)
    return module


def test_production_boots_without_relay_configuration():
    settings = _load_settings()
    assert not hasattr(settings, "SECURITY_RELAY_REQUIRED")
    assert not hasattr(settings, "SECURITY_RELAY_TOKEN")


def test_production_boots_with_relay_envvars_set_just_in_case():
    # A misconfigured Render dashboard might still carry the old env group.
    # The backend must not fail closed, and must not let these names reach
    # application code: they should be silently ignored.
    settings = _load_settings(
        SECURITY_RELAY_REQUIRED="True",
        SECURITY_RELAY_TOKEN="this-token-must-not-cause-startup-to-fail",
    )
    assert not hasattr(settings, "SECURITY_RELAY_REQUIRED")
    assert not hasattr(settings, "SECURITY_RELAY_TOKEN")


def test_proxy_ssl_header_is_trusted_for_render():
    settings = _load_settings()
    assert settings.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")


# ---------------------------------------------------------------------------
# ASGI — no relay boundary wrapping the application
# ---------------------------------------------------------------------------


def test_asgi_application_does_not_wrap_a_relay_boundary():
    """The ASGI module must not import or wire a SecurityRelayBoundary."""
    source = (BACKEND / "config" / "asgi.py").read_text()
    assert "SecurityRelayBoundary" not in source
    assert "apps.security.relay" not in source
    assert "relay" not in source.lower(), "ASGI entry point must not mention the relay"


def test_asgi_application_still_handles_http_and_websocket_directly():
    """Channels, WebSockets, JWT auth, and origin allowlist remain in place."""
    from config.asgi import JWTAuthMiddleware, OriginAllowlist, application

    assert callable(application)
    assert isinstance(JWTAuthMiddleware, type)
    assert isinstance(OriginAllowlist, type)
    # The outermost router must be Channels' ProtocolTypeRouter so that
    # both HTTP and WebSocket scopes still flow through the application.
    assert application.__class__.__name__ == "ProtocolTypeRouter"


# ---------------------------------------------------------------------------
# Repository — no surviving Security Relay code or deployment artefacts
# ---------------------------------------------------------------------------


def _walk(relative: str):
    return (REPO / relative).rglob("*")


def test_relay_module_is_gone():
    assert not (BACKEND / "apps" / "security" / "relay.py").exists(), (
        "apps/security/relay.py must be deleted"
    )


def test_relay_top_level_directory_is_gone():
    assert not (REPO / "relay").exists(), (
        "The standalone relay/ directory must be deleted"
    )


def test_render_blueprint_does_not_deploy_a_relay_service():
    blueprint = (REPO / "render.yaml").read_text()
    assert "nexora-security-relay" not in blueprint
    assert "nexora-security-boundary" not in blueprint
    assert "rootDir: relay" not in blueprint
    assert "UPSTREAM_HOST" not in blueprint
    assert "UPSTREAM_PORT" not in blueprint
    assert "UPSTREAM_SCHEME" not in blueprint
    assert "PUBLIC_SCHEME" not in blueprint
    assert "MAX_BODY_BYTES" not in blueprint


def test_render_blueprint_runs_the_backend_as_a_public_web_service():
    blueprint = (REPO / "render.yaml").read_text()
    assert "type: web" in blueprint
    assert "name: nexora-backend" in blueprint
    assert "config.asgi:application" in blueprint
    assert "uvicorn.workers.UvicornWorker" in blueprint
    assert "healthCheckPath: /health/live/" in blueprint
    # The backend must NOT be a private service anymore.
    assert "type: pserv" not in blueprint


def test_active_code_does_not_reference_relay_settings():
    """Search every Python file under backend/ outside tests/ for relay names."""
    forbidden = (
        "SECURITY_RELAY_REQUIRED",
        "SECURITY_RELAY_TOKEN",
        "RELAY_AUTH_TOKEN",
        "x-nexora-relay-token",
        "SecurityRelayBoundary",
    )
    offenders: list[str] = []
    for path in (BACKEND).rglob("*.py"):
        if "/tests/" in str(path) or "/migrations/" in str(path) or "__pycache__" in str(path):
            continue
        text = path.read_text()
        for needle in forbidden:
            if needle in text:
                offenders.append(f"{path}: {needle}")
    assert not offenders, f"Active code still mentions the relay: {offenders}"


def test_frontend_env_example_does_not_recommend_a_relay_origin():
    env = (REPO / "frontend" / ".env.example").read_text().lower()
    assert "security relay" not in env
    assert "security-relay" not in env
    assert "security_relay" not in env