"""
NEXORA — centralized environment/infrastructure configuration.

This is the ONLY module in the backend that is allowed to read the process
environment. Everything else must obtain configuration from
``django.conf.settings`` (or from this module's ``config`` object at settings
build time).

Rationale
---------
Infrastructure configuration (secrets, database credentials, Redis, object
storage, VAPID keys, CORS/CSRF origins) is deployment-scoped and must never be
editable from the admin UI or exposed through the API. Organization-scoped
configuration (branding, policies, limits) lives in the database instead — see
``apps.platform_settings``.

Usage
-----
    from config.env import config, csv_list, boolean

    SECRET_KEY = config("SECRET_KEY")
    DEBUG = config("DEBUG", default=False, cast=bool)
    ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="localhost,127.0.0.1", cast=csv_list)
"""

from __future__ import annotations

from pathlib import Path

from decouple import AutoConfig, Csv, UndefinedValueError  # noqa: F401

BASE_DIR = Path(__file__).resolve().parent.parent

#: ``config("NAME", default=..., cast=...)`` — reads ``backend/.env`` first, then
#: the real process environment (which always wins in containerised deploys).
config = AutoConfig(search_path=str(BASE_DIR))


def csv_list(value) -> list[str]:
    """Parse a comma-separated configuration value into a clean list.

    Empty entries and surrounding whitespace are discarded so that
    ``"a, b,,c "`` and ``"a,b,c"`` are equivalent, and an empty string yields
    ``[]`` rather than ``[""]`` (which would silently break ALLOWED_HOSTS).
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = str(value).replace("\n", ",").split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def boolean(value) -> bool:
    """Tolerant boolean cast: accepts true/1/yes/on (any case)."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def optional(name: str, default=None, cast=None):
    """Read a value that may legitimately be absent, without raising."""
    try:
        return config(name, default=default, cast=cast) if cast else config(name, default=default)
    except UndefinedValueError:
        return default


def require(name: str, cast=None):
    """Read a value that must be present; raises ImproperlyConfigured if not."""
    from django.core.exceptions import ImproperlyConfigured

    try:
        return config(name, cast=cast) if cast else config(name)
    except UndefinedValueError as exc:  # pragma: no cover - configuration error path
        raise ImproperlyConfigured(f"Required configuration value {name} is missing.") from exc


__all__ = ["BASE_DIR", "config", "csv_list", "boolean", "optional", "require", "Csv"]
