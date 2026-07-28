import logging

from django.apps import AppConfig
from django.conf import settings

logger = logging.getLogger(__name__)


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        import core.signals  # noqa: F401

        token = getattr(settings, "POSTHOG_PROJECT_TOKEN", "")
        if not token:
            if settings.DEBUG:
                raise RuntimeError(
                    "POSTHOG_PROJECT_TOKEN variable required by PostHog is missing or "
                    "un-configured, this causes events to be silently missed. "
                    "This error stops appearing once POSTHOG_PROJECT_TOKEN is configured."
                )
            return

        import posthog

        posthog.api_key = token
        posthog.host = getattr(settings, "POSTHOG_HOST", "https://us.i.posthog.com")
        posthog.disabled = getattr(settings, "POSTHOG_DISABLED", False)

        if settings.DEBUG:
            posthog.debug = True

        from core import posthog_signals  # noqa: F401
