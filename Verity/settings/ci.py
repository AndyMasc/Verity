"""CI-specific Django settings for GitHub Actions.

Imports base settings and overrides for test environments with
explicit ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, and a default SQLite
database when DATABASE_URL is not provided.
"""

from .base import *  # noqa: F403

DEBUG = False

# Test client requests are plain HTTP, so disable the HTTPS-only security
# defaults that base.py enables whenever DEBUG is off (otherwise every request
# 301-redirects to https://testserver and cookie-secure settings drop sessions).
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])  # noqa: F405

CSRF_TRUSTED_ORIGINS = env.list(  # noqa: F405
    "CSRF_TRUSTED_ORIGINS", default=["http://localhost:8000", "http://127.0.0.1:8000"]
)

# Use SQLite for CI if no DATABASE_URL is set (already the base default).
# Use faster password hasher for tests.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

# Cachalot's query cache is keyed by DB alias + SQL only, not by the actual
# database name. Under pytest-xdist each worker creates its own test database
# (test_*_gw0, test_*_gw1) but they share the same Redis cache keys, so one
# worker's cached query results (e.g. Django's post_migrate contenttype
# lookup) can be served to another worker mid-migration, causing duplicate
# inserts ("duplicate key value violates unique constraint
# django_content_type_app_label_model_76bd3d3b_uniq"). Tests don't need query
# caching, so disable it.
CACHALOT_ENABLED = False

# Disable rate limiting in CI tests (short-circuits before any cache lookup)
RATELIMIT_ENABLE = False

# Use plain static file storage (no manifest hashing) so template-rendering
# tests don't require a prior "collectstatic" run. ManifestStaticFilesStorage
# (base's non-DEBUG default) raises on uncollected files like css/dist/styles.css.
# Replace the whole entry: base's OPTIONS carry S3-only kwargs (bucket_name, ...)
# that plain StaticFilesStorage rejects at startup.
STORAGES["staticfiles"] = {  # noqa: F405
    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
}
