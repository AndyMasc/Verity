"""Shared PostHog client initialized during Django application startup."""

import atexit

from posthog import Posthog

posthog_client: Posthog | None = None


def initialize_posthog(project_api_key: str, host: str) -> Posthog:
    """Create the process-wide PostHog client once Django is ready."""
    global posthog_client

    if posthog_client is None:
        posthog_client = Posthog(
            project_api_key,
            host=host,
            enable_exception_autocapture=True,
        )
        atexit.register(posthog_client.shutdown)

    return posthog_client


def get_posthog_client() -> Posthog | None:
    """Return the startup-initialized client, if PostHog is configured."""
    return posthog_client
