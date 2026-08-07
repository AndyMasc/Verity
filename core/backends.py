"""Custom backends for email delivery and optimized user loading.

``DramatiqEmailBackend`` sends messages asynchronously via Dramatiq.
``SelectRelatedModelBackend`` prefetches ``user.settings`` on every
auth lookup to avoid per-view lazy FK queries.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.core.mail.backends.base import BaseEmailBackend

from .tasks import send_background_email


class DramatiqEmailBackend(BaseEmailBackend):
    """Email backend that enqueues each message as a Dramatiq background task.

    Extracts HTML alternatives from the message and forwards everything to
    ``send_background_email`` for delivery through the Resend provider.
    """

    def send_messages(self, email_messages):
        if not email_messages:
            return 0

        sent_count = 0
        for message in email_messages:
            html_message = None
            if hasattr(message, "alternatives") and message.alternatives:
                for content, mimetype in message.alternatives:
                    if mimetype == "text/html":
                        html_message = content

            send_background_email.send(
                subject=message.subject,
                message=message.body,
                from_email=message.from_email or settings.DEFAULT_FROM_EMAIL,
                recipient_list=message.to,
                html_message=html_message,
            )
            sent_count += 1

        return sent_count


class SelectRelatedModelBackend(ModelBackend):
    """Auth backend that attaches ``UserSettings`` via ``select_related``.

    This eliminates the lazy FK query when templates or views access
    ``request.user.settings`` — the join happens once during the initial
    user load instead of on every access.
    """

    def get_user(self, user_id):
        User = get_user_model()
        try:
            return User.objects.select_related("settings").get(pk=user_id)
        except User.DoesNotExist:
            return None
