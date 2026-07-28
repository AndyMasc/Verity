"""PostHog identity for the login request.

The PosthogContextMiddleware reads request.user once, before any view runs.
On a login request the visitor is still anonymous at that point, so a bare
capture() would be unattributed. This signal runs inside the login request
and fixes the ambient context, so every capture later in that same request
is attributed to the user who just logged in.
"""

import posthog
from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver
from posthog import identify_context


@receiver(user_logged_in)
def identify_posthog_user(sender, request, user, **kwargs):  # noqa: ARG001
    identify_context(str(user.pk))

    posthog.set(
        distinct_id=str(user.pk),
        properties={
            "email": user.email,
            "is_staff": user.is_staff,
            "date_joined": user.date_joined.isoformat(),
        },
    )
