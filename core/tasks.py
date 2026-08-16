"""Background tasks for delivering email and webpush notifications.

Tasks are executed asynchronously via Dramatiq. Email delivery
uses the Resend provider through django-anymail.
"""

import logging
from dataclasses import asdict, dataclass

import dramatiq
from anymail.exceptions import AnymailRequestsAPIError
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives, get_connection
from dramatiq.encoder import JSONEncoder
from webpush import send_user_notification

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailTaskPayload:
    """Structured payload for asynchronous email delivery."""

    subject: str
    message: str
    from_email: str
    recipient_list: list[str]
    html_message: str | None = None


class EmailPayloadEncoder(JSONEncoder):
    """Dramatiq JSON encoder that serializes "EmailTaskPayload" dataclasses.

    Actors only carry JSON-safe arguments over the broker, so the payload is
    converted with "asdict" at enqueue time while call sites keep the typed
    dataclass. Selected via "DRAMATIQ_ENCODER" in settings.
    """

    def encode(self, data):
        return super().encode(_json_safe(data))


def _json_safe(value):
    """Recursively replace "EmailTaskPayload" instances with plain dicts."""
    if isinstance(value, EmailTaskPayload):
        return asdict(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _build_email_message(payload: EmailTaskPayload, connection):
    email = EmailMultiAlternatives(
        subject=payload.subject,
        body=payload.message,
        from_email=payload.from_email,
        to=payload.recipient_list,
        connection=connection,
    )

    if payload.html_message:
        email.attach_alternative(payload.html_message, "text/html")

    return email


def _is_permanent_email_error(error: AnymailRequestsAPIError) -> bool:
    status = getattr(error, "status_code", None)
    return status is not None and 400 <= status < 500 and status != 429


@dramatiq.actor(max_retries=3)
def send_background_email(payload: EmailTaskPayload):
    """Send an email via the backend as a background task.

    Supports optional HTML content for richer email templates. Permanent
    rejections (4xx, e.g. invalid recipient) are logged and skipped so Dramatiq
    doesn't retry them; transient failures (network, 5xx, rate limits) raise
    so Dramatiq retries.
    """
    resend_connection = get_connection(backend="anymail.backends.resend.EmailBackend")
    email = _build_email_message(payload, resend_connection)

    try:
        email.send()
    except AnymailRequestsAPIError as exc:
        if _is_permanent_email_error(exc):
            logger.error(
                "Email permanently rejected (%s) to %s: %s",
                getattr(exc, "status_code", None),
                payload.recipient_list,
                exc,
            )
            return
        raise
    except Exception:
        logger.exception("Email send failed to %s", payload.recipient_list)
        raise


@dramatiq.actor
def fire_single_webpush(user_id: int, payload: dict, ttl: int = 1000) -> None:
    """Dispatch a single webpush notification to a user via django-webpush.

    Runs as a background task to avoid blocking the request cycle. Failures
    are logged but never raised to prevent task retries for transient issues.
    """
    try:
        User = get_user_model()
        user = User.objects.get(id=user_id)
        send_user_notification(user=user, payload=payload, ttl=ttl)
        logger.info(f"Dispatched webpush to {user.email}")
    except User.DoesNotExist:
        logger.error(f"Abandoning webpush task. User ID {user_id} not found.")
    except Exception as e:
        logger.error(f"Failed webpush delivery to user {user_id}: {e}")
