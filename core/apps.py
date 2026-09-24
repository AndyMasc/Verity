import atexit
import os

from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from posthog import Posthog

posthog_client: Posthog | None = None


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        global posthog_client

        import core.signals  # noqa: F401

        project_token = os.environ.get("POSTHOG_PROJECT_TOKEN")
        host = os.environ.get("POSTHOG_HOST")

        if not project_token or not host:
            if settings.DEBUG:
                missing_variable = "POSTHOG_PROJECT_TOKEN" if not project_token else "POSTHOG_HOST"
                raise ImproperlyConfigured(
                    f"{missing_variable} variable required by PostHog is missing or un-configured, "
                    f"this causes events to be silently missed. This error stops appearing once "
                    f"{missing_variable} is configured"
                )
            return

        posthog_client = Posthog(
            project_api_key=project_token,
            host=host,
            enable_exception_autocapture=True,
            privacy_mode=False,
        )
        atexit.register(posthog_client.shutdown)
        settings.POSTHOG_MW_CLIENT = posthog_client

        from core.posthog_logs import configure_posthog_log_export

        configure_posthog_log_export(project_token, host)
