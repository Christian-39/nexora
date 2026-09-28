"""Argon2id tuned to the OWASP Password Storage Cheat Sheet baseline.

OWASP recommendation: Argon2id with m=19456 KiB (19 MiB), t=2, p=1.
This keeps per-login memory bounded (important on small instances running
several workers) while verifying in tens of milliseconds instead of the
hundreds of milliseconds-to-seconds PBKDF2/1M-iterations costs on a small
shared vCPU. Security is NOT reduced: these are current OWASP-recommended
parameters for interactive authentication.
"""

from django.contrib.auth.hashers import Argon2PasswordHasher


class TunedArgon2PasswordHasher(Argon2PasswordHasher):
    time_cost = 2
    memory_cost = 19456  # KiB == 19 MiB
    parallelism = 1
