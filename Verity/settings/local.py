from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import database_config, env

_LOCAL_HOSTS = {
    "",
    "localhost",
    "127.0.0.1",
    "db",
    "redis",
    "lavinmq",
    "rabbitmq",
    # Dev database on Neon
    "ep-fragrant-mud-axccmsnm-pooler.c-4.us-east-2.aws.neon.tech",
}

# Guardrail: local/dev tooling (manage.py, pytest, runserver) must never point
# at shared/production infrastructure. Without this, a stray DATABASE_URL or
# RABBIT_MQ_URL in .env makes local commands read/migrate the prod database and
# tests purge live task queues.
for _var, _host in [
    ("DATABASE_URL", database_config.get("HOST", "")),
    ("RABBIT_MQ_URL", env("RABBIT_MQ_URL", default="").split("@")[-1].split(":")[0]),
    ("REDIS_URL", env("REDIS_URL", default="").split("@")[-1].split(":")[0]),
]:
    if _host and _host.lower() not in _LOCAL_HOSTS:
        raise ImproperlyConfigured(
            f"local settings refuse to run: {_var} points at non-local host {_host!r}. "
            "Point it at local docker services (see docker-compose.yml)."
        )

INSTALLED_APPS.extend(["django_browser_reload", "debug_toolbar"])  # noqa: F405

MIDDLEWARE.insert(-1, "django_browser_reload.middleware.BrowserReloadMiddleware")  # noqa: F405
MIDDLEWARE.insert(-1, "debug_toolbar.middleware.DebugToolbarMiddleware")  # noqa: F405

CSRF_TRUSTED_ORIGINS = [env("NGROK_HTTPS_TUNNEL_URL", default="http://localhost:8000")]
CORS_ALLOW_ALL_ORIGINS = True
ALLOWED_HOSTS = ["*"]

SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

STORAGES = {
    "default": STORAGES["default"],  # noqa: F405
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

INTERNAL_IPS = [
    "127.0.0.1",  # localhost
]

# See ci.py: cachalot's Redis cache keys collide across xdist workers' separate
# test databases, racing Django's post_migrate contenttype inserts during test
# database creation.
CACHALOT_ENABLED = False

CONTENT_SECURITY_POLICY = {
    "DIRECTIVES": {
        "default-src": (
            "'self'",
            "'unsafe-inline'",
            "'unsafe-eval'",
            "data:",
            "blob:",
            "https:",
            "http:",
        ),
        "script-src": ("'self'", "'unsafe-inline'", "'unsafe-eval'", "https:", "http:"),
        "style-src": ("'self'", "'unsafe-inline'", "https:", "http:"),
        "img-src": ("'self'", "data:", "blob:", "https:", "http:"),
        "connect-src": ("'self'", "https:", "http:", "ws:", "wss:"),
        "font-src": ("'self'", "https:", "http:", "data:"),
        "frame-ancestors": ("'none'",),
    }
}

SITE_URL = "http://localhost:8000"
