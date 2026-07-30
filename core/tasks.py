"""Background tasks for delivering email and webpush notifications.

Tasks are executed asynchronously via QStash (django-qstash). Email delivery
uses the Resend provider through django-anymail.
"""

import logging

from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives, get_connection
from django_qstash import shared_task
from webpush import send_user_notification

logger = logging.getLogger(__name__)


@shared_task
def send_background_email(subject, message, from_email, recipient_list, html_message=None):
    """Send an email via the Resend backend as a background task.

    Supports optional HTML content for richer email templates.
    """
    resend_connection = get_connection(backend="anymail.backends.resend.EmailBackend")

    email = EmailMultiAlternatives(
        subject=subject,
        body=message,
        from_email=from_email,
        to=recipient_list,
        connection=resend_connection,
    )

    if html_message:
        email.attach_alternative(html_message, "text/html")

    email.send()


@shared_task
def fire_single_webpush(user_id: int, payload: dict, ttl: int = 1000) -> None:
    """Dispatch a single webpush notification to a user via django-webpush.

    Runs as a background task to avoid blocking the request cycle. Failures
    are logged but never raised to prevent task retries for transient issues.
    """
    """Async worker task wrapper around the webpush service execution."""
    try:
        User = get_user_model()
        user = User.objects.get(id=user_id)
        send_user_notification(user=user, payload=payload, ttl=ttl)
        logger.info(f"Dispatched webpush to {user.email}")
    except User.DoesNotExist:
        logger.error(f"Abandoning webpush task. User ID {user_id} not found.")
    except Exception as e:
        logger.error(f"Failed webpush delivery to user {user_id}: {e}")
