from urllib.parse import urlparse

from django.core.exceptions import ImproperlyConfigured
from pythonjsonlogger.json import JsonFormatter

from .base import *  # noqa: F403
from .base import env

# production must never run with DEBUG enabled.
if env.bool("DEBUG", default=False):
    raise ImproperlyConfigured("DEBUG must be disabled in production")

CORS_ALLOW_ALL_ORIGINS = False

REDIS_URL = env("REDIS_URL")

# Hosts the app will accept requests for. Required — no default.
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS")

# Origins allowed to submit unsafe (POST) requests, e.g.
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# Public base URL of the site, used to build absolute links in emails
# (reimbursement pay/view links) and webpush "open" URLs.
SITE_URL = env("SITE_URL", default="https://veritypay.app")

# Override TURNSTILE_HOSTNAMES if the app serves
# more than one frontend hostname. Do not include localhost/127.0.0.1 here.
TURNSTILE_HOSTNAMES = env.list(
    "TURNSTILE_HOSTNAMES",
    default=[urlparse(SITE_URL).hostname or "localhost"],
)

# Static files are immutable build artifacts (hashed names via WhiteNoise).
# Cache them for a year so returning visitors never re-fetch them.
WHITENOISE_MAX_AGE = 60 * 60 * 24 * 365

SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=True)
CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", default=True)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

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
