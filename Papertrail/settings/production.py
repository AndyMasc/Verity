from django.core.exceptions import ImproperlyConfigured
from pythonjsonlogger.json import JsonFormatter

from .base import *  # noqa: F403
from .base import env

# Fail fast: production must never run with DEBUG enabled.
if env.bool("DEBUG", default=False):
    raise ImproperlyConfigured("DEBUG must be disabled in production")

CORS_ALLOW_ALL_ORIGINS = False

# Hosts the app will accept requests for. Required — no default.
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS")

# Origins allowed to submit unsafe (POST) requests, e.g.
# ["https://app.papertrail.example", "https://www.papertrail.example"].
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# Transport security is enabled by base when DEBUG is off; reinforce it here
# so it cannot be disabled by a stray DEBUG override in the environment.
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=True)
CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", default=True)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Override base LOGGING with production JSON formatter
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {
            "()": "core.middleware.RequestIDLogFilter",
        },
    },
    "formatters": {
        "json": {
            "()": JsonFormatter,
            "format": "%(asctime)s %(name)s %(levelname)s %(message)s %(module)s %(process)d %(thread)d %(request_id)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "filters": ["request_id"],
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "django.security": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "documents": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "records": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
