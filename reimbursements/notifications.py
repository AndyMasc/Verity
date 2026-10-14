"""Shared notification helpers for the reimbursement system."""

import logging

from django.conf import settings
from django.template.loader import render_to_string
from django.utils.html import escape

from core.currencies import format_currency
from core.models import Notification
from core.services.notifications import build_site_context
from core.tasks import EmailTaskPayload, send_background_email

from .models import ReimbursementPackage

logger = logging.getLogger(__name__)


def build_package_url(package_uuid: str) -> str:
    site_url = getattr(settings, "SITE_URL", "http://localhost:8000")
    return f"{site_url}/reimbursements/{package_uuid}/"


def invoice_invoice_sent(package: ReimbursementPackage) -> None:
    """Tell the creator the invoice is out; Stripe mails the payer the link."""
    recipient = package.recipient_address
    if not recipient:
        return
    creator = escape(package.creator.get_full_name() or package.creator.email)
    subject = f'Reimbursement request sent: "{package.title}"'
    body = (
        f'{creator} sent you a reimbursement request for "{package.title}" '
        f"({format_currency(package.total_amount, package.currency)}). "
        f"Stripe has emailed you a link to view and pay it."
    )
    send_background_email.send(
        EmailTaskPayload(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient],
        )
    )
    # A registered recipient also needs the in-app record; email alone left
    # the notification panel empty for them.
    if package.recipient_id is not None:
        Notification.objects.create(
            recipient=package.recipient,
            subject=subject,
            message=f'{creator} sent you a reimbursement request for "{package.title}".',
        )
    logger.info("Invoice raised for package %s -> %s", package.uuid, recipient)


def send_package_paid_notification(package, payer) -> None:
    from core.services.notifications import send_multi_channel_notification

    package_url = build_package_url(package.uuid)
    payer_name = escape(payer.get_full_name() or payer.email) if payer else "Someone"
    safe_title = escape(package.title)
    safe_creator = escape(package.creator.get_full_name() or package.creator.email)
    amount = package.display_total
    currency = package.currency

    plain_title = package.title
    subject = f'Your reimbursement "{plain_title}" was paid'

    template_context = {
        "creator_name": safe_creator,
        "payer_name": payer_name,
        "title": safe_title,
        "amount": amount,
        "currency": currency,
        "package_url": package_url,
        **build_site_context(),
    }

    html_body = render_to_string("reimbursements/email/package_paid_message.html", template_context)
    text_body = render_to_string("reimbursements/email/package_paid_message.txt", template_context)

    formatted_amount = format_currency(amount, currency)
    plain_payer = payer.get_full_name() or payer.email if payer else "Someone"
    db_message = f'{plain_payer} paid {formatted_amount} for "{plain_title}".'

    send_multi_channel_notification(
        user=package.creator,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        webpush_payload={
            "head": "Reimbursement Paid",
            "body": db_message,
            "url": package_url,
        },
        send_db=True,
        db_message=db_message,
    )
