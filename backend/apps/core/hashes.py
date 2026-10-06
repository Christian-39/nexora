"""Stable fixed-width digests for database uniqueness on long external keys."""

from __future__ import annotations

import hashlib


def sha256_hex(value: str) -> str:
    """Return a deterministic SHA-256 hex digest without changing the source value."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()
