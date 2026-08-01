from pathlib import Path

import environ
import sentry_sdk

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
env_file = BASE_DIR / ".env"

if env_file.exists():
    env.read_env(str(env_file))

# PostHog
POSTHOG_PROJECT_TOKEN = env("POSTHOG_PROJECT_TOKEN", default="")
POSTHOG_HOST = env("POSTHOG_HOST", default="https://us.i.posthog.com")
POSTHOG_DISABLED = env.bool("POSTHOG_DISABLED", default=False)

# Core
SECRET_KEY = env("SECRET_KEY")
DEBUG = env.bool("DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS")
ROOT_URLCONF = "Papertrail.urls"
WSGI_APPLICATION = "Papertrail.wsgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Database
DATABASES = {"default": env.db("DATABASE_URL", default="sqlite:///db.sqlite3")}
DATABASES["default"].setdefault("CONN_MAX_AGE", env.int("DB_CONN_MAX_AGE", default=60))
if DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3":
    # Concurrent writers (webhooks, QStash workers, web requests) must queue
    # rather than fail with "database is locked". WAL allows a reader while a
    # writer is active; busy_timeout makes writers wait; IMMEDIATE takes the
    # write lock up front so we never block mid-transaction. Dev-only — the
    # production Postgres connection needs connect_timeout instead.
    DATABASES["default"].setdefault(
        "OPTIONS",
        {
            "init_command": (
                "PRAGMA journal_mode=WAL;"
                "PRAGMA busy_timeout=20000;"
                "PRAGMA synchronous=NORMAL;"
            ),
            "transaction_mode": "IMMEDIATE",
        },
    )
else:
    DATABASES["default"].setdefault("OPTIONS", {"connect_timeout": 10})

# Apps
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    # Security
    "csp",
    "corsheaders",
    # Django QStash
    "django_qstash",
    "django_qstash.results",
    "django_qstash.schedules",
    # Allauth apps
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth_ui",
    "allauth.socialaccount.providers.google",
    "allauth.socialaccount.providers.github",
    # Customization apps
    "tailwind",
    "theme",
    "widget_tweaks",
    "slippers",
    "django_filters",
    "simple_history",
    "django.contrib.humanize",
    # Local apps
    "core.apps.CoreConfig",
    "documents.apps.DocumentsConfig",
    "records.apps.RecordsConfig",
    "accounting.apps.AccountingConfig",
    "reimbursements.apps.ReimbursementsConfig",
    "plaid_integration.apps.PlaidIntegrationConfig",
    "billing.apps.BillingConfig",
    # Webpush
    "webpush",
    # Stripe
    "djstripe",
]

MIDDLEWARE = [
    "core.middleware.RequestIDMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "posthog.integrations.django.PosthogContextMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",
    "core.middleware.HtmxMessageMiddleware",  # Send messages without reload
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "csp.middleware.CSPMiddleware",
    "core.middleware.TimezoneMiddleware",  # Get user timezone via cookie
    "allauth.account.middleware.AccountMiddleware",
]

if not DEBUG:
    SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
    SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
    SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    CSRF_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    CSRF_COOKIE_SAMESITE = "Lax"
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# CSP - Content Security Policy
CONTENT_SECURITY_POLICY = {
    "DIRECTIVES": {
        "default-src": ("'self'",),
        "script-src": ("'self'", "'unsafe-inline'", "https://cdn.plaid.com", "https://*.plaid.com"),
        "style-src": (
            "'self'",
            "'unsafe-inline'",
            "https://fonts.googleapis.com",
        ),
        "font-src": ("'self'", "https://fonts.gstatic.com"),
        "img-src": ("'self'", "data:", "blob:", "https:"),
        "connect-src": (
            "'self'",
            "https://*.upstash.io",
            "https://*.resend.com",
            "https://*.plaid.com",
            "https://cdn.plaid.com",
        ),
        "frame-src": ("'self'", "https://cdn.plaid.com", "https://*.plaid.com"),
        "frame-ancestors": ("'none'",),
        "base-uri": ("'self'",),
        "form-action": ("'self'",),
        "object-src": ("'none'",),
    }
}

# CORS
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
CORS_ALLOW_CREDENTIALS = True

# Auth & Allauth
ACCOUNT_SIGNUP_FIELDS = ["email*"]
ACCOUNT_LOGIN_BY_CODE_SUPPORTS_RESEND = True
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_MAX_EMAIL_ADDRESSES = 3
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
LOGIN_REDIRECT_URL = "core:dashboard"
LOGOUT_REDIRECT_URL = "core:landing_page"
ACCOUNT_SIGNUP_REDIRECT_URL = "core:dashboard"
ACCOUNT_SESSION_REMEMBER = True
ACCOUNT_LOGIN_ON_EMAIL_CONFIRMATION = True
ACCOUNT_LOGOUT_ON_GET = False
ACCOUNT_EMAIL_NOTIFICATIONS = True
ACCOUNT_FORMS = {
    "signup": "core.forms.PasswordlessSignupForm",
    "login": "core.forms.PasswordlessLoginForm",
}
ACCOUNT_RATE_LIMITS = {
    "login": "3/m/ip",
    "login_failed": "3/5m/ip",
    "signup": "3/m/ip",
}
SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "APP": {
            "client_id": env("GOOGLE_OAUTH_CLIENT_ID"),
            "secret": env("GOOGLE_OAUTH_CLIENT_SECRET"),
            "key": "",
        },
    },
    "github": {
        "APP": {
            "client_id": env("GITHUB_OAUTH_CLIENT_ID"),
            "secret": env("GITHUB_OAUTH_CLIENT_SECRET"),
            "key": "",
        },
    },
}
AUTHENTICATION_BACKENDS = [
    "core.backends.SelectRelatedModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

# Password validation (disabled — project uses passwordless auth via allauth login-by-code)
AUTH_PASSWORD_VALIDATORS: list = []

# Custom user model to check for subscription and customer
AUTH_USER_MODEL = "billing.CustomUser"


# Templates
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.webpush_status",  # Check user webpush status
                "billing.context_processors.subscription_status",  # Subscription status for all templates
            ],
            "builtins": [
                "django.templatetags.static",
            ],
        },
    },
]

# Cache & Session
if DEBUG:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        },
    }
    SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
    SESSION_CACHE_ALIAS = "default"
else:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": env("REDIS_URL"),
            "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},  # type: ignore[dict-item]
        },
    }
    SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
    SESSION_CACHE_ALIAS = "default"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 7  # 7 days
SESSION_EXPIRE_AT_BROWSER_CLOSE = True

# Email
EMAIL_BACKEND = "core.backends.QStashEmailBackend"  # Use custom backend to queue emails sending, and use anymail
ANYMAIL = {"RESEND_API_KEY": env("RESEND_API_KEY")}
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="Papertrail <onboarding@resend.dev>")

# Storage (S3/R2) - Uploads use signed urls in Cloudflare R2
R2_ACCESS_KEY_ID = env("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = env("R2_SECRET_ACCESS_KEY")
R2_STORAGE_BUCKET_NAME = env("R2_STORAGE_BUCKET_NAME")
R2_S3_ENDPOINT_URL = env("R2_S3_ENDPOINT_URL")
R2_PAPERTRAIL_STORAGE_ACCOUNT_ID = env("R2_PAPERTRAIL_STORAGE_ACCOUNT_ID")

AWS_S3_FILE_OVERWRITE = False
AWS_ACCESS_KEY_ID = R2_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY = R2_SECRET_ACCESS_KEY
AWS_STORAGE_BUCKET_NAME = R2_STORAGE_BUCKET_NAME
AWS_S3_ENDPOINT_URL = R2_S3_ENDPOINT_URL
AWS_S3_REGION_NAME = "auto"
AWS_DEFAULT_ACL = None
AWS_QUERYSTRING_AUTH = True
AWS_S3_VERIFY = True
AWS_S3_MAX_MEMORY_SIZE = 5 * 1024 * 1024
STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
        "OPTIONS": {
            "bucket_name": AWS_STORAGE_BUCKET_NAME,
            "endpoint_url": AWS_S3_ENDPOINT_URL,
            "access_key": AWS_ACCESS_KEY_ID,
            "secret_key": AWS_SECRET_ACCESS_KEY,
            "region_name": AWS_S3_REGION_NAME,
            "default_acl": AWS_DEFAULT_ACL,
            "querystring_auth": AWS_QUERYSTRING_AUTH,
            "file_overwrite": False,
            "custom_domain": False,
        },
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage",
    },
}
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

# AI / OCR
GEMINI_API_KEY = env("GEMINI_API_KEY")

# QStash
QSTASH_TOKEN = env("QSTASH_TOKEN")
DJANGO_QSTASH_DOMAIN = env("DJANGO_QSTASH_DOMAIN")
DJANGO_QSTASH_WEBHOOK_PATH = env("DJANGO_QSTASH_WEBHOOK_PATH")
QSTASH_CURRENT_SIGNING_KEY = env("QSTASH_CURRENT_SIGNING_KEY")
QSTASH_NEXT_SIGNING_KEY = env("QSTASH_NEXT_SIGNING_KEY")

# Internationalization & Static
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_ROOT = BASE_DIR / "staticfiles"
STATIC_URL = "static/"


TAILWIND_APP_NAME = "theme"
TAILWIND_USE_STANDALONE_BINARY = True
SITE_ID = 1

# List view pagination
PAGINATE_BY = 25

# Logging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {
            "()": "core.middleware.RequestIDLogFilter",
        },
    },
    "formatters": {
        "verbose": {
            "format": "[{request_id}] {levelname} {asctime} {module} {process:d} {thread:d} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
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

# Celery/QStash settings
DJANGO_QSTASH_QUEUE_NAME = "default"
DJANGO_QSTASH_MAX_RETRIES = 3
DJANGO_QSTASH_BACKOFF_FACTOR = 2

# Webpush
WEBPUSH_SETTINGS = {
    "VAPID_PUBLIC_KEY": env("WEB_PUSH_PUBLIC_KEY"),
    "VAPID_PRIVATE_KEY": env("WEB_PUSH_PRIVATE_KEY"),
    "VAPID_ADMIN_EMAIL": env("WEB_PUSH_EMAIL"),
}

# Plaid
PLAID_CLIENT_ID = env("PLAID_CLIENT_ID")
PLAID_SECRET = env("PLAID_SECRET")
PLAID_ENV = env("PLAID_ENV")
PLAID_WEBHOOK_URL = env("PLAID_WEBHOOK_URL")

# Accounting
IMPORT_EXPORT_ESCAPE_FORMULAE_ON_EXPORT = (
    True  # Force Excel cell formulas to automatically escape (security measure)
)

# Fernet
FERNET_KEYS = [
    env("FERNET_KEY"),
]

# Stripe
STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = env("STRIPE_PUBLISHABLE_KEY")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET")
DJSTRIPE_FOREIGN_KEY_TO_FIELD = env("DJSTRIPE_FOREIGN_KEY_TO_FIELD")
STRIPE_PRICING_TABLE_ID = env("STRIPE_PRICING_TABLE_ID")
STRIPE_PRICING_TABLE_KEY = env("STRIPE_PRICING_TABLE_KEY")

# One Stripe webhook serves both pipelines: djstripe (billing/subscriptions) and
# reimbursements (see billing/webhooks.py signal receiver). In local dev the
# signing secret of the "stripe listen" webhook is stored on the djstripe
# WebhookEndpoint row (djstripe_validation_method="verify_signature").

# Sentry
_is_prod = env("SENTRY_ENVIRONMENT", default="development") == "production"
sentry_sdk.init(
    dsn=env("SENTRY_DSN"),
    environment=env("SENTRY_ENVIRONMENT", default="development"),
    # Add data like request headers and IP for users,
    # see https://docs.sentry.io/platforms/python/data-management/data-collected/ for more info
    send_default_pii=True,
    # Enable sending logs to Sentry
    enable_logs=True,
    # Set traces_sample_rate to 1.0 to capture 100%
    # of transactions for tracing.
    traces_sample_rate=1.0 if not _is_prod else 0.1,
    # Set profile_session_sample_rate to 1.0 to profile 100%
    # of profile sessions.
    profile_session_sample_rate=1.0 if not _is_prod else 0.1,
    # Set profile_lifecycle to "trace" to automatically
    # run the profiler on when there is an active transaction
    profile_lifecycle="trace",
)
