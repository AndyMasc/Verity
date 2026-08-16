"""Shared notification helpers for the reimbursement system."""

from django.conf import settings
from django.template.loader import render_to_string
from django.utils.html import escape

from core.currencies import format_currency
from core.tasks import EmailTaskPayload, send_background_email


def build_package_url(package_uuid: str) -> str:
    site_url = getattr(settings, "SITE_URL", "http://localhost:8000")
    return f"{site_url}/reimbursements/{package_uuid}/"


def build_pay_url(package_uuid: str) -> str:
    site_url = getattr(settings, "SITE_URL", "http://localhost:8000")
    return f"{site_url}/reimbursements/pay/{package_uuid}/"


def _site_context() -> dict:
    from core.services.notifications import build_site_context

    return build_site_context()


def send_package_created_notification(package, recipient=None) -> None:
    """Notify the recipient that a reimbursement package has been sent to them.

    Registered recipients get the full multi-channel notification; external
    (unauthenticated) recipients are emailed their payment link, which
    requires email verification before viewing or paying.
    """
    from core.services.notifications import send_multi_channel_notification

    recipient = recipient or package.recipient
    plain_title = package.title
    amount = package.display_total
    currency = package.currency
    plain_creator = package.creator.get_full_name() or package.creator.email

    if recipient is None:
        package_url = build_pay_url(package.uuid)
        recipient_name = package.recipient_email or ""
        if not recipient_name:
            return
        subject = f'{plain_creator} sent you a reimbursement request: "{plain_title}"'
        template_context = {
            "creator_name": escape(plain_creator),
            "recipient_name": escape(recipient_name),
            "title": escape(package.title),
            "amount": amount,
            "currency": currency,
            "package_url": package_url,
            "records": package.records.filter(is_active=True),
            **_site_context(),
        }
        html_body = render_to_string(
            "reimbursements/email/package_created_message.html", template_context
        )
        text_body = render_to_string(
            "reimbursements/email/package_created_message.txt", template_context
        )
        send_background_email.send(
            EmailTaskPayload(
                subject=subject,
                message=text_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[recipient_name],
                html_message=html_body,
            )
        )
        return

    package_url = build_package_url(package.uuid)
    creator_name = escape(plain_creator)
    safe_title = escape(package.title)
    safe_recipient = escape(recipient.get_full_name() or recipient.email)
    subject = f'{plain_creator} sent you a reimbursement request: "{plain_title}"'

    template_context = {
        "creator_name": creator_name,
        "recipient_name": safe_recipient,
        "title": safe_title,
        "amount": amount,
        "currency": currency,
        "package_url": package_url,
        "records": package.records.filter(is_active=True),
        **_site_context(),
    }

    html_body = render_to_string(
        "reimbursements/email/package_created_message.html", template_context
    )
    text_body = render_to_string(
        "reimbursements/email/package_created_message.txt", template_context
    )

    formatted_amount = format_currency(amount, currency)
    db_message = f'{plain_creator} sent you a reimbursement request for "{plain_title}" ({formatted_amount}).'

    send_multi_channel_notification(
        user=recipient,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        webpush_payload={
            "head": "Reimbursement Request",
            "body": db_message,
            "url": package_url,
        },
        send_db=True,
        db_message=db_message,
    )


def send_package_paid_notification(package, payer) -> None:
    """Send email + in-app notification to the package creator when paid."""
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
        **_site_context(),
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
