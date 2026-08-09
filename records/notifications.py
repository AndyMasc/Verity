"""Record sharing notifications: email, webpush, and in-app delivery.

Funnels through ``core.services.notifications.send_multi_channel_notification``
so delivery respects each user's push/email preferences (set in settings) and
runs asynchronously on the background broker.

All sending is fire-and-forget: callers mustnever let a notification failure
block the share grant itself.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.conf import settings
from django.template.loader import render_to_string
from django.utils.html import escape

from core.currencies import format_currency

if TYPE_CHECKING:
    from records.models import Record, RecordShare

logger = logging.getLogger(__name__)


def build_record_url(record_id: int) -> str:
    site_url = getattr(settings, "SITE_URL", "http://localhost:8000")
    return f"{site_url}/record_detail/{record_id}/"


def send_record_shared_notification(*, record: Record, share: RecordShare, actor) -> None:
    """Notify the share recipient that a record has been shared with them.

    Bulds the subject line, renders the email body (HTML + plain text), and
    dispatches across push/email/in-app channels in one call. Never raises:
    failures are logged so a broker hiccup cannot fail the share itself.
    """
    from core.services.notifications import send_multi_channel_notification

    recipient = share.user
    record_url = build_record_url(record.pk)

    plain_actor = actor.get_full_name() or actor.email
    plain_title = record.title or "Untitled record"
    safe_actor = escape(plain_actor)
    safe_title = escape(plain_title)

    subject = f'{plain_actor} shared a record with you: "{plain_title}"'

    amount = record.balance
    currency = record.currency or "usd"
    formatted_amount = format_currency(amount, currency)

    template_context = {
        "recipient_name": recipient.get_full_name() or recipient.email,
        "actor_name": safe_actor,
        "title": safe_title,
        "merchant": escape(record.merchant or ""),
        "amount": amount,
        "currency": currency,
        "record_url": record_url,
        "record": record,
        **_site_context(),
    }

    html_body = render_to_string("records/email/record_shared_message.html", template_context)
    text_body = render_to_string("records/email/record_shared_message.txt", template_context)

    db_message = f'{plain_actor} shared the record "{plain_title}" with you ({formatted_amount}).'

    try:
        send_multi_channel_notification(
            user=recipient,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            webpush_payload={
                "head": "Record Shared",
                "body": db_message,
                "url": record_url,
            },
            send_db=True,
            db_message=db_message,
        )
    except Exception:
        logger.exception(
            "Failed to deliver share notification for record %s to user %s",
            record.pk,
            recipient.pk,
        )


def _site_context() -> dict:
    from core.services.notifications import build_site_context

    return build_site_context()
