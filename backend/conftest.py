"""Pytest bootstrap: select the in-memory test configuration before Django starts."""

import os

os.environ.setdefault("DJANGO_ENV", "test")
os.environ.setdefault("DEBUG", "True")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-the-nexora-suite-0123456789abcdef")
os.environ.setdefault("MEDIA_PROCESS_INLINE", "True")
