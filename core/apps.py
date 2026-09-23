from django.apps import AppConfig
from django.conf import settings

from core.posthog_client import initialize_posthog


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        import core.signals  # noqa: F401

        token = getattr(settings, "POSTHOG_PROJECT_TOKEN", "")
        host = getattr(settings, "POSTHOG_HOST", "")
        for variable_name, value in (
            ("POSTHOG_PROJECT_TOKEN", token),
            ("POSTHOG_HOST", host),
        ):
            if not value:
                if settings.DEBUG:
                    raise RuntimeError(
                        f"{variable_name} variable required by PostHog is missing or "
                        "un-configured, this causes events to be silently missed. "
                        f"This error stops appearing once {variable_name} is configured."
                    )
                return

        settings.POSTHOG_MW_CLIENT = initialize_posthog(token, host)

        from core import posthog_signals  # noqa: F401
