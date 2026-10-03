"""Record sharing notifications: email, webpush, and in-app delivery.

Funnels through "core.services.notifications.send_multi_channel_notification"
so delivery respects each user's push/email preferences (set in settings) and
runs asynchronously on the background broker.

All sending is fire-and-forget: callers must never let a notification failure
block the share grant itself.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.html import escape

from core.currencies import format_currency
from core.services.notifications import build_site_context

if TYPE_CHECKING:
    from records.models import RecordShare

logger = logging.getLogger(__name__)


def build_record_url(record_id: int) -> str:
    """Absolute URL to a record's detail page.

    The path comes from "reverse" rather than a literal: the route is mounted
    under /records/, so a hand-written "/record_detail/<id>/" 404s.
    """
    site_url = getattr(settings, "SITE_URL", "http://localhost:8000")
    return f"{site_url}{reverse('records:record_detail', args=[record_id])}"


def send_records_shared_notification(*, shares: list[RecordShare], actor) -> None:
    """Notify each recipient about every record they were just granted, in one email.

    Grants are grouped by recipient so sharing 20 records with someone produces
    one email listing all 20, not 20 separate emails. Never raises: failures are
    logged so a broker hiccup cannot fail the share itself.
    """
    grouped: dict[int, list[RecordShare]] = {}
    for share in shares:
        grouped.setdefault(share.user_id, []).append(share)

    for recipient_shares in grouped.values():
        try:
            _notify_recipient(shares=recipient_shares, actor=actor)
        except Exception:
            logger.exception(
                "Failed to deliver share notification for records %s to user %s",
                ", ".join(str(s.record_id) for s in recipient_shares),
                recipient_shares[0].user_id,
            )


def _notify_recipient(*, shares: list[RecordShare], actor) -> None:
    """Build and dispatch one recipient's share email, webpush, and in-app message."""
    from core.services.notifications import send_multi_channel_notification
    from records.models import RecordShare

    recipient = shares[0].user
    plain_actor = actor.get_full_name() or actor.email
    site_context = build_site_context()
    multiple = len(shares) > 1

    rows = [
        {
            "title": escape(share.record.title or "Untitled record"),
            "merchant": escape(share.record.merchant or ""),
            "date": share.record.transaction_date,
            "amount": format_currency(share.record.balance, share.record.currency or "usd"),
            "url": build_record_url(share.record.pk),
        }
        for share in shares
    ]

    if multiple:
        subject = f"{plain_actor} shared {len(shares)} records with you"
        db_message = f"{plain_actor} shared {len(shares)} records with you."
        webpush_url = site_context["site_url"]
    else:
        plain_title = shares[0].record.title or "Untitled record"
        subject = f'{plain_actor} shared a record with you: "{plain_title}"'
        db_message = (
            f'{plain_actor} shared the record "{plain_title}" with you ({rows[0]["amount"]}).'
        )
        webpush_url = rows[0]["url"]

    template_context = {
        "recipient_name": recipient.get_full_name() or recipient.email,
        "actor_name": escape(plain_actor),
        "rows": rows,
        "multiple": multiple,
        "can_edit": all(s.permission == RecordShare.Permission.EDIT for s in shares),
        "record_count": len(shares),
        "record_url": rows[0]["url"],
        **site_context,
    }

    html_body = render_to_string("records/email/record_shared_message.html", template_context)
    text_body = render_to_string("records/email/record_shared_message.txt", template_context)

    send_multi_channel_notification(
        user=recipient,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        webpush_payload={
            "head": "Record Shared" if not multiple else "Records Shared",
            "body": db_message,
            "url": webpush_url,
        },
        send_db=True,
        db_message=db_message,
    )
