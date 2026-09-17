"""Django settings module for ``collectstatic`` during the Docker image build.

The production settings require secrets (REDIS_URL, ALLOWED_HOSTS, API keys...)
and raise ``ImproperlyConfigured`` when absent. At image build time none exist,
yet the image must ship static files so WhiteNoise serves them under any runtime
command. This module injects placeholders into ``os.environ`` FIRST, then boots
the real app settings exactly as production does (Verity.settings dispatcher).

It lives outside ``Verity.settings`` on purpose: that package's __init__ imports
the app settings before any submodule body runs, so placeholders set there are
too late — this module's import body runs before ``Verity.settings`` is touched.

Only ``collectstatic`` is ever run against these settings; no DB, cache, or
external service is connected.
"""

import os

_REQUIRED_ENV = {
    "POSTHOG_PROJECT_TOKEN": "staticbuild",
    "SECRET_KEY": "staticbuild",
    "GOOGLE_OAUTH_CLIENT_ID": "staticbuild",
    "GOOGLE_OAUTH_CLIENT_SECRET": "staticbuild",
    "GITHUB_OAUTH_CLIENT_ID": "staticbuild",
    "GITHUB_OAUTH_CLIENT_SECRET": "staticbuild",
    "REDIS_URL": "redis://localhost:6379/0",
    "RESEND_API_KEY": "staticbuild",
    "R2_ACCESS_KEY_ID": "staticbuild",
    "R2_SECRET_ACCESS_KEY": "staticbuild",
    "R2_STORAGE_BUCKET_NAME": "staticbuild",
    "R2_S3_ENDPOINT_URL": "https://staticbuild.example.invalid",
    "R2_VERITY_STORAGE_ACCOUNT_ID": "staticbuild",
    "GEMINI_API_KEY": "staticbuild",
    "WEB_PUSH_PUBLIC_KEY": "staticbuild",
    "WEB_PUSH_PRIVATE_KEY": "staticbuild",
    "WEB_PUSH_EMAIL": "staticbuild@example.invalid",
    "PLAID_CLIENT_ID": "staticbuild",
    "PLAID_SECRET": "staticbuild",
    "PLAID_ENV": "sandbox",
    "PLAID_WEBHOOK_URL": "https://staticbuild.example.invalid/plaid/webhook/",
    "FERNET_KEY": "staticbuild",
    "STRIPE_SECRET_KEY": "rk_live_staticbuild",
    "STRIPE_PUBLISHABLE_KEY": "pk_staticbuild",
    "DJSTRIPE_FOREIGN_KEY_TO_FIELD": "id",
    "RABBIT_MQ_URL": "amqp://guest:guest@localhost:5672/%2F",
    "ALLOWED_HOSTS": "localhost",
}

for _name, _value in _REQUIRED_ENV.items():
    os.environ.setdefault(_name, _value)

from Verity.settings import *  # noqa: F403,E402
