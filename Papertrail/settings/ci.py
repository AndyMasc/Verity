"""CI-specific Django settings for GitHub Actions.

Imports base settings and overrides for test environments with
explicit ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, and a default SQLite
database when DATABASE_URL is not provided.
"""

from .base import *  # noqa: F403

DEBUG = False

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])  # noqa: F405

CSRF_TRUSTED_ORIGINS = env.list(  # noqa: F405
    "CSRF_TRUSTED_ORIGINS", default=["http://localhost:8000", "http://127.0.0.1:8000"]
)

# Use SQLite for CI if no DATABASE_URL is set (already the base default).
# Use faster password hasher for tests.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

# Disable rate limiting in CI tests
RATELIMIT_USE_CACHE = None
