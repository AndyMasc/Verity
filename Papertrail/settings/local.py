from .base import *  # noqa: F401, F403

INSTALLED_APPS.extend(["django_browser_reload", "debug_toolbar"])

MIDDLEWARE.insert(-1, "django_browser_reload.middleware.BrowserReloadMiddleware")
MIDDLEWARE.insert(-1, "debug_toolbar.middleware.DebugToolbarMiddleware")

CSRF_TRUSTED_ORIGINS = [env("NGROK_HTTPS_TUNNEL_URL", default="http://localhost:8000")]
CORS_ALLOW_ALL_ORIGINS = True
ALLOWED_HOSTS = ["*"]

SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

STORAGES = {
    "default": STORAGES["default"],
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

INTERNAL_IPS = [
    "127.0.0.1",  # localhost
]

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
